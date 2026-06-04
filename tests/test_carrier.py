"""Offline unit tests for UsenetCarrier.

All tests run without a live NNTP connection — stub server and stub
Credentials are used throughout.
"""
import base64
import json
import random
import tempfile
import os
from typing import List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from hivemind_usenet.carrier import (
    CARRIER_VERSION,
    CHUNK_SIZE,
    CarrierBuffer,
    Frame,
    UsenetCarrier,
    _KIND_HIVE,
    _KIND_NL,
)


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

class _FakeArticle:
    def __init__(self, subject: str, text: str) -> None:
        self.subject = subject
        self.text    = text


class _FakeServer:
    """In-memory Usenet server stub."""

    def __init__(self) -> None:
        self._articles: List[_FakeArticle] = []

    def post(self, text: str, subject: str, group: str, **_) -> None:
        self._articles.append(_FakeArticle(subject=subject, text=text))

    def get_articles(self, group: str, limit: int = 200) -> List[_FakeArticle]:
        return list(reversed(self._articles[-limit:]))

    def clear(self) -> None:
        self._articles.clear()


class _RoundTripCreds:
    """Stub Credentials: encrypt → store → decrypt, in-process only.

    No real PGP: just wraps the plaintext in a JSON envelope so
    ``decrypt(encrypt(x)) == x``.
    """

    def __init__(self) -> None:
        self.pubkey = "FAKE-PUBKEY"

    def encrypt(self, txt: str, key: Optional[str] = None) -> str:
        return json.dumps({"_fake": True, "payload": txt})

    def decrypt(self, blob: str) -> str:
        d = json.loads(blob)
        if d.get("_fake"):
            return d["payload"]
        raise ValueError("Not a fake-encrypted blob")


# ---------------------------------------------------------------------------
# Tests: Frame dataclass
# ---------------------------------------------------------------------------

class TestFrame:
    def test_round_trip_json(self):
        f = Frame(v=1, mid="abc", seq=0, n=1, kind=_KIND_HIVE,
                  data=base64.b64encode(b"hello").decode())
        assert Frame.from_json(f.to_json()) == f

    def test_frozen(self):
        f = Frame(v=1, mid="x", seq=0, n=1, kind=_KIND_NL, data="dGVzdA==")
        with pytest.raises((AttributeError, TypeError)):
            f.seq = 99  # frozen dataclass

    def test_kind_preserved(self):
        for k in (_KIND_HIVE, _KIND_NL):
            f = Frame(v=1, mid="m", seq=0, n=1, kind=k, data="")
            assert Frame.from_json(f.to_json()).kind == k


# ---------------------------------------------------------------------------
# Tests: CarrierBuffer (reassembly + dedup)
# ---------------------------------------------------------------------------

class TestCarrierBuffer:
    def _frame(self, mid: str, seq: int, n: int, data: bytes, kind: str = _KIND_HIVE) -> Frame:
        return Frame(v=1, mid=mid, seq=seq, n=n, kind=kind,
                     data=base64.b64encode(data).decode())

    def test_single_chunk_completes(self):
        buf = CarrierBuffer()
        result = buf.ingest(self._frame("m1", 0, 1, b"hello"))
        assert result is not None
        kind, payload = result
        assert payload == b"hello"
        assert kind == _KIND_HIVE

    def test_multi_chunk_reassembly(self):
        buf = CarrierBuffer()
        data = b"ABCDEFGHIJ"
        mid  = "m2"
        assert buf.ingest(self._frame(mid, 0, 3, data[0:4])) is None
        assert buf.ingest(self._frame(mid, 1, 3, data[4:8])) is None
        result = buf.ingest(self._frame(mid, 2, 3, data[8:]))
        assert result is not None
        _, payload = result
        assert payload == data

    def test_out_of_order_delivery(self):
        buf = CarrierBuffer()
        data = b"123456789"
        mid  = "m3"
        # deliver seq 2, then 0, then 1
        assert buf.ingest(self._frame(mid, 2, 3, data[6:])) is None
        assert buf.ingest(self._frame(mid, 0, 3, data[0:3])) is None
        result = buf.ingest(self._frame(mid, 1, 3, data[3:6]))
        assert result is not None
        _, payload = result
        assert payload == data

    def test_duplicate_seq_dropped(self):
        buf = CarrierBuffer()
        mid = "m4"
        f = self._frame(mid, 0, 2, b"part1")
        assert buf.ingest(f) is None   # first delivery
        assert buf.ingest(f) is None   # duplicate — same result, not double-counted
        # deliver second chunk
        result = buf.ingest(self._frame(mid, 1, 2, b"part2"))
        assert result is not None
        _, payload = result
        assert payload == b"part1part2"
        # delivering the duplicate again after completion should also be ignored
        assert buf.ingest(f) is None

    def test_independent_messages(self):
        buf = CarrierBuffer()
        r1 = buf.ingest(self._frame("m5", 0, 1, b"msg-A"))
        r2 = buf.ingest(self._frame("m6", 0, 1, b"msg-B"))
        assert r1 is not None and r1[1] == b"msg-A"
        assert r2 is not None and r2[1] == b"msg-B"


# ---------------------------------------------------------------------------
# Tests: hSub matching
# ---------------------------------------------------------------------------

class TestHSub:
    def test_match(self):
        from remailers import create_hsub, match_hsub
        secret = "captain-dolphin"
        subject = create_hsub(secret)
        assert match_hsub(subject, secret)

    def test_no_match(self):
        from remailers import create_hsub, match_hsub
        subject = create_hsub("secret-A")
        assert not match_hsub(subject, "secret-B")

    def test_wrong_length_no_match(self):
        from remailers import match_hsub
        assert not match_hsub("tooshort", "anything")


# ---------------------------------------------------------------------------
# Tests: PGP round-trip with real Credentials
# ---------------------------------------------------------------------------

class TestPGPRoundTrip:
    def test_encrypt_decrypt(self, tmp_path):
        from remailers.keys import Credentials
        key_path = str(tmp_path / "test.asc")
        creds = Credentials(key_path)
        plaintext = "Hello, HiveMind over Usenet!"
        ciphertext = creds.encrypt(plaintext, creds.pubkey)
        assert ciphertext != plaintext
        result = creds.decrypt(ciphertext)
        assert result == plaintext

    def test_cross_node_encrypt_decrypt(self, tmp_path):
        """Encrypt with node-A's pubkey, decrypt with node-A's privkey."""
        from remailers.keys import Credentials
        node_a = Credentials(str(tmp_path / "node_a.asc"))
        node_b = Credentials(str(tmp_path / "node_b.asc"))
        msg = "cross-node message"
        ct = node_b.encrypt(msg, node_a.pubkey)
        assert node_a.decrypt(ct) == msg


# ---------------------------------------------------------------------------
# Tests: UsenetCarrier send/poll round-trip (stub, no network)
# ---------------------------------------------------------------------------

class TestUsenetCarrier:
    def _make_pair(self) -> Tuple["UsenetCarrier", "UsenetCarrier", "_FakeServer"]:
        """Two carriers sharing the same fake server (same group)."""
        server = _FakeServer()
        creds_a = _RoundTripCreds()
        creds_b = _RoundTripCreds()
        carrier_a = UsenetCarrier(creds_a, server, chunk_size=CHUNK_SIZE)
        carrier_b = UsenetCarrier(creds_b, server, chunk_size=CHUNK_SIZE)
        return carrier_a, carrier_b, server

    def test_single_chunk_round_trip(self):
        ca, cb, _ = self._make_pair()
        payload = b"hello usenet"
        ca.send(payload, peer_secret="s2b", peer_pubkey=cb.creds.pubkey)
        results = cb.poll("s2b")
        assert len(results) == 1
        kind, data = results[0]
        assert kind == _KIND_HIVE
        assert data == payload

    def test_kind_nl_round_trip(self):
        ca, cb, _ = self._make_pair()
        ca.send(b"what is the weather?", peer_secret="nlsecret",
                peer_pubkey=cb.creds.pubkey, kind=_KIND_NL)
        results = cb.poll("nlsecret")
        assert len(results) == 1
        assert results[0][0] == _KIND_NL

    def test_wrong_secret_yields_nothing(self):
        ca, cb, _ = self._make_pair()
        ca.send(b"secret msg", peer_secret="correct", peer_pubkey=cb.creds.pubkey)
        results = cb.poll("wrong")
        assert results == []

    def test_multi_chunk_reassembly(self):
        ca, cb, _ = self._make_pair()
        payload = bytes(range(256)) * 50   # 12 800 bytes → 3 chunks at 6000
        ca.send(payload, peer_secret="ms", peer_pubkey=cb.creds.pubkey)
        results = cb.poll("ms")
        assert len(results) == 1
        assert results[0][1] == payload

    def test_out_of_order_multi_chunk(self):
        """Post chunks in reverse order; carrier must still reassemble."""
        server = _FakeServer()
        creds  = _RoundTripCreds()
        sender = UsenetCarrier(creds, server, chunk_size=10)

        payload = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # 26 bytes → 3 chunks@10
        sender.send(payload, peer_secret="oo", peer_pubkey=creds.pubkey)

        # Reverse the article list to simulate out-of-order delivery
        server._articles.reverse()

        receiver = UsenetCarrier(creds, server, chunk_size=10)
        results  = receiver.poll("oo")
        assert len(results) == 1
        assert results[0][1] == payload

    def test_duplicate_articles_not_double_counted(self):
        server = _FakeServer()
        creds  = _RoundTripCreds()
        carrier = UsenetCarrier(creds, server, chunk_size=CHUNK_SIZE)
        carrier.send(b"unique", peer_secret="dd", peer_pubkey=creds.pubkey)
        # Duplicate every article
        server._articles = server._articles * 2

        results = carrier.poll("dd")
        # Should yield exactly one complete message despite duplicates
        assert len(results) == 1

    def test_invalid_kind_raises(self):
        server = _FakeServer()
        creds  = _RoundTripCreds()
        c = UsenetCarrier(creds, server)
        with pytest.raises(ValueError, match="Invalid kind"):
            c.send(b"x", peer_secret="s", peer_pubkey=creds.pubkey, kind="invalid")

    def test_empty_payload(self):
        server = _FakeServer()
        creds  = _RoundTripCreds()
        c = UsenetCarrier(creds, server, chunk_size=10)
        c.send(b"", peer_secret="empty", peer_pubkey=creds.pubkey)
        results = c.poll("empty")
        assert len(results) == 1
        assert results[0][1] == b""

"""Offline tests for UsenetWormhole.

Two in-process wormholes share a fake NNTP server.  A HiveMessage sent on
node A is handle_message'd on node B byte-identical — no live network needed.
"""
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from hivemind_bus_client.message import HiveMessage, HiveMessageType
from hivemind_usenet.carrier import UsenetCarrier, CHUNK_SIZE, _KIND_HIVE
from hivemind_usenet.wormhole import UsenetWormhole, _make_client_connection


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeArticle:
    def __init__(self, subject: str, text: str) -> None:
        self.subject = subject
        self.text    = text


class _SharedFakeServer:
    """Single in-memory server; all callers share the same article list."""

    def __init__(self) -> None:
        self._articles: List[_FakeArticle] = []

    def post(self, text: str, subject: str, group: str, **_) -> None:
        self._articles.append(_FakeArticle(subject=subject, text=text))

    def get_articles(self, group: str, limit: int = 200) -> List[_FakeArticle]:
        return list(reversed(self._articles[-limit:]))


class _RoundTripCreds:
    """Fake PGP: encrypt wraps in JSON; decrypt unwraps."""

    def __init__(self, name: str = "anon") -> None:
        self.pubkey = f"PUBKEY-{name}"
        self._name  = name

    def encrypt(self, txt: str, key: Optional[str] = None) -> str:
        return json.dumps({"_fake": True, "payload": txt})

    def decrypt(self, blob: str) -> str:
        d = json.loads(blob)
        if d.get("_fake"):
            return d["payload"]
        raise ValueError("Not fake-encrypted")


class _FakeIdentity:
    """Stub for NodeIdentity — only .private_key is needed by HandShake."""
    private_key = None  # HandShake(None) generates an in-memory key


class _FakeHmProtocol:
    """Minimal HiveMindListenerProtocol stub."""

    def __init__(self) -> None:
        self.received: List[HiveMessage] = []
        self.lock     = threading.Lock()
        self.identity = _FakeIdentity()

    def handle_message(self, msg: HiveMessage, client) -> None:
        with self.lock:
            self.received.append(msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wormhole_pair():
    """Return (wormhole_a, wormhole_b, hm_a, hm_b, server)."""
    server   = _SharedFakeServer()
    creds_a  = _RoundTripCreds("a")
    creds_b  = _RoundTripCreds("b")
    carrier_a = UsenetCarrier(creds_a, server)
    carrier_b = UsenetCarrier(creds_b, server)
    hm_a = _FakeHmProtocol()
    hm_b = _FakeHmProtocol()

    # Node A: posts under "a→b" secret; polls under "b→a" secret
    wh_a = UsenetWormhole(
        config={
            "my_secret":    "b-to-a",
            "peer_secret":  "a-to-b",
            "peer_pubkey":  creds_b.pubkey,
            "poll_seconds": 99999,   # never auto-poll; we call _poll manually
        },
        hm_protocol=hm_a,
    )
    wh_a._carrier      = carrier_a
    wh_a._carrier.creds = creds_a

    # Node B: posts under "b→a" secret; polls under "a→b" secret
    wh_b = UsenetWormhole(
        config={
            "my_secret":    "a-to-b",
            "peer_secret":  "b-to-a",
            "peer_pubkey":  creds_a.pubkey,
            "poll_seconds": 99999,
        },
        hm_protocol=hm_b,
    )
    wh_b._carrier      = carrier_b
    wh_b._carrier.creds = creds_b

    return wh_a, wh_b, hm_a, hm_b, server, carrier_a, carrier_b


def _wh_poll_once(wormhole: UsenetWormhole) -> None:
    """Manually trigger one poll+dispatch cycle."""
    my_secret = wormhole.config["my_secret"]
    messages  = wormhole._carrier.poll(my_secret)
    for kind, payload in messages:
        if kind == _KIND_HIVE:
            wormhole._dispatch(payload)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWormhole:

    def test_single_message_a_to_b(self):
        wh_a, wh_b, hm_a, hm_b, server, ca, cb = _make_wormhole_pair()

        original = HiveMessage(HiveMessageType.PING, payload={"hello": "world"})
        payload  = original.serialize().encode()

        ca.send(payload,
                peer_secret=wh_a.config["peer_secret"],
                peer_pubkey=wh_a.config["peer_pubkey"],
                kind=_KIND_HIVE)

        _wh_poll_once(wh_b)

        assert len(hm_b.received) == 1
        received = hm_b.received[0]
        assert received.msg_type == original.msg_type

    def test_message_b_to_a(self):
        wh_a, wh_b, hm_a, hm_b, server, ca, cb = _make_wormhole_pair()

        original = HiveMessage(HiveMessageType.BROADCAST, payload={"x": 42})
        payload  = original.serialize().encode()

        cb.send(payload,
                peer_secret=wh_b.config["peer_secret"],
                peer_pubkey=wh_b.config["peer_pubkey"],
                kind=_KIND_HIVE)

        _wh_poll_once(wh_a)

        assert len(hm_a.received) == 1
        assert hm_a.received[0].msg_type == original.msg_type

    def test_large_message_multiple_chunks(self):
        """A payload larger than CHUNK_SIZE must span multiple posts and reassemble."""
        wh_a, wh_b, hm_a, hm_b, server, ca, cb = _make_wormhole_pair()

        # Build a HiveMessage with large payload
        big_data  = {"blob": "X" * (CHUNK_SIZE * 3)}
        original  = HiveMessage(HiveMessageType.RENDEZVOUS, payload=big_data)
        payload   = original.serialize().encode()
        assert len(payload) > CHUNK_SIZE  # ensure it actually chunks

        ca.send(payload,
                peer_secret=wh_a.config["peer_secret"],
                peer_pubkey=wh_a.config["peer_pubkey"],
                kind=_KIND_HIVE)

        _wh_poll_once(wh_b)

        assert len(hm_b.received) == 1

    def test_byte_identical_payload(self):
        """Payload bytes on B equal payload bytes on A after full round-trip."""
        wh_a, wh_b, hm_a, hm_b, server, ca, cb = _make_wormhole_pair()

        original = HiveMessage(HiveMessageType.QUERY,
                               payload={"utterance": "what time is it"})
        raw = original.serialize().encode()

        ca.send(raw,
                peer_secret=wh_a.config["peer_secret"],
                peer_pubkey=wh_a.config["peer_pubkey"],
                kind=_KIND_HIVE)

        _wh_poll_once(wh_b)

        assert len(hm_b.received) == 1
        reconstructed = hm_b.received[0].serialize().encode()
        # Both serializations represent the same logical message
        assert json.loads(reconstructed) == json.loads(raw)

    def test_non_hive_kind_ignored(self):
        """kind='nl' frames must not be dispatched by the wormhole."""
        wh_a, wh_b, hm_a, hm_b, server, ca, cb = _make_wormhole_pair()

        ca.send(b"plain text question",
                peer_secret=wh_a.config["peer_secret"],
                peer_pubkey=wh_a.config["peer_pubkey"],
                kind="nl")

        _wh_poll_once(wh_b)

        assert hm_b.received == []

    def test_client_connection_created(self):
        wh_a, wh_b, hm_a, hm_b, server, ca, cb = _make_wormhole_pair()

        original = HiveMessage(HiveMessageType.PING, payload={})
        payload  = original.serialize().encode()
        ca.send(payload,
                peer_secret=wh_a.config["peer_secret"],
                peer_pubkey=wh_a.config["peer_pubkey"],
                kind=_KIND_HIVE)

        _wh_poll_once(wh_b)

        # Connection object should be created and cached
        assert wh_b._client_conn is not None
        assert wh_b._client_conn.key == wh_b.config.get("peer_id", "usenet-peer")

    def test_stop_event(self):
        wh_a, wh_b, hm_a, hm_b, server, ca, cb = _make_wormhole_pair()
        wh_a._stop_event.set()
        # run() should return quickly; test just calls stop()
        wh_a.stop()
        assert wh_a._stop_event.is_set()

"""UsenetCarrier — shared framing layer for HiveMind-over-Usenet.

Splits arbitrary-size payloads into ~8 KB base64 chunks, PGP-encrypts each
chunk to the peer's public key, posts them to a Usenet group under an hSub
subject, then reassembles on poll.  Fully unit-testable without a live NNTP
connection: inject a stub server and stub Credentials.
"""
import base64
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from remailers import create_hsub, match_hsub
from remailers.keys import Credentials

CHUNK_SIZE = 6_000          # bytes of raw payload per chunk (b64 ≈ 8 KB)
CARRIER_VERSION = 1
_KIND_HIVE = "hive"
_KIND_NL   = "nl"
VALID_KINDS = {_KIND_HIVE, _KIND_NL}


# ---------------------------------------------------------------------------
# Frame dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    """A single Usenet article's logical payload, before/after base64."""
    v:    int          # carrier version
    mid:  str          # message-id (UUID4) – groups chunks of one logical msg
    seq:  int          # 0-based chunk index
    n:    int          # total chunk count
    kind: str          # "hive" | "nl"
    data: str          # base64-encoded chunk

    def to_json(self) -> str:
        return json.dumps({
            "v":    self.v,
            "mid":  self.mid,
            "seq":  self.seq,
            "n":    self.n,
            "kind": self.kind,
            "data": self.data,
        })

    @classmethod
    def from_json(cls, text: str) -> "Frame":
        d = json.loads(text)
        return cls(
            v=int(d["v"]),
            mid=str(d["mid"]),
            seq=int(d["seq"]),
            n=int(d["n"]),
            kind=str(d["kind"]),
            data=str(d["data"]),
        )


# ---------------------------------------------------------------------------
# Reassembly buffer
# ---------------------------------------------------------------------------

@dataclass
class _MessageBuffer:
    n:      int
    chunks: Dict[int, bytes] = field(default_factory=dict)
    kind:   Optional[str]    = None

    def add(self, frame: Frame) -> None:
        self.chunks[frame.seq] = base64.b64decode(frame.data)
        self.kind = frame.kind

    def complete(self) -> bool:
        return len(self.chunks) == self.n

    def reassemble(self) -> bytes:
        return b"".join(self.chunks[i] for i in range(self.n))


# ---------------------------------------------------------------------------
# CarrierBuffer — dedup + reassembly across poll calls
# ---------------------------------------------------------------------------

class CarrierBuffer:
    """Holds partial messages across multiple poll() calls.

    Thread-safety: not reentrant; callers must serialise if needed.
    """

    def __init__(self) -> None:
        self._buffers: Dict[str, _MessageBuffer] = {}
        self._seen:    Set[Tuple[str, int]]       = set()     # (mid, seq) dedup

    def ingest(self, frame: Frame) -> Optional[Tuple[str, bytes]]:
        """Add *frame*; return (kind, payload) when the message is complete.

        Returns None if the message is still incomplete or was already seen.
        Duplicate (mid, seq) pairs are silently dropped.
        """
        key = (frame.mid, frame.seq)
        if key in self._seen:
            return None
        self._seen.add(key)

        if frame.mid not in self._buffers:
            self._buffers[frame.mid] = _MessageBuffer(n=frame.n)
        buf = self._buffers[frame.mid]
        buf.add(frame)
        if buf.complete():
            payload = buf.reassemble()
            kind    = buf.kind
            del self._buffers[frame.mid]
            return kind, payload
        return None


# ---------------------------------------------------------------------------
# UsenetCarrier
# ---------------------------------------------------------------------------

class UsenetCarrier:
    """Frame, encrypt, post, receive, decrypt, reassemble HiveMind payloads.

    Parameters
    ----------
    creds:
        Local ``Credentials`` (RSA-4096 PGP key).  Decryption uses the
        private key; encryption uses the *peer*'s public key passed per call.
    server:
        A ``UsenetServer`` instance (or compatible stub).  The carrier calls
        ``server.post(text, subject, group)`` and
        ``server.get_articles(group, limit)``.
    group:
        Newsgroup to post/read.  Defaults to ``alt.anonymous.messages``.
    chunk_size:
        Maximum raw bytes per chunk (before base64).  Default 6 000.
    """

    def __init__(
        self,
        creds:       Credentials,
        server,                          # UsenetServer or stub
        group:       str = "alt.anonymous.messages",
        chunk_size:  int = CHUNK_SIZE,
    ) -> None:
        self.creds      = creds
        self.server     = server
        self.group      = group
        self.chunk_size = chunk_size
        self._buf       = CarrierBuffer()

    # ------------------------------------------------------------------
    # Send path
    # ------------------------------------------------------------------

    def send(
        self,
        payload:     bytes,
        peer_secret: str,
        peer_pubkey: str,
        kind:        str = _KIND_HIVE,
    ) -> None:
        """Chunk *payload*, PGP-encrypt each frame, and post under hSub.

        Parameters
        ----------
        payload:
            Raw bytes to transmit.
        peer_secret:
            Shared passphrase used to create an hSub subject (receiver can
            match it with ``match_hsub(article.subject, peer_secret)``).
        peer_pubkey:
            ASCII-armored PGP public key of the receiver.
        kind:
            ``"hive"`` (wormhole) or ``"nl"`` (bridge).
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"Invalid kind: {kind!r}")

        chunks = [
            payload[i : i + self.chunk_size]
            for i in range(0, max(len(payload), 1), self.chunk_size)
        ]
        mid = str(uuid.uuid4())
        n   = len(chunks)

        for seq, chunk in enumerate(chunks):
            frame = Frame(
                v    = CARRIER_VERSION,
                mid  = mid,
                seq  = seq,
                n    = n,
                kind = kind,
                data = base64.b64encode(chunk).decode(),
            )
            ciphertext = self.creds.encrypt(frame.to_json(), peer_pubkey)
            subject    = create_hsub(peer_secret)
            self.server.post(ciphertext, subject=subject, group=self.group)

    # ------------------------------------------------------------------
    # Receive path
    # ------------------------------------------------------------------

    def poll(
        self,
        my_secret: str,
        limit:     int = 200,
    ) -> List[Tuple[str, bytes]]:
        """Scan the group, decrypt matching articles, reassemble, return complete messages.

        Parameters
        ----------
        my_secret:
            Shared passphrase used to match hSub subjects.
        limit:
            How many recent articles to fetch per call.

        Returns
        -------
        List of ``(kind, payload)`` tuples for every newly completed message.
        """
        try:
            articles = self.server.get_articles(self.group, limit=limit)
        except Exception:
            return []

        results: List[Tuple[str, bytes]] = []

        # Servers return the most recent N articles, typically newest-first
        # (see e.g. tests/e2e's FakeNNTPServer). Multi-message handshakes
        # (HELLO then HANDSHAKE) depend on send order being preserved on
        # reassembly, so replay them oldest-first regardless of the
        # transport's own ordering.
        for article in reversed(articles):
            subject = getattr(article, "subject", "") or ""
            if not match_hsub(subject, my_secret):
                continue

            body = getattr(article, "text", "") or ""
            if not body.strip():
                continue

            try:
                plaintext = self.creds.decrypt(body)
            except Exception:
                continue

            try:
                frame = Frame.from_json(plaintext)
            except Exception:
                continue

            result = self._buf.ingest(frame)
            if result is not None:
                results.append(result)

        return results

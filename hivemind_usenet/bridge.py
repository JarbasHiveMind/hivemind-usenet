"""UsenetBridge — NL gateway between Usenet newsgroup posts and the HiveMind.

Polls a code-word hSub for kind="nl" (or plaintext) posts, injects the text
as an utterance into the hive via HiveMessageBusClient, awaits the spoken
reply, and sends the answer back under the reply hSub.

BRIDGE-1 conformance (hivemind-ovos-bridge-conformance §5):
- ``context.source`` is stamped per *peer* (each distinct sender address).
- ``context.session`` carries a per-peer ``session_id`` that never collides
  with ``"default"`` (SESSION-1 §3.1).
- Per-session FIFO: a ``collections.deque`` queue per session_id serialises
  processing; a dedicated worker thread drains each queue in order.
"""
import json
import queue
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ovos_utils.log import LOG

from hivemind_bus_client.client import HiveMessageBusClient
from hivemind_bus_client.message import HiveMessage, HiveMessageType

from remailers.keys import Credentials
from usenet import UsenetServer

from hivemind_usenet.carrier import UsenetCarrier, _KIND_NL


# ---------------------------------------------------------------------------
# Session/FIFO helpers
# ---------------------------------------------------------------------------

class _SessionQueue:
    """FIFO queue for one session.  Items are (text, peer_id, reply_callback)."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self, handler: Callable) -> None:
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._drain, args=(handler,), daemon=True
        )
        self._worker.start()

    def enqueue(self, item: Any) -> None:
        self._q.put(item)

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)  # unblock

    def _drain(self, handler: Callable) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                handler(item)
            except Exception as exc:
                LOG.exception("UsenetBridge: session handler error: %s", exc)


# ---------------------------------------------------------------------------
# UsenetBridge
# ---------------------------------------------------------------------------

class UsenetBridge(threading.Thread):
    """Natural-language bridge: Usenet post → hive utterance → Usenet reply.

    Parameters (all passed as constructor kwargs):
        creds          – Credentials for PGP; if None, plaintext-only mode.
        server         – UsenetServer instance.
        hive_host      – HiveMind server host.
        hive_port      – HiveMind server port (default 5678).
        hive_key       – API key for HiveMind connection.
        my_secret      – hSub passphrase for inbound (questions to us).
        reply_secret   – hSub passphrase for outbound (answers back).
        reply_pubkey   – PGP pubkey to encrypt answers; None = plaintext.
        group          – Newsgroup (default alt.anonymous.messages).
        poll_seconds   – Poll cadence (default 60).
        poll_limit     – Articles per poll (default 200).
        allowed_senders– Optional set of peer_ids to allowlist; None = all.
    """

    def __init__(
        self,
        creds:           Optional[Credentials],
        server,
        hive_host:       str = "127.0.0.1",
        hive_port:       int = 5678,
        hive_key:        str = "",
        my_secret:       str = "",
        reply_secret:    str = "",
        reply_pubkey:    Optional[str] = None,
        group:           str = "alt.anonymous.messages",
        poll_seconds:    int = 60,
        poll_limit:      int = 200,
        allowed_senders: Optional[set] = None,
        **kwargs,
    ) -> None:
        super().__init__(daemon=True, **kwargs)
        self.creds           = creds
        self.server          = server
        self.hive_host       = hive_host
        self.hive_port       = hive_port
        self.hive_key        = hive_key
        self.my_secret       = my_secret
        self.reply_secret    = reply_secret
        self.reply_pubkey    = reply_pubkey
        self.group           = group
        self.poll_seconds    = poll_seconds
        self.poll_limit      = poll_limit
        self.allowed_senders = allowed_senders

        self._carrier  = UsenetCarrier(creds, server, group=group) if creds else None
        self._stop     = threading.Event()

        # per-session FIFO queues: session_id → _SessionQueue
        self._sessions: Dict[str, _SessionQueue] = {}
        self._sessions_lock = threading.Lock()

        # HiveMind client (lazy)
        self._hm_client: Optional[HiveMessageBusClient] = None

    # ------------------------------------------------------------------
    # HiveMind client (lazy)
    # ------------------------------------------------------------------

    def _get_hm_client(self) -> HiveMessageBusClient:
        if self._hm_client is None:
            self._hm_client = HiveMessageBusClient(
                key=self.hive_key,
                host=self.hive_host,
                port=self.hive_port,
            )
            self._hm_client.connect()
        return self._hm_client

    # ------------------------------------------------------------------
    # Session helpers (BRIDGE-1 §5 FIFO)
    # ------------------------------------------------------------------

    def _session_id(self, peer_id: str) -> str:
        """Stable per-peer session id that never equals 'default'."""
        return f"usenet-bridge-{peer_id}"

    def _get_session_queue(self, session_id: str) -> _SessionQueue:
        with self._sessions_lock:
            if session_id not in self._sessions:
                sq = _SessionQueue()
                sq.start(self._process_item)
                self._sessions[session_id] = sq
            return self._sessions[session_id]

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def _process_item(self, item: Dict) -> None:
        text       = item["text"]
        peer_id    = item["peer_id"]
        session_id = item["session_id"]
        reply_cb   = item["reply_cb"]

        LOG.info("UsenetBridge: query from %s: %s", peer_id, text[:80])

        try:
            client = self._get_hm_client()
            response_text = client.ask(
                text,
                context={
                    "source":      peer_id,
                    "destination": peer_id,
                    "session_id":  session_id,
                },
            )
        except Exception as exc:
            LOG.exception("UsenetBridge: hive query error: %s", exc)
            response_text = "Error processing request."

        try:
            reply_cb(response_text)
        except Exception as exc:
            LOG.exception("UsenetBridge: reply error: %s", exc)

    def _make_reply_cb(self, peer_id: str) -> Callable:
        """Return a callback that posts the answer back to the peer."""

        def _cb(text: str) -> None:
            if self._carrier and self.reply_pubkey:
                self._carrier.send(
                    text.encode(),
                    peer_secret = self.reply_secret,
                    peer_pubkey = self.reply_pubkey,
                    kind        = _KIND_NL,
                )
            elif self.server:
                subject = self.reply_secret or "hivemind-reply"
                self.server.post(text, subject=subject, group=self.group)

        return _cb

    # ------------------------------------------------------------------
    # Inbound polling
    # ------------------------------------------------------------------

    def _handle_nl_payload(self, payload: bytes, peer_id: str = "anonymous") -> None:
        """Decode a raw NL payload and push into the per-session FIFO queue."""
        try:
            text = payload.decode("utf-8")
        except Exception:
            return

        text = text.strip()
        if not text:
            return

        if self.allowed_senders and peer_id not in self.allowed_senders:
            LOG.debug("UsenetBridge: ignoring sender %s (not in allowlist)", peer_id)
            return

        session_id = self._session_id(peer_id)
        sq         = self._get_session_queue(session_id)
        sq.enqueue({
            "text":       text,
            "peer_id":    peer_id,
            "session_id": session_id,
            "reply_cb":   self._make_reply_cb(peer_id),
        })

    def _poll_once(self) -> None:
        if self._carrier:
            # PGP-encrypted path via carrier
            messages = self._carrier.poll(self.my_secret, limit=self.poll_limit)
            for kind, payload in messages:
                if kind == _KIND_NL:
                    self._handle_nl_payload(payload)
        else:
            # Plaintext path — scan newsgroup directly
            from remailers import match_hsub
            articles = self.server.get_articles(self.group, limit=self.poll_limit)
            for article in articles:
                subject = getattr(article, "subject", "") or ""
                if not match_hsub(subject, self.my_secret):
                    if subject != self.my_secret:
                        continue
                body = getattr(article, "text", "") or ""
                if body.strip():
                    self._handle_nl_payload(body.encode())

    # ------------------------------------------------------------------
    # Thread entry-point
    # ------------------------------------------------------------------

    def run(self) -> None:
        LOG.info("UsenetBridge: starting poll loop (interval=%ds)", self.poll_seconds)
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                LOG.exception("UsenetBridge: poll error: %s", exc)
            self._stop.wait(self.poll_seconds)
        LOG.info("UsenetBridge: stopped")

    def stop(self) -> None:
        self._stop.set()
        with self._sessions_lock:
            for sq in self._sessions.values():
                sq.stop()


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HiveMind Usenet NL Bridge")
    parser.add_argument("--my-secret",    required=True)
    parser.add_argument("--reply-secret", default="")
    parser.add_argument("--reply-pubkey", default=None,
                        help="Path to ASCII-armored peer public key file")
    parser.add_argument("--key-path",     default=None)
    parser.add_argument("--server-url",   default="paganini.bofh.team")
    parser.add_argument("--group",        default="alt.anonymous.messages")
    parser.add_argument("--hive-host",    default="127.0.0.1")
    parser.add_argument("--hive-port",    type=int, default=5678)
    parser.add_argument("--hive-key",     default="")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()

    creds = Credentials(args.key_path) if args.key_path else None
    server = UsenetServer(args.server_url)

    reply_pubkey = None
    if args.reply_pubkey:
        with open(args.reply_pubkey) as fh:
            reply_pubkey = fh.read()

    bridge = UsenetBridge(
        creds          = creds,
        server         = server,
        hive_host      = args.hive_host,
        hive_port      = args.hive_port,
        hive_key       = args.hive_key,
        my_secret      = args.my_secret,
        reply_secret   = args.reply_secret,
        reply_pubkey   = reply_pubkey,
        group          = args.group,
        poll_seconds   = args.poll_seconds,
    )
    bridge.start()
    bridge.join()

"""HiveMindUsenetClient — satellite-side HiveMind transport over Usenet.

Symmetric peer to UsenetWormhole (hub side).  Mirrors the API surface of
HiveMindHTTPClient so it can serve as a drop-in satellite transport:
same connect/emit/run/close, same HiveMindSlaveProtocol usage.

Crypto layering
---------------
The UsenetCarrier handles the TRANSPORT layer (PGP+hSub per article — the
analogue of TLS for a websocket).  The HiveMind handshake + AES session key
negotiation rides on top, unchanged, exactly as it does for the HTTP/WS
transports.  The client sends HELLO/HANDSHAKE as normal HiveMessages; they
are serialised, chunked, PGP-encrypted, and posted by the carrier.

Latency note
------------
Usenet is poll-based.  Each poll cycle may take 30 s or more depending on
server load and ``poll_seconds``.  The HiveMind handshake therefore spans
several poll cycles before the AES session key is established.  This is an
async/covert link, not a real-time channel.  Timeouts are intentionally
generous; the ``_stop_event`` is honoured throughout so the thread exits
cleanly.

Config keys (passed as ``config`` dict or keyword arguments to __init__):
    my_secret    (str)  – hSub passphrase the hub posts with (we read these)
    hub_secret   (str)  – hSub passphrase we post with (hub reads these)
    hub_pubkey   (str)  – ASCII-armored PGP public key of the hub
    key_path     (str)  – path for local Credentials auto-generation
    server_url   (str)  – NNTP server hostname  (default: paganini.bofh.team)
    group        (str)  – newsgroup              (default: alt.anonymous.messages)
    poll_seconds (int)  – poll cadence           (default: 30)
    poll_limit   (int)  – articles per poll      (default: 200)

Symmetry with UsenetWormhole
-----------------------------
    client.my_secret  == wormhole.peer_secret   (hub posts with this)
    client.hub_secret == wormhole.my_secret     (hub reads with this... wait)

Actually the secret naming follows the carrier convention:
    carrier.send(payload, peer_secret=X, ...)  → posts with hSub(X)
    carrier.poll(my_secret=Y, ...)             → reads articles with hSub(Y)

So:
    client posts  → peer_secret = hub_secret  (hub polls with hub_secret)
    client polls  → my_secret   = my_secret   (hub posts under my_secret)
    wormhole posts → peer_secret = my_secret  (client polls with my_secret ✓)
    wormhole polls → my_secret  = hub_secret  (client posts under hub_secret ✓)
"""
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union

from ovos_bus_client import Message as MycroftMessage, MessageBusClient as OVOSBusClient
from ovos_bus_client.session import Session
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG

from hivemind_bus_client.identity import NodeIdentity
from hivemind_bus_client.message import HiveMessage, HiveMessageType
from hivemind_bus_client.protocol import HiveMindSlaveProtocol

from remailers.keys import Credentials
from usenet import UsenetServer

from hivemind_usenet.carrier import UsenetCarrier, _KIND_HIVE

_USERAGENT = "HiveMindUsenetClientV1.0"


class HiveMindUsenetClient(threading.Thread):
    """Satellite-side HiveMind transport over Usenet.

    Drop-in replacement for HiveMindHTTPClient with a UsenetCarrier transport.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        # convenience keyword equivalents of config keys
        my_secret: Optional[str] = None,
        hub_secret: Optional[str] = None,
        hub_pubkey: Optional[str] = None,
        key_path: Optional[str] = None,
        server_url: Optional[str] = None,
        group: Optional[str] = None,
        poll_seconds: Optional[int] = None,
        poll_limit: Optional[int] = None,
        # HiveMind identity / bus options
        identity: Optional[NodeIdentity] = None,
        useragent: str = _USERAGENT,
        share_bus: bool = False,
        internal_bus: Optional[OVOSBusClient] = None,
        # injectable carrier/server (for testing)
        _carrier: Optional[UsenetCarrier] = None,
    ) -> None:
        super().__init__(daemon=True)

        # Build the merged config dict (kwargs win over config dict).
        cfg: Dict[str, Any] = dict(config or {})
        if my_secret   is not None: cfg["my_secret"]    = my_secret
        if hub_secret  is not None: cfg["hub_secret"]   = hub_secret
        if hub_pubkey  is not None: cfg["hub_pubkey"]   = hub_pubkey
        if key_path    is not None: cfg["key_path"]     = key_path
        if server_url  is not None: cfg["server_url"]   = server_url
        if group       is not None: cfg["group"]        = group
        if poll_seconds is not None: cfg["poll_seconds"] = poll_seconds
        if poll_limit  is not None: cfg["poll_limit"]   = poll_limit
        self.config: Dict[str, Any] = cfg

        # Identity / session
        self.identity: NodeIdentity = identity or NodeIdentity()
        self.identity.name = self.identity.name or useragent
        self.share_bus = share_bus

        sess = Session()
        if internal_bus:
            self.internal_bus: Union[OVOSBusClient, FakeBus] = internal_bus
        else:
            self.internal_bus = FakeBus(session=sess)
        self.session_id = sess.session_id

        # HiveMind slave protocol (set by connect())
        self.protocol: Optional[HiveMindSlaveProtocol] = None
        # crypto key negotiated during handshake (set by SlaveProtocol)
        self.crypto_key: Optional[str] = None

        # Transport carrier (built in connect() or injected for tests)
        self._carrier: Optional[UsenetCarrier] = _carrier

        # Threading events
        self.stopped       = threading.Event()
        self.connected     = threading.Event()
        self.handshake_event = threading.Event()
        self._stop_event   = threading.Event()

        # Message/event handlers (mirrors HTTPClient)
        self._handlers: Dict[str, List[Callable[[HiveMessage], None]]] = {}
        self._agent_handlers: Dict[str, List[Callable[[MycroftMessage], None]]] = {}

        LOG.info("HiveMindUsenetClient: session_id=%s", self.session_id)

    # ------------------------------------------------------------------
    # Properties mirroring HiveMindHTTPClient
    # ------------------------------------------------------------------

    @property
    def useragent(self) -> str:
        return self.identity.name or _USERAGENT

    @useragent.setter
    def useragent(self, val: str) -> None:
        self.identity.name = val

    @property
    def key(self) -> Optional[str]:
        return self.identity.access_key

    @key.setter
    def key(self, val: str) -> None:
        self.identity.access_key = val

    @property
    def password(self) -> Optional[str]:
        return self.identity.password

    @password.setter
    def password(self, val: str) -> None:
        self.identity.password = val

    @property
    def site_id(self) -> str:
        return self.identity.site_id or "unknown"

    @site_id.setter
    def site_id(self, val: str) -> None:
        self.identity.site_id = val

    # ------------------------------------------------------------------
    # Carrier construction
    # ------------------------------------------------------------------

    def _build_carrier(self) -> UsenetCarrier:
        key_path   = self.config.get("key_path", "/tmp/hivemind_usenet_client.asc")
        server_url = self.config.get("server_url", "paganini.bofh.team")
        group      = self.config.get("group", "alt.anonymous.messages")
        creds  = Credentials(key_path)
        server = UsenetServer(server_url)
        return UsenetCarrier(creds, server, group=group)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(
        self,
        bus: Union[OVOSBusClient, FakeBus, None] = None,
        protocol: Optional[HiveMindSlaveProtocol] = None,
        site_id: Optional[str] = None,
    ) -> None:
        """Initialise the slave protocol and kick off the handshake.

        The actual HELLO/HANDSHAKE exchange happens asynchronously over
        subsequent poll cycles; call ``wait_for_handshake()`` to block until
        the session key is established.
        """
        if site_id:
            self.identity.site_id = site_id

        if bus is None:
            bus = self.internal_bus

        if protocol is None:
            LOG.debug("HiveMindUsenetClient: initialising HiveMindSlaveProtocol")
            self.protocol = HiveMindSlaveProtocol(
                self,
                shared_bus=self.share_bus,
                site_id=self.identity.site_id or "unknown",
                identity=self.identity,
            )
        else:
            self.protocol = protocol
            self.protocol.identity = self.identity
            if self.identity.site_id:
                self.protocol.site_id = self.identity.site_id

        self.protocol.bind(bus)

        if self._carrier is None:
            self._carrier = self._build_carrier()

        self.connected.set()
        LOG.info("HiveMindUsenetClient: connected (awaiting handshake over Usenet)")

        # Trigger the outbound HANDSHAKE message.  The hub will respond with
        # HELLO; we reply with HELLO; then the AES session key is established.
        # This is fire-and-forget — the round-trip takes poll_seconds*N cycles.
        self.protocol.start_handshake()

    def wait_for_handshake(self, timeout: float = 300.0) -> bool:
        """Block until the HiveMind session key is negotiated.

        Usenet latency means this can take several minutes.  Returns True when
        the handshake succeeds, False on timeout.
        """
        return self.handshake_event.wait(timeout=timeout)

    def emit(self, message: Union[MycroftMessage, HiveMessage]) -> None:
        """Send a HiveMessage (or MycroftMessage wrapped in BUS) to the hub."""
        if not self.connected.is_set():
            raise ConnectionAbortedError("call connect() first")

        if isinstance(message, MycroftMessage):
            ctxt = dict(message.context)
            ctxt.setdefault("source", self.useragent)
            ctxt.setdefault("platform", self.useragent)
            ctxt.setdefault("destination", "HiveMind")
            ctxt.setdefault("session", {})
            ctxt["session"]["session_id"] = self.session_id
            ctxt["session"]["site_id"]    = self.site_id
            message.context = ctxt
            message = HiveMessage(msg_type=HiveMessageType.BUS, payload=message)

        elif message.msg_type == HiveMessageType.BUS:
            ctxt = dict(message.payload.context)
            ctxt.setdefault("source", self.useragent)
            ctxt.setdefault("platform", self.useragent)
            ctxt.setdefault("destination", "HiveMind")
            ctxt.setdefault("session", {})
            ctxt["session"]["session_id"] = self.session_id
            ctxt["session"]["site_id"]    = self.site_id
            message.payload.context = ctxt

        LOG.debug("HiveMindUsenetClient: emitting %s", message.msg_type)

        hub_secret = self.config.get("hub_secret", "")
        hub_pubkey = self.config.get("hub_pubkey", "")

        raw = message.serialize().encode()
        self._carrier.send(raw, peer_secret=hub_secret,
                           peer_pubkey=hub_pubkey, kind=_KIND_HIVE)

    # ------------------------------------------------------------------
    # on() / on_mycroft() / remove() helpers
    # ------------------------------------------------------------------

    def on(self, event_name: str, func: Callable) -> None:
        self._handlers.setdefault(event_name, []).append(func)

    def on_mycroft(self, event_name: str, func: Callable) -> None:
        self._agent_handlers.setdefault(event_name, []).append(func)

    def remove(self, event_name: str, func: Callable) -> None:
        if event_name in self._handlers:
            self._handlers[event_name] = [h for h in self._handlers[event_name] if h is not func]

    def remove_mycroft(self, event_name: str, func: Callable) -> None:
        if event_name in self._agent_handlers:
            self._agent_handlers[event_name] = [h for h in self._agent_handlers[event_name] if h is not func]

    # ------------------------------------------------------------------
    # Inbound dispatch — identical to HiveMindHTTPClient._handle_hive_protocol
    # ------------------------------------------------------------------

    def _handle_hive_protocol(self, message: HiveMessage) -> None:
        LOG.debug("HiveMindUsenetClient: inbound %s", message.msg_type)

        if message.msg_type == HiveMessageType.HELLO:
            self.protocol.handle_hello(message)
        if message.msg_type == HiveMessageType.HANDSHAKE:
            self.protocol.handle_handshake(message)
        if message.msg_type == HiveMessageType.BUS:
            self.protocol.handle_bus(message)
        if message.msg_type == HiveMessageType.BROADCAST:
            self.protocol.handle_broadcast(message)
        if message.msg_type == HiveMessageType.PROPAGATE:
            self.protocol.handle_propagate(message)
        if message.msg_type == HiveMessageType.INTERCOM:
            self.protocol.handle_intercom(message)
        if message.msg_type == HiveMessageType.ESCALATE:
            self.protocol.handle_illegal_msg(message)
        if message.msg_type == HiveMessageType.SHARED_BUS:
            self.protocol.handle_illegal_msg(message)

        for handler in self._handlers.get(message.msg_type, []):
            try:
                handler(message)
            except Exception as exc:
                LOG.error("HiveMindUsenetClient: handler error: %s", exc)

        if message.msg_type == HiveMessageType.BUS:
            for handler in self._agent_handlers.get(message.payload.msg_type, []):
                try:
                    handler(message.payload)
                except Exception as exc:
                    LOG.error("HiveMindUsenetClient: agent handler error: %s", exc)

    def _dispatch(self, payload: bytes) -> None:
        """Deserialise a raw payload and feed it to _handle_hive_protocol."""
        try:
            msg = HiveMessage.deserialize(payload.decode())
        except Exception as exc:
            LOG.warning("HiveMindUsenetClient: deserialize failed: %s", exc)
            return
        if self.protocol is None:
            LOG.warning("HiveMindUsenetClient: no protocol; dropping message")
            return
        self._handle_hive_protocol(msg)

    # ------------------------------------------------------------------
    # Main poll loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Poll Usenet for inbound HiveMessages until stopped."""
        self.stopped.clear()
        self._stop_event.clear()
        self.connected.wait()  # wait until connect() has been called

        my_secret    = self.config.get("my_secret", "")
        poll_seconds = int(self.config.get("poll_seconds", 30))
        poll_limit   = int(self.config.get("poll_limit", 200))

        LOG.info("HiveMindUsenetClient: poll loop started (interval=%ds)", poll_seconds)
        while not self._stop_event.is_set():
            try:
                messages = self._carrier.poll(my_secret, limit=poll_limit)
                for kind, payload in messages:
                    if kind != _KIND_HIVE:
                        continue
                    self._dispatch(payload)
            except Exception as exc:
                LOG.exception("HiveMindUsenetClient: poll error: %s", exc)

            self._stop_event.wait(poll_seconds)

        self.stopped.set()
        LOG.info("HiveMindUsenetClient: stopped")

    def close(self) -> None:
        """Stop the poll loop."""
        self._stop_event.set()
        self.connected.clear()
        self.handshake_event.clear()

    # alias for symmetry with HiveMindHTTPClient.shutdown()
    shutdown = close


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="HiveMind Usenet Client (satellite)")
    parser.add_argument("--config",       default=None, help="Path to JSON config file")
    parser.add_argument("--my-secret",    default=None)
    parser.add_argument("--hub-secret",   default=None)
    parser.add_argument("--hub-pubkey",   default=None, help="Path to ASCII-armored hub pubkey file")
    parser.add_argument("--key-path",     default=None)
    parser.add_argument("--server-url",   default="paganini.bofh.team")
    parser.add_argument("--group",        default="alt.anonymous.messages")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()

    cfg: Dict[str, Any] = {}
    if args.config:
        with open(args.config) as fh:
            cfg = _json.load(fh)

    if args.my_secret:    cfg["my_secret"]    = args.my_secret
    if args.hub_secret:   cfg["hub_secret"]   = args.hub_secret
    if args.key_path:     cfg["key_path"]      = args.key_path
    if args.server_url:   cfg["server_url"]    = args.server_url
    if args.group:        cfg["group"]          = args.group
    cfg["poll_seconds"] = args.poll_seconds

    if args.hub_pubkey:
        with open(args.hub_pubkey) as fh:
            cfg["hub_pubkey"] = fh.read()

    client = HiveMindUsenetClient(config=cfg)
    client.connect()
    client.run()

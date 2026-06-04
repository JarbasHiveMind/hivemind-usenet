"""UsenetWormhole — NetworkProtocol that tunnels HiveMessage objects over Usenet.

Each instance manages ONE point-to-point link:

    local-node  <---->  UsenetCarrier  <---->  peer-node
                         (hSub+PGP)

Inbound frames with kind="hive" are deserialized and routed to
``hm_protocol.handle_message(msg, client)``.  The matching
``HiveMindClientConnection.send_msg`` callback posts outbound frames back via
the carrier.

Config keys (all passed in ``self.config``):
    my_secret   (str)  – hSub passphrase we read with
    peer_secret (str)  – hSub passphrase we post with (peer reads this)
    peer_pubkey (str)  – ASCII-armored PGP public key of the peer
    key_path    (str)  – path for local Credentials auto-generation
    server_url  (str)  – NNTP server hostname
    group       (str)  – newsgroup  (default: alt.anonymous.messages)
    poll_seconds(int)  – poll cadence          (default: 30)
    poll_limit  (int)  – articles per poll     (default: 200)
"""
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ovos_utils.log import LOG

from hivemind_bus_client.message import HiveMessage
from hivemind_plugin_manager.protocols import NetworkProtocol
from hivemind_core.protocol import HiveMindClientConnection

from remailers.keys import Credentials
from usenet import UsenetServer

from hivemind_usenet.carrier import UsenetCarrier, _KIND_HIVE


# ---------------------------------------------------------------------------
# Synthetic HiveMindClientConnection for this peer
# ---------------------------------------------------------------------------

def _make_client_connection(peer_id: str, carrier: UsenetCarrier,
                             peer_secret: str, peer_pubkey: str,
                             hm_protocol=None) -> HiveMindClientConnection:
    """Build a minimal HiveMindClientConnection whose send_msg posts via carrier."""

    def _send(msg: HiveMessage) -> None:
        data = msg.serialize().encode()
        carrier.send(data, peer_secret=peer_secret,
                     peer_pubkey=peer_pubkey, kind=_KIND_HIVE)

    def _disconnect() -> None:
        LOG.debug(f"UsenetWormhole: peer disconnected: {peer_id}")

    return HiveMindClientConnection(
        key         = peer_id,
        send_msg    = _send,
        disconnect  = _disconnect,
        hm_protocol = hm_protocol,
    )


# ---------------------------------------------------------------------------
# UsenetWormhole
# ---------------------------------------------------------------------------

@dataclass
class UsenetWormhole(NetworkProtocol):
    """NetworkProtocol that transports full HiveMessage objects over Usenet.

    Entry-point: ``hivemind.network.protocol``
    Name:        ``hivemind-usenet-wormhole``
    """

    config:      Dict[str, Any] = field(default_factory=dict)
    _carrier:    Optional[UsenetCarrier]            = field(default=None, init=False, repr=False)
    _client_conn:Optional[HiveMindClientConnection] = field(default=None, init=False, repr=False)
    _stop_event: threading.Event                    = field(default_factory=threading.Event, init=False, repr=False)

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _build_carrier(self) -> UsenetCarrier:
        key_path   = self.config.get("key_path", "/tmp/hivemind_usenet_wormhole.asc")
        server_url = self.config.get("server_url", "paganini.bofh.team")
        group      = self.config.get("group", "alt.anonymous.messages")

        creds  = Credentials(key_path)
        server = UsenetServer(server_url)
        return UsenetCarrier(creds, server, group=group)

    def _ensure_client(self, carrier: UsenetCarrier) -> HiveMindClientConnection:
        if self._client_conn is not None:
            return self._client_conn
        peer_id     = self.config.get("peer_id", "usenet-peer")
        peer_secret = self.config["peer_secret"]
        peer_pubkey = self.config["peer_pubkey"]
        self._client_conn = _make_client_connection(
            peer_id, carrier, peer_secret, peer_pubkey,
            hm_protocol=self.hm_protocol,
        )
        if self.hm_protocol:
            self.callbacks.on_connect(self._client_conn)
        return self._client_conn

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Block, poll Usenet, dispatch inbound HiveMessages."""
        poll_seconds = int(self.config.get("poll_seconds", 30))
        poll_limit   = int(self.config.get("poll_limit", 200))
        my_secret    = self.config["my_secret"]

        self._carrier = self._build_carrier()
        self._stop_event.clear()

        LOG.info("UsenetWormhole: starting poll loop (interval=%ds)", poll_seconds)
        while not self._stop_event.is_set():
            try:
                messages = self._carrier.poll(my_secret, limit=poll_limit)
                for kind, payload in messages:
                    if kind != _KIND_HIVE:
                        continue
                    self._dispatch(payload)
            except Exception as exc:
                LOG.exception("UsenetWormhole: poll error: %s", exc)

            self._stop_event.wait(poll_seconds)

        LOG.info("UsenetWormhole: stopped")

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, payload: bytes) -> None:
        try:
            msg = HiveMessage.deserialize(payload.decode())
        except Exception as exc:
            LOG.warning("UsenetWormhole: failed to deserialize HiveMessage: %s", exc)
            return

        if not self.hm_protocol:
            LOG.warning("UsenetWormhole: no hm_protocol; dropping message")
            return

        client = self._ensure_client(self._carrier)
        try:
            self.hm_protocol.handle_message(msg, client)
        except Exception as exc:
            LOG.exception("UsenetWormhole: handle_message error: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse, json as _json

    parser = argparse.ArgumentParser(description="HiveMind Usenet Wormhole")
    parser.add_argument("--config", default=None,
                        help="Path to JSON config file")
    parser.add_argument("--my-secret",   default=None)
    parser.add_argument("--peer-secret", default=None)
    parser.add_argument("--peer-pubkey", default=None,
                        help="Path to ASCII-armored peer public key file")
    parser.add_argument("--key-path",    default=None)
    parser.add_argument("--server-url",  default="paganini.bofh.team")
    parser.add_argument("--group",       default="alt.anonymous.messages")
    parser.add_argument("--poll-seconds",type=int, default=30)
    args = parser.parse_args()

    cfg: Dict[str, Any] = {}
    if args.config:
        with open(args.config) as fh:
            cfg = _json.load(fh)

    if args.my_secret:   cfg["my_secret"]    = args.my_secret
    if args.peer_secret: cfg["peer_secret"]  = args.peer_secret
    if args.key_path:    cfg["key_path"]     = args.key_path
    if args.server_url:  cfg["server_url"]   = args.server_url
    if args.group:       cfg["group"]        = args.group
    cfg["poll_seconds"] = args.poll_seconds

    if args.peer_pubkey:
        with open(args.peer_pubkey) as fh:
            cfg["peer_pubkey"] = fh.read()

    wormhole = UsenetWormhole(config=cfg)
    wormhole.run()

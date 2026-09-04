"""In-process Usenet-carrier link harness for end-to-end tests.

This module wires a *real* hivemind-core master (``HiveMindListenerProtocol``,
via hivescope's :class:`~hivescope.node.MasterNode`) to a *real* satellite
(``HiveMindSlaveProtocol`` driven through hivescope's ``InProcessHiveShim``),
with the wire between them carried by the *real* :class:`UsenetCarrier`
(chunking + PGP encryption + hSub addressing + reassembly) over an in-memory
fake NNTP server.

Nothing here is mocked except the NNTP *transport* itself:

* The HiveMind RSA/password handshake and AES session crypto are the real
  hivemind-core code paths.
* The PGP carrier crypto is real ``remailers.keys.Credentials`` (RSA-4096).
* The hSub addressing is real ``remailers.create_hsub`` / ``match_hsub``.
* Only :class:`FakeNNTPServer` stands in for a live news server — there is no
  socket, no network, and no real ``usenet.UsenetServer``.

The result is a deterministic, network-free reproduction of the full
store-and-forward link: a satellite posts encrypted, chunked articles to a
newsgroup; the master polls, decrypts and reassembles them; and vice versa.
"""
import os
import tempfile
import threading
from typing import List, Optional, Union

from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovos_utils.fakebus import FakeBus

from hivemind_bus_client.message import HiveMessage, HiveMessageType
from hivemind_bus_client.protocol import HiveMindSlaveProtocol
from hivemind_core.protocol import HiveMindClientConnection, HiveMindNodeType

from poorman_handshake import PasswordHandShake

from remailers.keys import Credentials

from hivescope.node import InProcessHiveShim, MasterNode
from hivescope.utils import make_identity

from hivemind_usenet.carrier import UsenetCarrier, _KIND_HIVE


# ---------------------------------------------------------------------------
# Fake NNTP server (the ONLY mock — no socket, no network)
# ---------------------------------------------------------------------------

class _Article:
    """The two attributes UsenetCarrier reads off an article."""

    def __init__(self, subject: str, text: str) -> None:
        self.subject = subject
        self.text = text


class FakeNNTPServer:
    """In-memory stand-in for ``usenet.UsenetServer``.

    Stores posted articles in a list; ``get_articles`` returns them
    newest-first, exactly like the real server's most-recent-N semantics.
    Shared by both carriers so a post by one peer is visible to the other's
    poll — modelling a single newsgroup that both nodes read and write.
    """

    def __init__(self) -> None:
        self._articles: List[_Article] = []
        self.post_count = 0

    def post(self, text: str, subject: str, group: str, **_) -> None:
        self._articles.append(_Article(subject=subject, text=text))
        self.post_count += 1

    def get_articles(self, group: str, limit: int = 200) -> List[_Article]:
        return list(reversed(self._articles[-limit:]))


# ---------------------------------------------------------------------------
# UsenetLink — master <-> satellite over the carrier
# ---------------------------------------------------------------------------

# hSub passphrases for the two directions of the link.
_HUB_TO_CLIENT = "usenet-e2e-hub-to-client"
_CLIENT_TO_HUB = "usenet-e2e-client-to-hub"


class UsenetLink:
    """A started master+satellite pair bridged by a real UsenetCarrier.

    Use :meth:`pump` to advance the store-and-forward link until quiescent,
    :meth:`handshake` to complete the HiveMind handshake, and the recorded
    ``master`` / ``slave`` objects to assert on protocol state.
    """

    def __init__(self, password_handshake: bool = True,
                 allowed_types: Optional[List[str]] = None) -> None:
        self._tmp = tempfile.mkdtemp(prefix="hivemind_usenet_e2e_")
        self.server = FakeNNTPServer()

        # Real PGP identities for the carrier (transport-layer crypto).
        self.hub_creds = Credentials(os.path.join(self._tmp, "hub.asc"))
        self.client_creds = Credentials(os.path.join(self._tmp, "client.asc"))

        self.hub_carrier = UsenetCarrier(self.hub_creds, self.server)
        self.client_carrier = UsenetCarrier(self.client_creds, self.server)

        # Real hivemind-core master.
        self.master = MasterNode.create(
            "M0", require_crypto=True, handshake_enabled=True
        )

        # Real satellite slave protocol via hivescope's in-process shim.
        # make_identity always generates a password; for the RSA handshake we
        # must clear it so the slave uses pubkey (not password) negotiation.
        self.sat_identity = make_identity("S0", site_id="S0-site")
        if not password_handshake:
            self.sat_identity.password = None
        self.bus = FakeBus()
        self.shim = InProcessHiveShim(identity=self.sat_identity, satellite_ref=None)
        self.slave = HiveMindSlaveProtocol(
            hm=self.shim,
            identity=self.sat_identity,
            shared_bus=False,
            site_id="S0-site",
        )
        self.slave.bind(self.bus)

        # The satellite's upstream emit() must post over the client carrier.
        self.shim.emit = self._satellite_emit  # type: ignore[assignment]

        # Register the satellite in the master DB so it is admitted. The
        # allowed_types whitelist is what the master's ACL policy lets the
        # satellite inject onto the agent bus (deny-by-default).
        self._password = self.sat_identity.password if password_handshake else None
        self.master.register_satellite(
            key=self.sat_identity.access_key,
            password=self._password,
            allowed_types=allowed_types,
        )

        self.conn: Optional[HiveMindClientConnection] = None

    # ------------------------------------------------------------------
    # Wire callbacks
    # ------------------------------------------------------------------

    def _master_send(self, payload: Union[str, bytes], is_binary: bool) -> None:
        """Master downstream: post the wire payload over the hub carrier."""
        raw = payload.encode() if isinstance(payload, str) else payload
        self.hub_carrier.send(
            raw,
            peer_secret=_HUB_TO_CLIENT,
            peer_pubkey=self.client_creds.pubkey,
            kind=_KIND_HIVE,
        )

    def _satellite_emit(self, message: Union[HiveMessage, Message]) -> None:
        """Satellite upstream: serialise + post the HiveMessage over the client carrier.

        Mirrors ``InProcessHiveShim.emit`` but routes onto the carrier instead
        of straight into the master. The master's ``HiveMindClientConnection``
        will decode it on the next :meth:`pump`.
        """
        if isinstance(message, Message):
            message = HiveMessage(HiveMessageType.BUS, payload=message)
        # The slave protocol hands us a fully-formed HiveMessage; whether it is
        # encrypted is the slave's concern (it encrypts once crypto_key is set).
        raw = message.serialize().encode()
        self.client_carrier.send(
            raw,
            peer_secret=_CLIENT_TO_HUB,
            peer_pubkey=self.hub_creds.pubkey,
            kind=_KIND_HIVE,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Admit the satellite at the master; this posts HELLO+HANDSHAKE.

        The master's ``handle_new_client`` synchronously emits HELLO and (RSA)
        HANDSHAKE downstream — i.e. it *posts them onto the carrier*. They are
        delivered to the satellite on the next :meth:`pump`.
        """
        self.conn = HiveMindClientConnection(
            key=self.sat_identity.access_key,
            name=self.sat_identity.name,
            send_msg=self._master_send,
            disconnect=lambda code=None, reason=None: None,
            sess=Session(session_id="default"),
            hm_protocol=self.master.hm_protocol,
            pswd_handshake=(
                PasswordHandShake(self._password) if self._password else None
            ),
            node_type=HiveMindNodeType.NODE,
        )
        self.master.hm_protocol.handle_new_client(self.conn)

    def _pump_to_satellite(self) -> int:
        """Deliver hub->client articles to the slave protocol. Returns count.

        Articles are encrypted to the *client's* PGP key, so the *client*
        carrier (which holds the client private key) is the one that polls,
        decrypts, and reassembles them.
        """
        delivered = 0
        for kind, payload in self.client_carrier.poll(_HUB_TO_CLIENT):
            if kind != _KIND_HIVE:
                continue
            if getattr(self.shim, "noise_transport", None) is not None:
                # A v3 Noise session is encrypted with reciprocal (not
                # shared) CipherStates per direction. This frame was
                # encrypted by the *master's* noise_transport (its send
                # key); decrypting it must go through the *satellite's*
                # noise_transport (its matching receive key) -- reusing
                # self.conn.decode() here would decrypt with the master's
                # own object and fail AEAD (wrong direction).
                plaintext = self.shim.noise_transport.decrypt_frame(payload)
                if isinstance(plaintext, bytes):
                    plaintext = plaintext.decode()
                message = HiveMessage.deserialize(plaintext)
            else:
                message = self.conn.decode(payload.decode())
            self.shim.emitter.emit(message.msg_type, message)
            delivered += 1
        return delivered

    def _pump_to_master(self) -> int:
        """Deliver client->hub articles to the master protocol. Returns count.

        Articles are encrypted to the *hub's* PGP key, so the *hub* carrier
        polls and decrypts them.
        """
        delivered = 0
        for kind, payload in self.hub_carrier.poll(_CLIENT_TO_HUB):
            if kind != _KIND_HIVE:
                continue
            message = HiveMessage.deserialize(payload.decode())
            self.master.hm_protocol.handle_message(message, self.conn)
            delivered += 1
        return delivered

    def pump(self, rounds: int = 8) -> None:
        """Advance the link until no articles move (or ``rounds`` exhausted).

        Each round drains both directions once. Because handshake replies and
        bus responses are generated *inside* the protocol handlers (which post
        new articles), several rounds are needed for a full exchange to settle.
        """
        for _ in range(rounds):
            moved = self._pump_to_satellite()
            moved += self._pump_to_master()
            if moved == 0:
                break

    def handshake(self) -> None:
        """Complete the HiveMind handshake over the carrier."""
        self.connect()
        # Deliver master's HELLO/HANDSHAKE, let the slave reply, deliver the
        # reply back, etc. For RSA mode the slave kicks off start_handshake on
        # HELLO; for password mode the master completes it on the reply.
        self.pump()
        if not self.shim.handshake_event.is_set():
            # Mirror a real client's wait_for_handshake fallback.
            self.slave.start_handshake()
            self.pump()

    def emit_from_satellite(self, message: Union[HiveMessage, Message]) -> None:
        """Send a message upstream from the satellite and pump it to the master."""
        self.slave.hm.emit(message)
        self.pump()

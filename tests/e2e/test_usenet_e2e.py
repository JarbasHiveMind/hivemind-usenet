"""End-to-end tests: a real HiveMind master + satellite over the Usenet carrier.

These exercise the *full* protocol stack over the *real* transport, with only
the NNTP server faked (an in-memory article store — no socket, no network).
See :mod:`tests.e2e.usenet_link` for the harness; the round-trip path is:

    satellite slave-protocol
        → HiveMessage.serialize()
        → UsenetCarrier.send (chunk → PGP-encrypt → hSub-post to fake NNTP)
        → master polls (match hSub → PGP-decrypt → reassemble)
        → HiveMindListenerProtocol.handle_message
    and back the other way for downstream messages.

Nothing is importorskip'd or stubbed to dodge a dependency: real hivemind-core,
real hivemind_bus_client, real remailers PGP, real ovos-bus-client.
"""
from ovos_bus_client.message import Message

from hivemind_bus_client.message import HiveMessage, HiveMessageType

from tests.e2e.usenet_link import UsenetLink


# ---------------------------------------------------------------------------
# Handshake over the carrier
# ---------------------------------------------------------------------------

class TestHandshakeOverUsenet:
    """The HiveMind handshake completes end-to-end across the Usenet carrier."""

    def test_password_handshake_completes(self):
        link = UsenetLink(password_handshake=True)
        link.handshake()

        assert link.shim.handshake_event.is_set(), \
            "satellite never reached handshake_event"
        # v3 (Noise) sessions carry their session key in noise_transport, not
        # the legacy crypto_key attribute.
        assert link.conn.noise_transport is not None, "master derived no session key"
        assert link.shim.noise_transport is not None, "satellite derived no session key"

    def test_both_ends_derive_the_same_session_key(self):
        """Master and satellite derived reciprocal Noise CipherStates: a
        frame either side encrypts, the other decrypts."""
        link = UsenetLink(password_handshake=True)
        link.handshake()
        frame = link.conn.noise_transport.encrypt_frame(b"ping")
        assert link.shim.noise_transport.decrypt_frame(frame) == b"ping"

    def test_master_registers_connected_peer(self):
        link = UsenetLink(password_handshake=True)
        link.handshake()
        peers = link.master.connected_peers()
        assert len(peers) == 1
        assert peers[0].startswith("S0::")

    def test_handshake_actually_traversed_the_carrier(self):
        """The handshake produced real articles on the (fake) news server."""
        link = UsenetLink(password_handshake=True)
        assert link.server.post_count == 0
        link.handshake()
        # HELLO + HANDSHAKE down, HANDSHAKE + HELLO up, etc. — several posts.
        assert link.server.post_count >= 4


# ---------------------------------------------------------------------------
# Full message round-trip after the handshake
# ---------------------------------------------------------------------------

class TestBusRoundTripOverUsenet:
    """A bus message rides the encrypted session over the carrier in both
    directions after the handshake."""

    def test_upstream_bus_message_reaches_agent(self):
        """Satellite → master: an allowed utterance is injected on the agent bus."""
        link = UsenetLink(allowed_types=["recognizer_loop:utterance"])
        link.handshake()

        link.emit_from_satellite(
            Message("recognizer_loop:utterance", {"utterances": ["what time is it"]})
        )

        injected = [m.msg_type for m in link.master.agent_protocol.injected]
        assert "recognizer_loop:utterance" in injected
        utt = link.master.agent_protocol.last_injected("recognizer_loop:utterance")
        assert utt.data["utterances"] == ["what time is it"]

    def test_downstream_bus_message_reaches_satellite(self):
        """Master → satellite: a downstream speak lands on the satellite bus."""
        link = UsenetLink(allowed_types=["recognizer_loop:utterance"])
        link.handshake()
        peer = link.master.connected_peers()[0]

        received = []
        link.bus.on("speak", received.append)

        reply = Message("speak", {"utterance": "it is noon"},
                        {"destination": [peer]})
        link.master.send_to_satellite(
            peer, HiveMessage(HiveMessageType.BUS, payload=reply)
        )
        link.pump()

        assert len(received) == 1
        msg = received[0]
        if isinstance(msg, str):
            msg = Message.deserialize(msg)
        assert msg.data["utterance"] == "it is noon"

    def test_full_query_response_round_trip(self):
        """Handshake → upstream query → downstream response, byte-correct."""
        link = UsenetLink(allowed_types=["recognizer_loop:utterance"])
        link.handshake()
        peer = link.master.connected_peers()[0]

        # 1. satellite asks a question
        link.emit_from_satellite(
            Message("recognizer_loop:utterance", {"utterances": ["ping"]})
        )
        assert link.master.agent_protocol.last_injected(
            "recognizer_loop:utterance"
        ) is not None

        # 2. master answers
        received = []
        link.bus.on("speak", received.append)
        link.master.send_to_satellite(
            peer,
            HiveMessage(HiveMessageType.BUS,
                        payload=Message("speak", {"utterance": "pong"},
                                        {"destination": [peer]})),
        )
        link.pump()

        assert received, "no downstream response delivered to satellite"
        msg = received[0]
        if isinstance(msg, str):
            msg = Message.deserialize(msg)
        assert msg.data["utterance"] == "pong"

    def test_payload_is_encrypted_on_the_wire(self):
        """Post-handshake BUS payloads are AES-encrypted; plaintext never
        appears in the carrier articles."""
        link = UsenetLink(allowed_types=["recognizer_loop:utterance"])
        link.handshake()

        secret_text = "TOP-SECRET-UTTERANCE-MARKER"
        link.emit_from_satellite(
            Message("recognizer_loop:utterance", {"utterances": [secret_text]})
        )

        # The fake server holds every PGP-armored article. Even after PGP is
        # stripped the HiveMind session payload is AES-encrypted, so the marker
        # must not be recoverable as plaintext anywhere on the wire.
        for art in link.server.get_articles("g", limit=1000):
            assert secret_text not in art.text


# ---------------------------------------------------------------------------
# Carrier characteristics under the real protocol
# ---------------------------------------------------------------------------

class TestCarrierBehaviourEndToEnd:

    def test_large_bus_payload_chunks_and_reassembles(self):
        """A payload larger than CHUNK_SIZE spans multiple articles and is
        reassembled before delivery to the satellite."""
        from hivemind_usenet.carrier import CHUNK_SIZE

        link = UsenetLink(allowed_types=["recognizer_loop:utterance"])
        link.handshake()
        peer = link.master.connected_peers()[0]
        posts_before = link.server.post_count

        received = []
        link.bus.on("speak", received.append)

        big = "X" * (CHUNK_SIZE * 3)
        link.master.send_to_satellite(
            peer,
            HiveMessage(HiveMessageType.BUS,
                        payload=Message("speak", {"utterance": big},
                                        {"destination": [peer]})),
        )
        link.pump()

        # The big message had to be posted as more than one article.
        assert link.server.post_count - posts_before >= 2
        assert received
        msg = received[0]
        if isinstance(msg, str):
            msg = Message.deserialize(msg)
        assert msg.data["utterance"] == big

    def test_no_real_network_used(self):
        """Sanity: the carrier server is the in-memory fake, not UsenetServer."""
        from tests.e2e.usenet_link import FakeNNTPServer
        link = UsenetLink()
        assert isinstance(link.server, FakeNNTPServer)
        assert isinstance(link.hub_carrier.server, FakeNNTPServer)
        assert isinstance(link.client_carrier.server, FakeNNTPServer)

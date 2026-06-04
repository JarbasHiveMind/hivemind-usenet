# hivemind-usenet

HiveMind transport over Usenet: anonymous, store-and-forward, censorship-resistant mesh links using `alt.anonymous.messages`.

Ships three components in one package:

- **`UsenetCarrier`** — shared framing layer. Chunks arbitrary payloads into ~8 KB base64 frames, PGP-encrypts each to the peer's key, posts under an hSub subject, reassembles on poll. Fully unit-testable without a live NNTP connection.
- **`UsenetWormhole`** (`NetworkProtocol`) — full HiveMessage transport. Point-to-point link; poll loop reassembles kind=`"hive"` frames and routes via `hm_protocol.handle_message`. Entry-point: `hivemind.network.protocol`.
- **`UsenetBridge`** (`threading.Thread`) — natural-language gateway. Polls a code-word hSub for text posts, injects as utterances into the hive, posts spoken answers back. BRIDGE-1 compliant: per-peer session ids, per-session FIFO queues.

## Quick start

```bash
pip install hivemind-usenet

# Wormhole node
hivemind-usenet-wormhole \
    --my-secret  "my-passphrase" \
    --peer-secret "their-passphrase" \
    --peer-pubkey /path/to/peer.asc \
    --server-url paganini.bofh.team

# NL bridge
hivemind-usenet-bridge \
    --my-secret "bridge-codeword" \
    --hive-host 127.0.0.1 \
    --hive-key  "my-hivemind-api-key"
```

## First-contact / hSub addressing

hSub addressing is shared-secret symmetric: both peers must agree on a passphrase out-of-band before posting. There is no in-band key exchange in v1; a discovery "lobby" hSub is a planned stretch goal.

## License

Apache-2.0

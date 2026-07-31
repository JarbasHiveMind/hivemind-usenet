# Components

The package ships four surfaces over the shared carrier.

## `UsenetCarrier`

The framing and transport primitive. Given a `Credentials` (PGP identity) and a
`UsenetServer`, it:

- `send(payload, peer_secret, peer_pubkey, kind)`: chunk, PGP-encrypt, and post
  under `create_hsub(peer_secret)`.
- `poll(my_secret, limit)`: fetch, hSub-match, decrypt, reassemble, and return
  the completed `(kind, bytes)` payloads.

It needs no live NNTP connection to test. Inject a stub server and stub
credentials instead. Its constants are `CHUNK_SIZE` (raw bytes per chunk),
`CARRIER_VERSION`, and the valid `kind` values (`"hive"`, `"nl"`).

## `UsenetWormhole`: full HiveMessage transport

A `NetworkProtocol` (entry point `hivemind.network.protocol`). Its `run()` poll
loop reassembles `kind="hive"` payloads, deserializes each into a
`HiveMessage`, and routes it through `hm_protocol.handle_message(msg, client)`.
The peer is modeled as a single `HiveMindClientConnection` whose `send_msg`
posts back under the reply hSub. Use it to tunnel complete HiveMind protocol
traffic between two nodes over Usenet.

CLI: `hivemind-usenet-wormhole`.

## `UsenetBridge`: natural-language gateway

A `threading.Thread` that moves *text*, so a human on any newsreader can
interact:

- Polls a code-word hSub for `kind="nl"` (or plaintext) posts.
- Injects each as an utterance or query into a local hive through a
  `HiveMessageBusClient`.
- Posts the spoken answer back under the reply hSub.

Per-peer session ids and per-session FIFO queues keep concurrent conversations
separate. PGP is optional here, since a human can post plaintext under the code
word.

CLI: `hivemind-usenet-bridge`.

## `UsenetClient`: satellite side

Mirrors the HTTP/WS client API (`connect` / `emit` / `run` / `close`), so the
same satellite code works over the Usenet carrier. The HiveMind handshake and
AES session ride on top exactly as for the other transports. Expect the
handshake to span several poll cycles.

CLI: `hivemind-usenet-client`.

---
[← How it works](how-it-works.md) · [Home](index.md) · [Configuration →](configuration.md)

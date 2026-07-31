# hivemind-usenet

An **experimental** [HiveMind](https://github.com/JarbasHiveMind/HiveMind-core)
transport over **Usenet**: anonymous, store-and-forward, censorship-resistant mesh
links carried by `alt.anonymous.messages`. Nodes post PGP-encrypted, hSub-addressed
articles to a newsgroup and poll for replies. There is no direct connection, no
shared IP, and no registered account on the public anon-post servers.

Usenet is high-latency and poll-based. Treat this as a covert or control-plane
link, or as a fallback for nodes that are never reachable directly, not as a
low-latency audio channel.

## Where it sits

Standard HiveMind links are live encrypted WebSocket connections to a
[hivemind-core](https://github.com/JarbasHiveMind/HiveMind-core) hub.
hivemind-usenet swaps the carrier for Usenet and keeps the HiveMind handshake
and AES session on top, unchanged. It ships three surfaces in one package:

- **`UsenetCarrier`**: the shared framing layer. It chunks arbitrary payloads
  into roughly 8 KB base64 frames, PGP-encrypts each to the peer's key, posts
  under an hSub subject, and reassembles on poll. You can test it fully without
  a live NNTP connection.
- **`UsenetWormhole`** (a `NetworkProtocol`): the full HiveMessage transport. A
  point-to-point link whose poll loop reassembles `kind="hive"` frames and routes
  them through `hm_protocol.handle_message`. It is registered under the
  `hivemind.network.protocol` entry point.
- **`UsenetBridge`** (a `threading.Thread`): a natural-language gateway. It
  polls a code-word hSub for text posts, injects them into the hive as
  utterances, and posts the spoken answers back. Per-peer session ids and
  per-session FIFO queues keep concurrent conversations separate.

A satellite-side client (`UsenetClient`) mirrors the HTTP/WS client API, so the
same connect/emit/run/close usage works over the Usenet carrier.

## How it works

```
local node  ◀──── UsenetCarrier (PGP + hSub frames) ────▶  peer node
                 post to alt.anonymous.messages
                 poll + match hSub + decrypt + reassemble
```

- **Addressing** is hSub (hashed subject): a shared passphrase per peer. The
  sender stamps the subject with `create_hsub(peer_secret)`. The receiver
  filters articles with `match_hsub(subject, my_secret)`.
- **Confidentiality** is PGP. Each frame is encrypted to the peer's public key.
- **Reassembly** dedupes on `(message-id, chunk)` and rebuilds the payload once
  all chunks arrive.

## Prerequisites

- Python 3.10-3.12. The carrier's PGP crypto (`remailers` to **PGPy**) imports
  the standard-library `imghdr` module, which was removed in Python 3.13
  (PEP 594). The package is capped `>=3.10,<3.13`. See
  [the security docs](docs/security.md#python-312-ceiling--why).
- A PGP identity per node. `remailers.Credentials` generates one automatically
  at the configured `key_path` if it is missing.
- For each peer link, an out-of-band exchange of an **hSub passphrase** and the
  peer's **PGP public key** (see [first contact](#first-contact--hsub-addressing)).
- Network access to a Usenet server that accepts anonymous posts. This defaults
  to `paganini.bofh.team`. `news.tcpreset.net` also accepts anonymous posts.

## Install

```bash
pip install hivemind-usenet
```

From source:

```bash
git clone https://github.com/JarbasHiveMind/hivemind-usenet
cd hivemind-usenet
pip install -e .
```

This pulls in the `usenet` and `remailers` carrier libraries plus the HiveMind
packages.

## Quickstart

Two nodes that have exchanged passphrases and public keys out of band:

```bash
# Wormhole node (full HiveMessage transport)
hivemind-usenet-wormhole \
    --my-secret   "my-passphrase" \
    --peer-secret "their-passphrase" \
    --peer-pubkey /path/to/peer.asc \
    --server-url  paganini.bofh.team

# NL bridge (text in/out of a local hive)
hivemind-usenet-bridge \
    --my-secret "bridge-codeword" \
    --hive-host 127.0.0.1 \
    --hive-key  "my-hivemind-api-key"
```

`--my-secret` is the hSub passphrase you **read** with. `--peer-secret` is the
one you **post** with, and the peer reads it. They are the mirror image on the
other node.

## First contact / hSub addressing

hSub addressing is **shared-secret symmetric**: both peers must agree on a
passphrase and exchange PGP public keys out of band before any post. There is
no in-band key exchange in v1. A discovery "lobby" hSub is a planned stretch
goal.

## Configuration

CLI flags map to config keys read by each component. See
[configuration](docs/configuration.md) for the full table and the JSON
config-file form.

## Documentation

See [`docs/`](docs/index.md):

- [How it works](docs/how-it-works.md): carrier framing, hSub, PGP, reassembly.
- [Components](docs/components.md): carrier, wormhole, bridge, client.
- [Configuration](docs/configuration.md): every CLI flag and config key.
- [Security model](docs/security.md): the two crypto layers, the threat model, and the Python 3.12 ceiling.
- [Examples](docs/examples.md): the live-network demos under `examples/`.
- [Testing & development](docs/testing.md): running the offline suite and how the end-to-end carrier is faked.

## Related projects

- [HiveMind-core](https://github.com/JarbasHiveMind/HiveMind-core): the hub this
  transport connects to.
- [hivescope](https://github.com/JarbasHiveMind/hivescope): the in-process
  HiveMind test harness used by this package's end-to-end tests.

## Tests

All tests run **offline**. There is no live news server and no network. The
carrier's NNTP transport is the only thing faked. The end-to-end suite drives a
real hivemind-core master and satellite through a real handshake and bus
round-trip over the real `UsenetCarrier`. The package is capped at Python 3.12,
as noted above:

```bash
uv venv --python 3.12
uv pip install --prerelease=allow -e .[test]
uv run pytest tests/
```

See [docs/testing.md](docs/testing.md) for the full layout and how the carrier
is faked.

## License

Apache-2.0

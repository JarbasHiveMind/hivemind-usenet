# hivemind-usenet

An experimental HiveMind transport over Usenet: anonymous, store-and-forward,
censorship-resistant mesh links carried by `alt.anonymous.messages`.

- [How it works](how-it-works.md)
- [Components](components.md)
- [Configuration](configuration.md)
- [Security model](security.md): the two crypto layers, the threat model, and the Python 3.12 ceiling.
- [Examples](examples.md)
- [Testing & development](testing.md): running the offline suite and how the end-to-end carrier is faked.

## Status

Experimental. The carrier framing is fully offline-testable. The wormhole,
bridge, and client run against live Usenet servers. Usenet is high-latency and
poll-based, so this is a covert or control-plane link, or a fallback for
unreachable nodes, not a real-time audio channel.

## Where it sits

The transport swaps the HiveMind carrier for Usenet and keeps the HiveMind
handshake and AES session on top, unchanged. A node posts PGP-encrypted,
hSub-addressed articles to a newsgroup and polls for replies. The
[hivemind-core](https://github.com/JarbasHiveMind/HiveMind-core) protocol layer
is otherwise untouched.

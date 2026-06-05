# hivemind-usenet

An experimental HiveMind transport over Usenet: anonymous, store-and-forward,
censorship-resistant mesh links carried by `alt.anonymous.messages`.

- [How it works](how-it-works.md)
- [Components](components.md)
- [Configuration](configuration.md)
- [Examples](examples.md)

## Status

Experimental. The carrier framing is fully offline-testable; the wormhole, bridge,
and client run against live Usenet servers. Usenet is high-latency and poll-based,
so this is a covert / control-plane link or a fallback for unreachable nodes — not
a real-time audio channel.

## Where it sits

The transport swaps the HiveMind carrier for Usenet while keeping the HiveMind
handshake and AES session on top, unchanged. A node posts PGP-encrypted,
hSub-addressed articles to a newsgroup and polls for replies; the
[hivemind-core](https://github.com/JarbasHiveMind/HiveMind-core) protocol layer is
otherwise untouched.

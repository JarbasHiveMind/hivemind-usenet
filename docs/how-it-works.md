# How it works

Every surface (wormhole, bridge, client) sits on the shared `UsenetCarrier`. The
carrier turns a byte payload into a set of Usenet articles and back.

## Framing

```
payload ──chunk──▶ Frame{v, mid, seq, n, kind, data(b64)} ──PGP encrypt──▶ article
                   posted under subject = create_hsub(peer_secret)
```

- The payload is split into ~6 KB raw chunks (~8 KB after base64) so each post
  stays within a safe article size.
- Each chunk becomes a `Frame` dataclass: carrier version `v`, a `mid` (UUID4
  message id grouping the chunks of one logical message), `seq`/`n` (chunk index
  and total), `kind` (`"hive"` or `"nl"`), and the base64 `data`.
- Each frame is PGP-encrypted to the peer's public key, then posted to the
  newsgroup.

## Addressing — hSub

Articles are addressed with **hSub** (hashed subject), a shared-secret scheme:

- The sender stamps the subject with `create_hsub(peer_secret)`.
- The receiver scans the group and keeps only articles where
  `match_hsub(subject, my_secret)` is true.

Because it is symmetric, both peers must agree on the passphrase out of band. The
recipient is hidden — only a holder of the secret can tell which articles are
theirs.

## Confidentiality

PGP encryption is the transport's equivalent of TLS for a WebSocket: every frame
is encrypted to the peer's pubkey. The HiveMind handshake and its AES session key
negotiation ride on top, unchanged — HELLO/HANDSHAKE are ordinary HiveMessages,
serialised, chunked, encrypted, and posted by the carrier.

## Poll and reassembly

There is no push. Each node polls the group on a cadence (`poll_seconds`):

1. Fetch recent articles, keep hSub matches, decrypt each frame.
2. Buffer frames by `mid`; dedupe on `(mid, seq)` (the same article arrives via
   many feeds).
3. When all `n` chunks of a `mid` are present, reassemble and deliver
   `(kind, bytes)`.

## Latency

A poll cycle can take 30 s or more depending on server load and `poll_seconds`, and
the full HiveMind handshake spans several cycles before the AES session is
established. Timeouts are intentionally generous. Treat this as an async/covert
link, not a real-time channel.

# Security model

hivemind-usenet stacks two independent crypto layers. Understanding the split is
the key to reasoning about what is protected and against whom.

```
┌─ HiveMind session (end-to-end) ─────────────────────────────┐
│  HELLO / HANDSHAKE  →  AES-GCM (or ChaCha20-Poly1305) session│
│  the same handshake the WebSocket transport uses, unchanged  │
└──────────────────────────────────────────────────────────────┘
                          ▲ rides on top of ▼
┌─ Usenet carrier (transport, per article) ───────────────────┐
│  PGP encryption to the peer's public key                     │
│  hSub addressing (hashed subject, shared passphrase)         │
└──────────────────────────────────────────────────────────────┘
                          ▲ posted to ▼
                   alt.anonymous.messages
```

## Layer 1: Usenet carrier (transport)

The carrier is the analogue of TLS for a WebSocket. Each article is:

- **Encrypted with PGP** to the recipient's RSA public key (through
  `remailers.keys.Credentials`, RSA-4096 by default). Only the holder of the
  matching private key can decrypt it.
- **Addressed with hSub**: the subject line is `create_hsub(peer_secret)`, a
  hash of a shared passphrase. A receiver keeps only articles where
  `match_hsub(subject, my_secret)` is true. The recipient stays *unlinkable*.
  To any third party the subject is opaque, and only a holder of the secret
  can tell which articles are theirs.

hSub is **symmetric shared-secret**: both peers must agree on the passphrase
and exchange PGP public keys out of band before the first post. There is no
in-band key exchange in v1.

## Layer 2: HiveMind session (end-to-end)

The HiveMind handshake and AES session key negotiation ride on top of the
carrier *unchanged* from the WebSocket transport. HELLO and HANDSHAKE are
ordinary `HiveMessage`s, serialized, chunked, PGP-encrypted, and posted like
any other payload. Once the handshake completes, hivemind-core AES-encrypts
every bus message **before** it reaches the carrier. So even an attacker who
defeats the PGP layer, or a future bug in it, still faces the HiveMind session
cipher.

The `test_payload_is_encrypted_on_the_wire` end-to-end test asserts exactly
this. A known plaintext marker placed in a bus utterance never appears in any
article held by the (faked) news server.

## Threat model: what each layer buys you

| Adversary | Mitigation |
|-----------|------------|
| Passive newsgroup reader | PGP hides content. hSub hides the recipient. |
| News server operator | The same protection applies: the operator sees opaque ciphertext under opaque subjects. |
| Defeats or strips the PGP layer | The HiveMind AES session still protects bus payloads. |
| Wants to inject a forged bus message | The HiveMind handshake authenticates the session. ACLs (`allowed_types`) gate what a satellite may inject. |
| Network-level observer of your IP | You only ever talk to a public news server, with no direct peer connection, no shared IP, and no registered account on anon-post servers. |

What it does **not** give you: low latency, delivery guarantees, or resistance
to traffic analysis of *volume and timing* on the newsgroup. Treat it as a
covert, control-plane, or fallback link.

## Python 3.12 ceiling: why

The package is capped at `requires-python = ">=3.10,<3.13"`, and the CI matrix
covers `3.10 / 3.11 / 3.12`.

The carrier's PGP crypto comes from `remailers`, which depends on **PGPy**.
PGPy imports the standard-library **`imghdr`** module, and `imghdr` was
**removed in Python 3.13** (PEP 594). On 3.13+ the import fails at load time,
so the whole carrier is unusable.

The cap is therefore a hard transitive constraint, not a policy choice.
Supporting 3.13+ would require migrating off PGPy, which is out of scope for
this repo. The `license_check` and `pip_audit` CI jobs are pinned to Python
3.12 and exclude the git-installed `usenet`, `remailers`, and self packages so
they stay green under the same ceiling.

---
[← Configuration](configuration.md) · [Home](index.md) · [Examples →](examples.md)

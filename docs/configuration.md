# Configuration

Each component reads its settings from CLI flags (or a JSON config file via
`--config`). The hSub secret naming follows the carrier convention: you **read**
articles with `my_secret` and **post** with the peer's secret.

## Wormhole (`hivemind-usenet-wormhole`)

| Flag | Default | Description |
| --- | --- | --- |
| `--config` | none | Path to a JSON config file. |
| `--my-secret` | none | hSub passphrase you read with. |
| `--peer-secret` | none | hSub passphrase you post with (peer reads this). |
| `--peer-pubkey` | none | Path to the peer's ASCII-armored PGP public key. |
| `--key-path` | `/tmp/hivemind_usenet_wormhole.asc` | Local PGP identity path (auto-generated if missing). |
| `--server-url` | `paganini.bofh.team` | NNTP server hostname. |
| `--group` | `alt.anonymous.messages` | Newsgroup. |
| `--poll-seconds` | `30` | Poll cadence in seconds. |

## Bridge (`hivemind-usenet-bridge`)

| Flag | Default | Description |
| --- | --- | --- |
| `--my-secret` | required | hSub passphrase / code word the bridge reads. |
| `--reply-secret` | `""` | hSub passphrase used to post replies. |
| `--reply-pubkey` | none | Path to the pubkey replies are encrypted to. |
| `--key-path` | none | Local PGP identity path. |
| `--server-url` | `paganini.bofh.team` | NNTP server hostname. |
| `--group` | `alt.anonymous.messages` | Newsgroup. |
| `--hive-host` | `127.0.0.1` | Local hive (hivemind-core) host. |
| `--hive-port` | `5678` | Local hive port. |
| `--hive-key` | `""` | HiveMind access key for the bridge's bus client. |
| `--poll-seconds` | `60` | Poll cadence in seconds. |

## Client (`hivemind-usenet-client`)

| Flag | Default | Description |
| --- | --- | --- |
| `--config` | none | Path to a JSON config file. |
| `--my-secret` | none | hSub passphrase the hub posts with (the client reads these). |
| `--hub-secret` | none | hSub passphrase the client posts with (the hub reads these). |
| `--hub-pubkey` | none | Path to the hub's ASCII-armored PGP public key. |
| `--key-path` | none | Local PGP identity path. |
| `--server-url` | `paganini.bofh.team` | NNTP server hostname. |
| `--group` | `alt.anonymous.messages` | Newsgroup. |
| `--poll-seconds` | `30` | Poll cadence in seconds. |

## Servers

The public anon-post hosts that accept posts without an account are
`paganini.bofh.team` (default) and `news.tcpreset.net`. Pick a low-traffic group
if running a dedicated link.

---
[← Components](components.md) · [Home](index.md) · [Security model →](security.md)

# Examples

The `examples/` directory holds live-network demos. They require real Usenet
access and are **not** run in the test suite.

## Wormhole ping (`examples/live_wormhole.py`)

Two nodes on separate machines (or terminals) with mirrored secrets. Node A sends a
periodic PING; node B prints it on receipt.

```bash
# Node A
python examples/live_wormhole.py \
    --my-secret   "a-receives-on-this" \
    --peer-secret "b-receives-on-this" \
    --peer-pubkey peer_b.asc \
    --key-path    node_a.asc

# Node B (mirror the secrets)
python examples/live_wormhole.py \
    --my-secret   "b-receives-on-this" \
    --peer-secret "a-receives-on-this" \
    --peer-pubkey peer_a.asc \
    --key-path    node_b.asc
```

Note the mirror: A's `--my-secret` equals B's `--peer-secret`, and vice versa.

## Bridge (`examples/live_bridge.py`)

Runs the NL gateway against a local hive: posts to the code-word hSub are injected
as utterances and the answers are posted back. Point `--hive-host`/`--hive-key` at
a running `hivemind-core` and pick a code word for `--my-secret`.

## First-contact checklist

Before either demo works, both peers must have exchanged out of band:

1. A shared hSub passphrase per direction.
2. Each other's PGP public keys (the `--peer-pubkey` / `--reply-pubkey` files).

The local PGP identity at `--key-path` is generated automatically on first run if
the file does not exist.

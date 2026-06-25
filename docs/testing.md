# Testing & development

All tests run **offline** — no live NNTP server and no network. The carrier's
NNTP transport is the only thing faked; everything else (HiveMind handshake, AES
session, PGP carrier crypto, hSub addressing) is the real code path.

## Running the tests

The project is capped at Python 3.12 (see [security](security.md#python-312-ceiling--why)).
Use a 3.12 interpreter. With [uv](https://github.com/astral-sh/uv):

```bash
uv venv --python 3.12
uv pip install --prerelease=allow -e .[test]
uv run pytest tests/
```

Or with stock tooling:

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .[test]
pytest tests/
```

The `test` extra pulls in `pytest` and
[`hivescope`](https://github.com/JarbasHiveMind/hivescope) (the in-process
HiveMind test harness used to drive a real hivemind-core master). The suite uses
real RSA-4096 PGP key generation, so a full run takes a few minutes — this is
expected, not a hang.

## Layout

One test directory, two tiers:

| Path | What it covers |
|------|----------------|
| `tests/test_carrier.py` | `UsenetCarrier` framing, chunking, dedup, reassembly, hSub, PGP round-trip — unit level, stub server + stub creds. |
| `tests/test_wormhole.py` | `UsenetWormhole` send/receive between two in-process wormholes over a shared fake server. |
| `tests/test_client.py` | `HiveMindUsenetClient` ↔ wormhole bidirectional messaging over a shared fake server. |
| `tests/e2e/test_smoke.py` | hivescope wiring check — a single-satellite topology completes a handshake. |
| `tests/e2e/test_usenet_e2e.py` | **Full end-to-end** — a real hivemind-core master + real satellite, with the wire carried by the real `UsenetCarrier` over a faked NNTP server. |

## How the end-to-end harness fakes the carrier

`tests/e2e/usenet_link.py` wires a real
[hivescope](https://github.com/JarbasHiveMind/hivescope) `MasterNode`
(`HiveMindListenerProtocol`) to a real `HiveMindSlaveProtocol`, but routes the
wire between them through real `UsenetCarrier` instances:

```
satellite slave-protocol
    → HiveMessage.serialize()
    → UsenetCarrier.send   (chunk → PGP-encrypt → post under hSub)
    → FakeNNTPServer       (in-memory article list — no socket, no network)
    → UsenetCarrier.poll   (match hSub → PGP-decrypt → reassemble)
    → HiveMindListenerProtocol.handle_message
        … and symmetrically for downstream master → satellite messages.
```

`FakeNNTPServer` is the **only** mock: an in-memory list with the real server's
`post` / `get_articles(newest-first)` semantics. Both carriers share one instance,
modelling a single newsgroup that both nodes read and write. The handshake and bus
messages produce genuine chunked, PGP-armored articles in that list.

Because Usenet is poll-based, the harness exposes `pump()` — it drains both
directions repeatedly until the store-and-forward exchange settles (handshake
replies and bus responses are generated *inside* the protocol handlers, which post
new articles, so several rounds are needed). This deterministically reproduces the
multi-poll-cycle latency of a real link without any wall-clock waiting.

Nothing is `importorskip`'d or `skipif`'d to dodge a dependency: the e2e requires
real `hivemind-core`, `hivemind-bus-client`, `remailers`, and `ovos-bus-client`.

## What the e2e asserts

- The HiveMind password handshake completes across the carrier; both ends derive
  the **same** AES session key; the master registers the connected peer.
- A bus message round-trips in both directions (satellite → agent bus, master →
  satellite internal bus) byte-correct.
- Post-handshake payloads are encrypted on the wire — a plaintext marker never
  appears in any article.
- A payload larger than `CHUNK_SIZE` spans multiple articles and reassembles.

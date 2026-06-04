"""Live-network wormhole demo (NOT run in tests).

Requires:
    - A live NNTP server (paganini.bofh.team or news.tcpreset.net)
    - Two separate machines (or two terminal sessions) each running this
      script with mirrored --my-secret / --peer-secret values.

Usage (node A):
    python live_wormhole.py \
        --my-secret  "a-receives-on-this" \
        --peer-secret "b-receives-on-this" \
        --peer-pubkey peer_b.asc \
        --key-path    node_a.asc

Usage (node B):
    python live_wormhole.py \
        --my-secret  "b-receives-on-this" \
        --peer-secret "a-receives-on-this" \
        --peer-pubkey peer_a.asc \
        --key-path    node_b.asc

Node A will periodically send a PING; node B will print it when received.
"""
import argparse
import time
import threading

from hivemind_bus_client.message import HiveMessage, HiveMessageType
from remailers.keys import Credentials
from usenet import UsenetServer

from hivemind_usenet.carrier import UsenetCarrier, _KIND_HIVE
from hivemind_usenet.wormhole import UsenetWormhole


class _PrintProtocol:
    def handle_message(self, msg, client):
        print(f"[RECEIVED] {msg.msg_type}: {msg.serialize()[:200]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--my-secret",   required=True)
    parser.add_argument("--peer-secret", required=True)
    parser.add_argument("--peer-pubkey", required=True)
    parser.add_argument("--key-path",    default="node.asc")
    parser.add_argument("--server-url",  default="paganini.bofh.team")
    parser.add_argument("--group",       default="alt.anonymous.messages")
    parser.add_argument("--poll-seconds",type=int, default=30)
    parser.add_argument("--send-ping",   action="store_true",
                        help="Send a PING every poll cycle")
    args = parser.parse_args()

    creds  = Credentials(args.key_path)
    server = UsenetServer(args.server_url)

    with open(args.peer_pubkey) as fh:
        peer_pubkey = fh.read()

    carrier = UsenetCarrier(creds, server, group=args.group)

    hm = _PrintProtocol()
    wh = UsenetWormhole(
        config={
            "my_secret":    args.my_secret,
            "peer_secret":  args.peer_secret,
            "peer_pubkey":  peer_pubkey,
            "poll_seconds": args.poll_seconds,
        },
        hm_protocol=hm,
    )
    wh._carrier = carrier

    if args.send_ping:
        def _pinger():
            while True:
                msg  = HiveMessage(HiveMessageType.PING, payload={"ts": time.time()})
                data = msg.serialize().encode()
                carrier.send(data, peer_secret=args.peer_secret,
                             peer_pubkey=peer_pubkey, kind=_KIND_HIVE)
                print(f"[SENT] PING at {time.time():.0f}")
                time.sleep(args.poll_seconds)
        threading.Thread(target=_pinger, daemon=True).start()

    print(f"Wormhole running (poll={args.poll_seconds}s)  Ctrl-C to stop")
    wh.run()


if __name__ == "__main__":
    main()

"""Live-network NL bridge demo (NOT run in tests).

Usage:
    python live_bridge.py \
        --my-secret   "bridge-codeword" \
        --reply-secret "reply-codeword" \
        --hive-host   127.0.0.1 \
        --hive-key    "your-hivemind-api-key"

Any newsreader that posts a message with an hSub for "bridge-codeword" will
receive a reply under "reply-codeword".
"""
import argparse
from remailers.keys import Credentials
from usenet import UsenetServer
from hivemind_usenet.bridge import UsenetBridge


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--my-secret",    required=True)
    parser.add_argument("--reply-secret", default="")
    parser.add_argument("--reply-pubkey", default=None)
    parser.add_argument("--key-path",     default=None)
    parser.add_argument("--server-url",   default="paganini.bofh.team")
    parser.add_argument("--group",        default="alt.anonymous.messages")
    parser.add_argument("--hive-host",    default="127.0.0.1")
    parser.add_argument("--hive-port",    type=int, default=5678)
    parser.add_argument("--hive-key",     default="")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()

    creds = Credentials(args.key_path) if args.key_path else None
    server = UsenetServer(args.server_url)

    reply_pubkey = None
    if args.reply_pubkey:
        with open(args.reply_pubkey) as fh:
            reply_pubkey = fh.read()

    bridge = UsenetBridge(
        creds=creds,
        server=server,
        hive_host=args.hive_host,
        hive_port=args.hive_port,
        hive_key=args.hive_key,
        my_secret=args.my_secret,
        reply_secret=args.reply_secret,
        reply_pubkey=reply_pubkey,
        group=args.group,
        poll_seconds=args.poll_seconds,
    )
    print("Bridge running  Ctrl-C to stop")
    bridge.start()
    bridge.join()


if __name__ == "__main__":
    main()

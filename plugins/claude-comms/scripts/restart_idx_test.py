"""
restart_idx_test.py — the message index survives a bridge restart.

Pre-seeds inbox.jsonl (as if written before a restart), constructs a fresh
IRCClient pointed at it, and verifies the index continues PAST the persisted
max — so the delivery hook's persistent cursor won't silently skip new messages
after a reload/reconnect.

Run:  python scripts/restart_idx_test.py
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bridge"))

from irc_client import IRCClient  # noqa: E402

failed = False


def check(label, cond):
    global failed
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failed = True


def main():
    d = tempfile.mkdtemp(prefix="comms_restart_")
    inbox = os.path.join(d, "inbox.jsonl")
    with open(inbox, "w", encoding="utf-8") as f:
        for i in (1, 2, 3):
            f.write(json.dumps({"idx": i, "from": "claude-x",
                                "target": "#p", "text": f"old {i}"}) + "\n")

    # fresh client (simulates the post-restart bridge process); not started.
    c = IRCClient("127.0.0.1", 0, "x", "#p", inbox_file=inbox)
    check("idx seeded from existing inbox.jsonl", c._idx == 3)

    # a newly received message must get idx 4, not reuse 1
    c._handle_line(":sender!u@h PRIVMSG #p :post-restart message")
    last = json.loads(open(inbox, encoding="utf-8").read().strip().splitlines()[-1])
    check("new message continues past persisted idx (idx=4)",
          last["idx"] == 4 and last["text"] == "post-restart message")

    print()
    if failed:
        print("RESULT: FAILED")
        return 1
    print("RESULT: ALL PASS — idx survives restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())

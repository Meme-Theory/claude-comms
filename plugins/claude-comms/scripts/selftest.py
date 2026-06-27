"""
selftest.py — proves the ClaudeComms transport end to end, no Claude required.

It starts the IRC server on an ephemeral port, connects two IRCClients
(simulating two sessions), and checks that:
  1. a message from A is received by B,
  2. multi-line messages arrive as multiple entries,
  3. peers() sees both nicks,
  4. the read cursor only returns *new* messages.

Exit code 0 = all good.

Run:  python scripts/selftest.py
"""

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bridge"))

from irc_client import IRCClient  # noqa: E402

CHANNEL = "#selftest"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(host, port, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        check.failed = True


check.failed = False


def main():
    port = free_port()
    server_proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "ircd.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port("127.0.0.1", port):
            print("[FAIL] server did not start")
            return 1
        print(f"[info] server up on 127.0.0.1:{port}")

        alice = IRCClient("127.0.0.1", port, "alice", CHANNEL)
        bob = IRCClient("127.0.0.1", port, "bob", CHANNEL)
        check("alice connects", alice.start(wait=6))
        check("bob connects", bob.start(wait=6))
        time.sleep(0.6)  # let JOINs propagate

        # 1. basic delivery A -> B
        alice.send("hello bob")
        time.sleep(0.5)
        msgs, cursor = bob.read()
        check("bob receives alice's message",
              any(m["text"] == "hello bob" and m["from"] == "alice" for m in msgs))

        # 2. cursor advances: a second read returns nothing new
        msgs2, _ = bob.read()
        check("read cursor consumes messages", len(msgs2) == 0)

        # 3. multi-line splits into multiple entries B -> A
        bob.send("line one\nline two\nline three")
        time.sleep(0.5)
        amsgs, _ = alice.read()
        texts = [m["text"] for m in amsgs]
        check("multi-line arrives as 3 entries",
              texts == ["line one", "line two", "line three"])

        # 4. presence
        peers = alice.peers()
        check("peers() sees alice and bob",
              "alice" in peers and "bob" in peers)

        # 5. direct message (nick target) A -> bob
        alice.send("psst, direct", channel="bob")
        time.sleep(0.5)
        dmsgs, _ = bob.read()
        check("direct message delivered",
              any(m["text"] == "psst, direct" for m in dmsgs))

        alice.stop()
        bob.stop()
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server_proc.kill()

    print()
    if check.failed:
        print("RESULT: FAILED")
        return 1
    print("RESULT: ALL PASS — transport works end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())

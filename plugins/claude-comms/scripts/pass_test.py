"""
pass_test.py — verify the passphrase gate (IRC PASS) on the hub.

Starts an embedded hub WITH a password and checks that the correct passphrase
connects and can message, wrong/missing passphrases are rejected (464), and a
rejected client recovers after reconfiguring with the right passphrase.

Run:  python scripts/pass_test.py
"""

import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bridge"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from irc_client import IRCClient  # noqa: E402
import ircd as ircd_mod           # noqa: E402

PW = "s3cret-pass"
CH = "#gated"
failed = False


def check(label, cond):
    global failed
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failed = True


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_conn(c, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if c.is_connected():
            return True
        time.sleep(0.1)
    return c.is_connected()


def main():
    port = free_port()
    ok, info = ircd_mod.serve_in_thread("127.0.0.1", port, password=PW)
    check("passphrase-gated hub starts", ok)
    clients = []
    try:
        good = IRCClient("127.0.0.1", port, "good", CH, password=PW)
        good.start(wait=5)
        clients.append(good)
        check("correct passphrase connects", good.is_connected() and not good.auth_failed())

        wrong = IRCClient("127.0.0.1", port, "wrong", CH, password="nope")
        wrong.start(wait=0)
        clients.append(wrong)
        time.sleep(1.2)
        check("wrong passphrase rejected (464)",
              (not wrong.is_connected()) and wrong.auth_failed())

        nopw = IRCClient("127.0.0.1", port, "nopw", CH)  # no password at all
        nopw.start(wait=0)
        clients.append(nopw)
        time.sleep(1.2)
        check("missing passphrase rejected", not nopw.is_connected())

        good2 = IRCClient("127.0.0.1", port, "good2", CH, password=PW)
        good2.start(wait=5)
        clients.append(good2)
        time.sleep(0.6)
        good.send("hello gated room")
        time.sleep(0.5)
        msgs, _ = good2.read()
        check("authorized members exchange messages",
              any(m["text"] == "hello gated room" for m in msgs))

        # a rejected client recovers once given the right passphrase
        wrong.reconfigure(password=PW)
        check("reconfigure with correct passphrase recovers", wait_conn(wrong, 4.0))

        # multi-word passphrase works (colon-framed PASS)
        sp_port = free_port()
        ircd_mod.serve_in_thread("127.0.0.1", sp_port, password="open sesame")
        sp = IRCClient("127.0.0.1", sp_port, "spaced", "#g2", password="open sesame")
        sp.start(wait=5)
        clients.append(sp)
        check("multi-word passphrase connects", sp.is_connected())

        # unauthenticated WHO is refused before registration (no IP/nick leak)
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        raw.sendall(b"WHO #gated\r\n")
        time.sleep(0.5)
        raw.settimeout(1.0)
        try:
            resp = raw.recv(4096).decode("utf-8", "replace")
        except Exception:
            resp = ""
        raw.close()
        check("pre-auth WHO refused (451, no 352 leak)", "451" in resp and "352" not in resp)
    finally:
        for c in clients:
            c.stop()

    print()
    if failed:
        print("RESULT: FAILED")
        return 1
    print("RESULT: ALL PASS — passphrase gate enforced")
    return 0


if __name__ == "__main__":
    sys.exit(main())

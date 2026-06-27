"""
e2e_hook_test.py — full realtime pipeline, no live Claude session needed:

   sender ──IRC──► server ──► receiver bridge writes inbox.jsonl ──► hook injects

Proves a message sent by one session is picked up by the delivery hook of another
with the exact session-keyed path wiring the real bridge + hook use.

Run:  python scripts/e2e_hook_test.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bridge"))

from irc_client import IRCClient  # noqa: E402

HOOK = os.path.join(ROOT, "hooks", "comms_hook.py")
SID = "e2e-session-9"
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


def wait_port(host, port, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    port = free_port()
    base = tempfile.mkdtemp(prefix="comms_e2e_")
    inbox = os.path.join(base, SID, "inbox.jsonl")
    srv = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "ircd.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    recv = send = None
    try:
        if not wait_port("127.0.0.1", port):
            print("[FAIL] server did not start")
            return 1
        recv = IRCClient("127.0.0.1", port, "receiver", "#e2e", inbox_file=inbox)
        send = IRCClient("127.0.0.1", port, "sender", "#e2e")
        recv.start(wait=6)
        send.start(wait=6)
        time.sleep(0.6)

        send.send("hello via full pipeline")
        time.sleep(0.6)

        check("bridge wrote inbox.jsonl", os.path.isfile(inbox))

        env = {**os.environ, "COMMS_STATE_DIR": base}
        p = subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"hook_event_name": "PostToolUse", "session_id": SID}),
            capture_output=True, text=True, env=env)
        out = json.loads(p.stdout) if p.stdout.strip() else None
        ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        check("hook injects the received message", "hello via full pipeline" in ctx)
        check("injection is addressed from the sender", "sender" in ctx)
    finally:
        if recv:
            recv.stop()
        if send:
            send.stop()
        srv.terminate()
        try:
            srv.wait(timeout=3)
        except subprocess.TimeoutExpired:
            srv.kill()

    print()
    if failed:
        print("RESULT: FAILED")
        return 1
    print("RESULT: ALL PASS — full bridge->inbox->hook pipeline works")
    return 0


if __name__ == "__main__":
    sys.exit(main())

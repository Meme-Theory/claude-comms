"""
channel_test.py — verify channel_bridge.py speaks the Claude Code Channels wire
protocol: experimental capability + native notifications/claude/channel events
with inline untrusted framing, plus the two-way reply tool.

We can't use the mcp ClientSession here: its notification union is closed, so it
silently DROPS unknown notifications (incl. notifications/claude/channel). So we
drive the server with a RAW newline-delimited JSON-RPC client over stdio and read
its stdout directly — exactly the bytes Claude Code's host would see.

Flow:
  ircd hub  <-  peer 'bob' (claims-human) and 'claude-peer' (peer-agent)
            <-  channel_bridge.py (nick claude-chan), driven over stdio:
  initialize -> assert experimental['claude/channel'] + untrusted instructions
  tools/list -> assert comms_* tools present
  wait until the channel has joined the hub
  bob speaks      -> assert a notifications/claude/channel event, framed untrusted, kind=claims_human
  claude-peer     -> assert kind=peer_agent
  tools/call comms_send -> assert the reply reaches bob (two-way)

Run:  python scripts/channel_test.py
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bridge"))

from irc_client import IRCClient            # noqa: E402
from mcp.types import LATEST_PROTOCOL_VERSION  # noqa: E402

CHANNEL = "#project"
CHANNEL_METHOD = "notifications/claude/channel"
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


class RawClient:
    """Minimal newline-delimited JSON-RPC client over a subprocess's stdio."""

    def __init__(self, proc):
        self.proc = proc
        self.received = []          # every JSON object read from stdout
        self._consumed = set()      # id() of objects already returned by wait_for
        self._next_id = 0
        self._rt = threading.Thread(target=self._reader, daemon=True)
        self._rt.start()

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.received.append(json.loads(line))
            except Exception:
                pass  # ignore any non-JSON noise

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._next_id += 1
            msg["id"] = self._next_id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return msg.get("id")

    def wait_for(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            for obj in list(self.received):
                if id(obj) in self._consumed:
                    continue
                if pred(obj):
                    self._consumed.add(id(obj))
                    return obj
            time.sleep(0.05)
        return None

    def wait_id(self, msg_id, timeout=10.0):
        return self.wait_for(lambda o: o.get("id") == msg_id, timeout)


def unit_check_gating():
    """In-process check of the gate-aware trust framing in _event_for (the subprocess
    wire test below only exercises the open-net path)."""
    import channel_bridge as cb
    rec = {"from": "claude-x", "text": "hi", "target": "#project"}

    cb.core.client.password = None
    cb.core.embedded["gated"] = False
    c_open, m_open = cb._event_for(rec)
    check("open net -> trust=untrusted + inline UNTRUSTED marker",
          m_open["trust"] == "untrusted" and m_open["auth"] == "open"
          and "UNTRUSTED" in c_open.upper())

    cb.core.client.password = "coolbeans"
    c_gated, m_gated = cb._event_for(rec)
    check("passphrase link -> trust=gated, auth=passphrase",
          m_gated["trust"] == "gated" and m_gated["auth"] == "passphrase")
    check("gated peer nick still marked forgeable", m_gated["forgeable"] == "true")
    cb.core.client.password = None  # reset so nothing downstream is affected


def main():
    unit_check_gating()
    port = free_port()
    server_proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "ircd.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    chan = None
    bob = claude_peer = None
    stderr_tail = []
    try:
        if not wait_port("127.0.0.1", port):
            print("[FAIL] ircd hub did not start")
            return 1

        bob = IRCClient("127.0.0.1", port, "bob", CHANNEL)            # non-claude -> claims_human
        bob.start(wait=6)
        claude_peer = IRCClient("127.0.0.1", port, "claude-peer", CHANNEL)  # -> peer_agent
        claude_peer.start(wait=6)
        time.sleep(0.4)

        chan = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "bridge", "channel_bridge.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
            env={**os.environ,
                 "COMMS_IRC_HOST": "127.0.0.1",
                 "COMMS_IRC_PORT": str(port),
                 "COMMS_NICK": "claude-chan",
                 "COMMS_NICK_EXACT": "1",
                 "COMMS_AUTOCONNECT": "1",
                 "COMMS_CHANNEL": CHANNEL},
        )

        def drain_stderr():
            for line in chan.stderr:
                stderr_tail.append(line.rstrip())
                if len(stderr_tail) > 40:
                    del stderr_tail[0]
        threading.Thread(target=drain_stderr, daemon=True).start()

        cli = RawClient(chan)

        # 1) initialize
        init_id = cli.send("initialize", {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "channel-test", "version": "0"},
        })
        init = cli.wait_id(init_id, timeout=12)
        result = (init or {}).get("result", {})
        caps = result.get("capabilities", {})
        exp = caps.get("experimental") or {}
        check("initialize handshake returns a result", bool(result))
        check("advertises experimental 'claude/channel' capability", "claude/channel" in exp)
        check("instructions frame channel content as UNTRUSTED",
              "UNTRUSTED" in (result.get("instructions") or "").upper())
        cli.send("notifications/initialized", {}, notify=True)

        # 2) tools/list
        tl_id = cli.send("tools/list", {})
        tl = cli.wait_id(tl_id, timeout=8)
        names = {t["name"] for t in (tl or {}).get("result", {}).get("tools", [])}
        expected = {"comms_send", "comms_read", "comms_peers", "comms_doctor",
                    "comms_serve", "comms_connect", "comms_disconnect",
                    "comms_join", "comms_whoami"}
        check(f"tools/list exposes the comms_* surface {sorted(names)}", expected <= names)

        # 3) wait until the channel has joined the hub (so it receives new traffic)
        connected = False
        for _ in range(30):
            wid = cli.send("tools/call", {"name": "comms_whoami", "arguments": {}})
            w = cli.wait_id(wid, timeout=4)
            sc = (w or {}).get("result", {}).get("structuredContent", {})
            if sc.get("connected") is True:
                connected = True
                break
            time.sleep(0.3)
        check("channel server joined the hub", connected)

        # 4) bob (claims-human) speaks -> a native channel event, framed untrusted
        bob.send("where are you two at?")
        ev = cli.wait_for(lambda o: o.get("method") == CHANNEL_METHOD
                          and "bob" in json.dumps(o.get("params", {})), timeout=10)
        params = (ev or {}).get("params", {})
        meta = params.get("meta", {})
        check("inbound IRC becomes a notifications/claude/channel event", bool(ev))
        check("event content carries the inline UNTRUSTED marker",
              "UNTRUSTED" in (params.get("content") or "").upper())
        check("event meta.trust == 'untrusted'", meta.get("trust") == "untrusted")
        check("event meta.nick == 'bob'", meta.get("nick") == "bob")
        check("non-claude nick classified kind=claims_human", meta.get("kind") == "claims_human")

        # 5) a claude-* peer -> kind=peer_agent
        claude_peer.send("on the auth refactor")
        ev2 = cli.wait_for(lambda o: o.get("method") == CHANNEL_METHOD
                           and "claude-peer" in json.dumps(o.get("params", {})), timeout=10)
        check("claude-* nick classified kind=peer_agent",
              ((ev2 or {}).get("params", {}).get("meta", {}) or {}).get("kind") == "peer_agent")

        # 6) two-way: comms_send tool reaches bob
        sid = cli.send("tools/call", {"name": "comms_send",
                                      "arguments": {"text": "in the channel — go"}})
        cli.wait_id(sid, timeout=6)
        time.sleep(0.6)
        bmsgs = [m["text"] for m in bob.read()[0]]
        check(f"comms_send (two-way reply) reaches bob {bmsgs}", "in the channel — go" in bmsgs)

    finally:
        for c in (bob, claude_peer):
            if c:
                c.stop()
        if chan:
            chan.terminate()
            try:
                chan.wait(timeout=3)
            except subprocess.TimeoutExpired:
                chan.kill()
        server_proc.terminate()
        try:
            server_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server_proc.kill()

    print()
    if failed:
        print("RESULT: FAILED")
        if stderr_tail:
            print("--- channel_bridge stderr (tail) ---")
            print("\n".join(stderr_tail))
        return 1
    print("RESULT: ALL PASS — channel protocol + untrusted framing + two-way reply")
    return 0


if __name__ == "__main__":
    sys.exit(main())

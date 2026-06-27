"""
mcp_smoke.py — exercise the MCP bridge exactly the way Claude Code will.

Boots an IRC server + a simulated peer (raw IRCClient 'bob'), then launches
comms_bridge.py as a real stdio MCP server (nick 'claude-ryan') and drives it
through the MCP protocol: initialize -> list_tools -> call tools.

Checks: handshake works, all comms_* tools are present, whoami shows linked,
peers sees bob, read receives bob's message, send reaches bob.

Run:  python scripts/mcp_smoke.py
"""

import asyncio
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

from irc_client import IRCClient                      # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client             # noqa: E402

CHANNEL = "#project"
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


def text_of(result):
    """Pull the JSON/text payload out of a CallToolResult."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for c in result.content:
        if getattr(c, "text", None) is not None:
            try:
                return json.loads(c.text)
            except Exception:
                return c.text
    return None


async def run():
    port = free_port()
    server_proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "ircd.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    bob = None
    try:
        if not wait_port("127.0.0.1", port):
            print("[FAIL] server did not start")
            return 1

        # simulated peer session
        bob = IRCClient("127.0.0.1", port, "bob", CHANNEL)
        bob.start(wait=6)
        time.sleep(0.3)

        params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.join(ROOT, "bridge", "comms_bridge.py")],
            env={**os.environ,
                 "COMMS_IRC_HOST": "127.0.0.1",
                 "COMMS_IRC_PORT": str(port),
                 "COMMS_NICK": "claude-ryan",
                 "COMMS_NICK_EXACT": "1",            # deterministic nick for the assertion
                 "COMMS_STATE_DIR": tempfile.mkdtemp(prefix="comms_smoke_"),
                 "COMMS_CHANNEL": CHANNEL},
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                check("MCP initialize handshake", True)

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                expected = {"comms_send", "comms_read", "comms_peers",
                            "comms_join", "comms_whoami"}
                check(f"all tools present {sorted(names)}", expected <= names)

                who = text_of(await session.call_tool("comms_whoami", {}))
                check("whoami: linked to IRC", bool(who) and who.get("connected") is True)
                check("whoami: correct nick", who.get("nick") == "claude-ryan")

                time.sleep(0.4)
                peers = text_of(await session.call_tool("comms_peers", {}))
                check("peers sees bob", "bob" in peers.get("peers", []))

                # bob -> bridge
                bob.send("hello from bob")
                time.sleep(0.6)
                read_res = text_of(await session.call_tool("comms_read", {}))
                got = [m["text"] for m in read_res.get("messages", [])]
                check(f"comms_read receives bob's msg {got}", "hello from bob" in got)

                # bridge -> bob
                await session.call_tool("comms_send", {"text": "hello from ryan"})
                time.sleep(0.6)
                bmsgs, _ = bob.read()
                btexts = [m["text"] for m in bmsgs]
                check(f"comms_send reaches bob {btexts}", "hello from ryan" in btexts)
    finally:
        if bob:
            bob.stop()
        server_proc.terminate()
        try:
            server_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server_proc.kill()

    print()
    if failed:
        print("RESULT: FAILED")
        return 1
    print("RESULT: ALL PASS — MCP bridge works over stdio")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

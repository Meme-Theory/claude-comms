"""
tools_test.py — exercise the segregated, tool-driven connection model over MCP.

Launches the bridge with autoconnect OFF (proving the MCP server starts with no
IRC hub up), then drives it purely through tools:
  comms_doctor (down) -> comms_serve (embedded hub) -> comms_doctor (up)
  -> a peer connects -> comms_send/comms_read both directions -> comms_peers
  -> comms_disconnect -> comms_doctor (down again)

Run:  python scripts/tools_test.py
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bridge"))

from irc_client import IRCClient                      # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client             # noqa: E402

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


def payload(result):
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        # FastMCP wraps a scalar (str) return as {"result": <value>}; unwrap it.
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    for c in result.content:
        if getattr(c, "text", None) is not None:
            try:
                return json.loads(c.text)
            except Exception:
                return c.text
    return None


async def run():
    port = free_port()
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "bridge", "comms_bridge.py")],
        env={**os.environ,
             "COMMS_IRC_HOST": "127.0.0.1",
             "COMMS_IRC_PORT": str(port),
             "COMMS_NICK": "claude-host",
             "COMMS_NICK_EXACT": "1",
             "COMMS_AUTOCONNECT": "0",         # prove server starts with no hub
             "COMMS_STATE_DIR": tempfile.mkdtemp(prefix="comms_tools_"),
             "COMMS_CHANNEL": "#project"},
    )
    peer = None
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                tools = {t.name for t in (await session.list_tools()).tools}
                expected = {"comms_doctor", "comms_serve", "comms_connect",
                            "comms_disconnect", "comms_send", "comms_read",
                            "comms_peers", "comms_join", "comms_whoami"}
                check(f"all tools present {sorted(tools)}", expected <= tools)

                doc = payload(await session.call_tool("comms_doctor", {}))
                check("server boots with NO hub (mcp_ok, not connected)",
                      doc.get("mcp_ok") is True and doc.get("connected") is False)

                served = payload(await session.call_tool("comms_serve", {"port": port}))
                check(f"comms_serve stands up an embedded hub [{served}]",
                      isinstance(served, str) and "listening" in served)

                doc2 = payload(await session.call_tool("comms_doctor", {}))
                check("comms_doctor now reports connected + embedded server",
                      doc2.get("connected") is True and bool(doc2.get("embedded_server")))

                # a peer session joins the embedded hub
                peer = IRCClient("127.0.0.1", port, "peer", "#project")
                peer.start(wait=6)
                time.sleep(0.6)

                peer.send("hi host, this is peer")
                time.sleep(0.5)
                rd = payload(await session.call_tool("comms_read", {}))
                got = [m["text"] for m in rd.get("messages", [])]
                check(f"comms_read receives peer msg {got}", "hi host, this is peer" in got)

                await session.call_tool("comms_send", {"text": "hi peer, this is host"})
                time.sleep(0.5)
                pm = [m["text"] for m in peer.read()[0]]
                check(f"comms_send reaches peer {pm}", "hi peer, this is host" in pm)

                pr = payload(await session.call_tool("comms_peers", {}))
                check(f"comms_peers sees both {pr.get('peers')}",
                      "peer" in pr.get("peers", []) and "claude-host" in pr.get("peers", []))

                await session.call_tool("comms_disconnect", {})
                time.sleep(0.4)
                doc3 = payload(await session.call_tool("comms_doctor", {}))
                check("comms_disconnect drops the link", doc3.get("connected") is False)
    finally:
        if peer:
            peer.stop()

    print()
    if failed:
        print("RESULT: FAILED")
        return 1
    print("RESULT: ALL PASS — segregated tool-driven connection works")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

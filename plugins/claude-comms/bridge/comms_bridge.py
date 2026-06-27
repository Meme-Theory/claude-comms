# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.0"]
# ///
"""
comms_bridge.py — FastMCP front-end bridging a Claude Code session onto a shared
IRC channel, with a SEGREGATED connection lifecycle.

The tool logic lives in comms_core.Comms (shared with channel_bridge.py so the
two front-ends cannot drift); this file is the thin FastMCP wrapper. Delivery of
inbound messages is via state/<session_id>/inbox.jsonl + the PostToolUse /
UserPromptSubmit hook. (For native push delivery, see channel_bridge.py.)

Design (robustness):
  * The MCP server starts INSTANTLY and never depends on IRC being up, so it
    cannot fail to launch just because a hub is missing. (The launcher guarantees
    the `mcp` dependency, so the process itself won't die with -32000 either.)
  * The IRC connection is driven by tools, not pinned at startup:
      - comms_doctor()            validate deps/identity/reachability/connection
      - comms_serve(port, host)   stand up an embedded IRC hub on a specific port
      - comms_connect(host, port) point this session at a specific server/port
      - comms_disconnect()        drop the link (reconnect later)
    A missing server becomes a *status you can read and fix live*, not a fatal
    reconnect error.

Config via env (ALL optional — tools override at runtime):
  COMMS_IRC_HOST (127.0.0.1)   COMMS_IRC_PORT (6667)
  COMMS_CHANNEL (#project)     COMMS_NICK (auto from session id)
  COMMS_AUTOCONNECT (1)        COMMS_STATE_DIR   COMMS_NICK_EXACT   COMMS_PASS

stdout is the MCP JSON-RPC channel; everything here logs to stderr.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)   # import comms_core

from mcp.server.fastmcp import FastMCP  # noqa: E402
from comms_core import Comms            # noqa: E402

mcp = FastMCP("claude-comms")
core = Comms(want_inbox=True)


# ---- messaging ------------------------------------------------------------

@mcp.tool()
def comms_send(text: str, channel: str = "") -> str:
    """Send a message to peer session(s) on the channel (or a specific #channel/nick).
    Multi-line text is sent as multiple lines."""
    return core.send(text, channel)


@mcp.tool()
def comms_read(since: int | None = None) -> dict:
    """Pull new peer messages. Without `since`, returns everything since your last
    read and marks it read. Returns {messages, cursor, count}.
    (With the delivery hook installed, messages also arrive automatically.)"""
    return core.read(since)


@mcp.tool()
def comms_peers(channel: str = "") -> dict:
    """List who is currently present in the channel (other connected sessions)."""
    return core.peers(channel)


# ---- connection lifecycle (segregated from MCP server startup) ------------

@mcp.tool()
def comms_doctor() -> dict:
    """Validate the comms setup end to end: deps, identity, whether an IRC hub is
    reachable on the target port, connection status, peers — and advise next step.
    Use this first when anything seems off."""
    return core.doctor()


@mcp.tool()
def comms_serve(port: int = 6667, host: str = "127.0.0.1", connect: bool = True,
                password: str = "") -> str:
    """Stand up an embedded IRC hub on a specific port — no separate process needed.
    Use host='0.0.0.0' to accept peers from other machines. Set `password` (or
    COMMS_PASS) to require a shared passphrase — only holders can connect (a closed
    trust group). By default this session also connects to the new hub."""
    return core.serve(port, host, connect, password)


@mcp.tool()
def comms_connect(host: str, port: int, channel: str = "", nick: str = "",
                  password: str = "") -> str:
    """Point this session at a specific IRC hub (host/port), optionally changing
    channel/nick. If the hub is passphrase-gated, pass `password` explicitly.
    (COMMS_PASS is NOT auto-sent to arbitrary hosts, to avoid leaking your secret.)
    Validates reachability first."""
    return core.connect(host, port, channel, nick, password)


@mcp.tool()
def comms_disconnect() -> str:
    """Leave the current net: disconnect this session, and if it is hosting an
    embedded hub, stop that hub too. Frees you to shift to a different net
    mid-session via comms_serve(port[, password]) or comms_connect(host, port)."""
    return core.disconnect()


@mcp.tool()
def comms_join(channel: str) -> str:
    """Join an additional channel/room."""
    return core.join(channel)


@mcp.tool()
def comms_whoami() -> dict:
    """This bridge's identity and link status."""
    return core.whoami()


def main():
    core.start()
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()

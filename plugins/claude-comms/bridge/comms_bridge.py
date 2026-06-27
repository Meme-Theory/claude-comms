# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.0"]
# ///
"""
comms_bridge.py — FastMCP server bridging a Claude Code session onto a shared
IRC channel, with a SEGREGATED connection lifecycle.

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
  COMMS_AUTOCONNECT (1)        COMMS_STATE_DIR   COMMS_NICK_EXACT

stdout is the MCP JSON-RPC channel; everything here logs to stderr.
"""

import glob
import os
import secrets
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)                            # import irc_client
sys.path.insert(0, os.path.join(_ROOT, "server"))   # import ircd (embedded hub)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from irc_client import IRCClient        # noqa: E402
import ircd as ircd_mod                 # noqa: E402


def _truthy(v):
    return str(v).lower() in ("1", "true", "yes", "on")


def _int_env(key, default):
    """Parse an int env var, falling back to default on empty/non-numeric so the
    bridge never crashes at import (which would surface as a -32000)."""
    try:
        return int(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return int(default)


def _session_id():
    """Stable id for THIS Claude Code session so the bridge self-names uniquely:
      1) CLAUDE_CODE_SESSION_ID / CLAUDE_SESSION_ID (set for subprocesses)
      2) newest .jsonl in this project's ~/.claude/projects/<encoded> dir
      3) a random token
    Returns (id, source)."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        return sid, "env"
    try:
        encoded = os.getcwd().replace(":", "-").replace("\\", "-").replace("/", "-")
        pat = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded, "*.jsonl")
        files = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
        if files:
            return os.path.splitext(os.path.basename(files[0]))[0], "fs"
    except Exception:
        pass
    return secrets.token_hex(8), "random"


SESSION_ID, SID_SOURCE = _session_id()


def _derive_nick():
    """nick = <base>-<6-char session token>, unique per session even if the
    config is copied verbatim. Set COMMS_NICK_EXACT=1 to use the base as-is."""
    base = os.environ.get("COMMS_NICK") or os.environ.get("COMMS_NICK_PREFIX") or "claude"
    if _truthy(os.environ.get("COMMS_NICK_EXACT")):
        return base
    token = SESSION_ID.replace("-", "")[:6] or secrets.token_hex(3)
    return f"{base}-{token}"


def _state_dir():
    """Per-session state dir shared with the delivery hook. Prefers
    CLAUDE_PLUGIN_DATA (persists across plugin updates, writable); both the
    bridge and the hook derive the same path from the session id."""
    base = (os.environ.get("COMMS_STATE_DIR")
            or (os.path.join(os.environ["CLAUDE_PLUGIN_DATA"], "state")
                if os.environ.get("CLAUDE_PLUGIN_DATA") else None)
            or os.path.join(_ROOT, "state"))
    d = os.path.abspath(os.path.join(base, SESSION_ID))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


HOST = os.environ.get("COMMS_IRC_HOST", "127.0.0.1")
PORT = _int_env("COMMS_IRC_PORT", 6667)
CHANNEL = os.environ.get("COMMS_CHANNEL", "#project")
PASS = os.environ.get("COMMS_PASS") or None
NICK = _derive_nick()
STATE_DIR = _state_dir()
INBOX_FILE = os.path.join(STATE_DIR, "inbox.jsonl")


def _log(*a):
    print("[comms_bridge]", *a, file=sys.stderr, flush=True)


def _tcp_reachable(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_connected(seconds=2.0):
    end = time.time() + seconds
    while time.time() < end:
        if client.is_connected():
            return True
        time.sleep(0.1)
    return client.is_connected()


mcp = FastMCP("claude-comms")
client = IRCClient(HOST, PORT, NICK, CHANNEL, inbox_file=INBOX_FILE, password=PASS)
_embedded = {"running": False, "addr": None, "gated": False, "stop": None}


# ---- messaging ------------------------------------------------------------

@mcp.tool()
def comms_send(text: str, channel: str = "") -> str:
    """Send a message to peer session(s) on the channel (or a specific #channel/nick).
    Multi-line text is sent as multiple lines."""
    target = channel or client.channel
    if not client.is_connected():
        return ("NOT CONNECTED. Run comms_doctor to diagnose, then comms_serve(port) "
                "to host a hub here or comms_connect(host, port) to join one.")
    try:
        n = client.send(text, target)
    except Exception as e:
        return f"send failed (link dropped mid-send?): {e}. Run comms_doctor."
    pending, _ = client.read(mark_read=False)  # peek without consuming, to nudge a read
    tip = (f" {len(pending)} unread message(s) waiting — call comms_read now."
           if pending else " Reminder: call comms_read shortly to catch replies.")
    return f"sent {n} line(s) to {target} as {client.nick}.{tip}"


@mcp.tool()
def comms_read(since: int | None = None) -> dict:
    """Pull new peer messages. Without `since`, returns everything since your last
    read and marks it read. Returns {messages, cursor, count}.
    (With the delivery hook installed, messages also arrive automatically.)"""
    msgs, cursor = client.read(since=since)
    return {"messages": msgs, "cursor": cursor, "count": len(msgs)}


@mcp.tool()
def comms_peers(channel: str = "") -> dict:
    """List who is currently present in the channel (other connected sessions)."""
    ch = channel or client.channel
    return {"channel": ch, "peers": client.peers(ch), "me": client.nick}


# ---- connection lifecycle (segregated from MCP server startup) ------------

@mcp.tool()
def comms_doctor() -> dict:
    """Validate the comms setup end to end: deps, identity, whether an IRC hub is
    reachable on the target port, connection status, peers — and advise next step.
    Use this first when anything seems off."""
    reachable = _tcp_reachable(client.host, client.port)
    connected = client.is_connected()
    peers = client.peers() if connected else []
    report = {
        "mcp_ok": True,
        "nick": client.nick,
        "session_id": SESSION_ID,
        "channel": client.channel,
        "target": f"{client.host}:{client.port}",
        "irc_server_reachable": reachable,
        "connected": connected,
        "auth": "failed" if client.auth_failed() else ("on" if client.password else "off"),
        "embedded_server": _embedded["addr"] if _embedded["running"] else None,
        "peers": peers,
        "state_dir": STATE_DIR,
    }
    if client.auth_failed():
        report["advice"] = ("Rejected: wrong/missing passphrase. Reconnect with "
                            "comms_connect(host, port, password=...) or set COMMS_PASS.")
    elif connected:
        report["advice"] = f"Healthy — {len(peers)} present on {client.channel}."
    elif reachable:
        report["advice"] = ("Hub reachable but not joined yet — wait a moment or call "
                            "comms_connect to (re)join.")
    else:
        report["advice"] = (f"No IRC hub at {client.host}:{client.port}. On the hub machine "
                            f"call comms_serve({client.port}); elsewhere call "
                            f"comms_connect('<hub-host>', {client.port}).")
    return report


@mcp.tool()
def comms_serve(port: int = 6667, host: str = "127.0.0.1", connect: bool = True,
                password: str = "") -> str:
    """Stand up an embedded IRC hub on a specific port — no separate process needed.
    Use host='0.0.0.0' to accept peers from other machines. Set `password` (or
    COMMS_PASS) to require a shared passphrase — only holders can connect (a closed
    trust group). By default this session also connects to the new hub."""
    pw = (password or PASS) or None
    if _embedded["running"]:
        cur = "passphrase-gated" if _embedded.get("gated") else "OPEN (no passphrase)"
        if pw and not _embedded.get("gated"):
            return (f"already serving on {_embedded['addr']} as {cur}. A hub's passphrase "
                    f"is fixed at startup — call comms_disconnect to stop it, then "
                    f"comms_serve({port}, password=...) to bring up the gated hub.")
        msg = (f"already serving on {_embedded['addr']} ({cur}); "
               f"call comms_disconnect first to move to a different hub")
        return msg if not connect else msg + f"; connected={client.is_connected()}"
    ok, info, stop = ircd_mod.serve_in_thread(host, int(port), password=pw)
    if not ok:
        return (f"could not start a hub on {host}:{port} -> {info}. "
                f"If the port is in use a hub may already be running — try "
                f"comms_connect('127.0.0.1', {port}).")
    _embedded["running"] = True
    _embedded["addr"] = info
    _embedded["gated"] = bool(pw)
    _embedded["stop"] = stop
    out = f"IRC hub listening on {info}{' (passphrase-gated)' if pw else ''}."
    if connect:
        dial = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        client.reconfigure(host=dial, port=int(port), password=pw)
        ok2 = _wait_connected(3.0)
        out += f" This session {'connected' if ok2 else 'is connecting'} as {client.nick}."
    return out


@mcp.tool()
def comms_connect(host: str, port: int, channel: str = "", nick: str = "",
                  password: str = "") -> str:
    """Point this session at a specific IRC hub (host/port), optionally changing
    channel/nick. If the hub is passphrase-gated, pass `password` explicitly.
    (COMMS_PASS is NOT auto-sent to arbitrary hosts, to avoid leaking your secret.)
    Validates reachability first."""
    if not _tcp_reachable(host, port):
        return (f"{host}:{port} is not reachable. Is a hub running there? On that "
                f"machine run comms_serve({port}, host='0.0.0.0').")
    pw = password or None
    client.reconfigure(host=host, port=int(port), channel=channel or None,
                       nick=nick or None, password=pw)
    ok = _wait_connected(3.0)
    if not ok and client.auth_failed():
        return f"REJECTED by {host}:{port}: wrong or missing passphrase."
    return (f"{'connected to' if ok else 'connecting to'} {host}:{port} "
            f"as {client.nick} on {client.channel}")


@mcp.tool()
def comms_disconnect() -> str:
    """Leave the current net: disconnect this session, and if it is hosting an
    embedded hub, stop that hub too. Frees you to shift to a different net
    mid-session via comms_serve(port[, password]) or comms_connect(host, port)."""
    client.pause()
    extra = ""
    if _embedded["running"]:
        fn = _embedded.get("stop")
        ok = bool(fn and fn())
        extra = (f" Stopped the embedded hub on {_embedded['addr']}." if ok
                 else f" (Embedded hub on {_embedded['addr']} did not stop cleanly.)")
        _embedded.update({"running": False, "addr": None, "gated": False, "stop": None})
    return ("disconnected." + extra +
            " Start fresh with comms_serve(port[, password]) or comms_connect(host, port).")


@mcp.tool()
def comms_join(channel: str) -> str:
    """Join an additional channel/room."""
    if not client.is_connected():
        return "NOT CONNECTED — connect first (run comms_doctor for help)."
    try:
        client.join(channel)
    except Exception as e:
        return f"join failed (link dropped?): {e}. Run comms_doctor."
    return f"joined {channel}"


@mcp.tool()
def comms_whoami() -> dict:
    """This bridge's identity and link status."""
    return {
        "nick": client.nick,
        "channel": client.channel,
        "server": f"{client.host}:{client.port}",
        "connected": client.is_connected(),
        "auth": "failed" if client.auth_failed() else ("on" if client.password else "off"),
        "session_id": SESSION_ID,
        "nick_source": SID_SOURCE,
        "state_dir": STATE_DIR,
        "embedded_server": _embedded["addr"] if _embedded["running"] else None,
    }


def main():
    _log(f"starting: nick={NICK} sid={SID_SOURCE} target={HOST}:{PORT} channel={CHANNEL}")
    if SID_SOURCE == "random":
        _log("WARNING: no session id from env or filesystem; using a RANDOM id. The "
             "delivery hook may not find this inbox (check comms_whoami.nick_source). "
             "Ensure CLAUDE_CODE_SESSION_ID is inherited by the MCP server.")
    # Non-blocking: the MCP server is up instantly; IRC connects in the background.
    client.start(wait=0)
    if not _truthy(os.environ.get("COMMS_AUTOCONNECT", "1")):
        client.pause()
        _log("autoconnect disabled; idle until comms_serve/comms_connect")
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()

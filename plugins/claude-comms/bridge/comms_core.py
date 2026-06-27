"""
comms_core.py — shared connection + tool logic for the ClaudeComms bridges.

Both front-ends import this so their tool behaviour cannot drift:
  * comms_bridge.py    — FastMCP server; delivery via the inbox.jsonl + hook path.
  * channel_bridge.py  — low-level MCP "channel" server (research preview);
                         delivery via native notifications/claude/channel events.

The Comms class owns identity (self-naming from the session id), the IRC client
(the transport seam) and the embedded-hub bookkeeping. Every method returns
exactly what the matching comms_* tool returns (str or dict), so each front-end
is a thin wrapper and the two cannot disagree.

NOTHING here prints to stdout — that belongs to the MCP JSON-RPC channel. Logs
go to stderr.
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

from irc_client import IRCClient   # noqa: E402
import ircd as ircd_mod            # noqa: E402


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


def _derive_nick(session_id):
    """nick = <base>-<6-char session token>, unique per session even if the
    config is copied verbatim. Set COMMS_NICK_EXACT=1 to use the base as-is."""
    base = os.environ.get("COMMS_NICK") or os.environ.get("COMMS_NICK_PREFIX") or "claude"
    if _truthy(os.environ.get("COMMS_NICK_EXACT")):
        return base
    token = session_id.replace("-", "")[:6] or secrets.token_hex(3)
    return f"{base}-{token}"


def _state_dir(session_id):
    """Per-session state dir shared with the delivery hook. Prefers
    CLAUDE_PLUGIN_DATA (persists across plugin updates, writable); both the
    bridge and the hook derive the same path from the session id."""
    base = (os.environ.get("COMMS_STATE_DIR")
            or (os.path.join(os.environ["CLAUDE_PLUGIN_DATA"], "state")
                if os.environ.get("CLAUDE_PLUGIN_DATA") else None)
            or os.path.join(_ROOT, "state"))
    d = os.path.abspath(os.path.join(base, session_id))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


class Comms:
    """Owns the IRC link + embedded-hub state and implements every comms_* tool.

    want_inbox=False suppresses the inbox.jsonl write-through. The channel
    front-end uses that: it delivers via native channel events, so the hook must
    stay silent and not double-deliver the same message.
    """

    def __init__(self, want_inbox=True):
        self.session_id, self.sid_source = _session_id()
        self.nick = _derive_nick(self.session_id)
        self.state_dir = _state_dir(self.session_id)
        self.inbox_file = os.path.join(self.state_dir, "inbox.jsonl") if want_inbox else None
        self.host = os.environ.get("COMMS_IRC_HOST", "127.0.0.1")
        self.port = _int_env("COMMS_IRC_PORT", 6667)
        self.channel = os.environ.get("COMMS_CHANNEL", "#project")
        self.password = os.environ.get("COMMS_PASS") or None
        self.client = IRCClient(self.host, self.port, self.nick, self.channel,
                                inbox_file=self.inbox_file, password=self.password)
        self.embedded = {"running": False, "addr": None, "gated": False, "stop": None}

    # ---- helpers ---------------------------------------------------------

    def log(self, *a):
        print("[comms]", *a, file=sys.stderr, flush=True)

    def _tcp_reachable(self, host, port, timeout=1.0):
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            return False

    def _wait_connected(self, seconds=2.0):
        end = time.time() + seconds
        while time.time() < end:
            if self.client.is_connected():
                return True
            time.sleep(0.1)
        return self.client.is_connected()

    def start(self):
        """Non-blocking startup: the MCP server is up instantly; IRC connects in
        the background (autoconnect on unless COMMS_AUTOCONNECT=0)."""
        self.log(f"starting: nick={self.nick} sid={self.sid_source} "
                 f"target={self.host}:{self.port} channel={self.channel}")
        if self.sid_source == "random":
            self.log("WARNING: no session id from env or filesystem; using a RANDOM id. "
                     "The delivery hook may not find this inbox (check comms_whoami.nick_source). "
                     "Ensure CLAUDE_CODE_SESSION_ID is inherited by the MCP server.")
        self.client.start(wait=0)
        if not _truthy(os.environ.get("COMMS_AUTOCONNECT", "1")):
            self.client.pause()
            self.log("autoconnect disabled; idle until comms_serve/comms_connect")

    # ---- messaging -------------------------------------------------------

    def send(self, text, channel=""):
        target = channel or self.client.channel
        if not self.client.is_connected():
            return ("NOT CONNECTED. Run comms_doctor to diagnose, then comms_serve(port) "
                    "to host a hub here or comms_connect(host, port) to join one.")
        try:
            n = self.client.send(text, target)
        except Exception as e:
            return f"send failed (link dropped mid-send?): {e}. Run comms_doctor."
        pending, _ = self.client.read(mark_read=False)  # peek without consuming, to nudge a read
        tip = (f" {len(pending)} unread message(s) waiting — call comms_read now."
               if pending else " Reminder: call comms_read shortly to catch replies.")
        return f"sent {n} line(s) to {target} as {self.client.nick}.{tip}"

    def read(self, since=None):
        msgs, cursor = self.client.read(since=since)
        return {"messages": msgs, "cursor": cursor, "count": len(msgs)}

    def peers(self, channel=""):
        ch = channel or self.client.channel
        return {"channel": ch, "peers": self.client.peers(ch), "me": self.client.nick}

    # ---- connection lifecycle (segregated from MCP server startup) -------

    def doctor(self):
        c = self.client
        reachable = self._tcp_reachable(c.host, c.port)
        connected = c.is_connected()
        peers = c.peers() if connected else []
        report = {
            "mcp_ok": True,
            "nick": c.nick,
            "session_id": self.session_id,
            "channel": c.channel,
            "target": f"{c.host}:{c.port}",
            "irc_server_reachable": reachable,
            "connected": connected,
            "auth": "failed" if c.auth_failed() else ("on" if c.password else "off"),
            "embedded_server": self.embedded["addr"] if self.embedded["running"] else None,
            "peers": peers,
            "state_dir": self.state_dir,
        }
        if c.auth_failed():
            report["advice"] = ("Rejected: wrong/missing passphrase. Reconnect with "
                                "comms_connect(host, port, password=...) or set COMMS_PASS.")
        elif connected:
            report["advice"] = f"Healthy — {len(peers)} present on {c.channel}."
        elif reachable:
            report["advice"] = ("Hub reachable but not joined yet — wait a moment or call "
                                "comms_connect to (re)join.")
        else:
            report["advice"] = (f"No IRC hub at {c.host}:{c.port}. On the hub machine "
                                f"call comms_serve({c.port}); elsewhere call "
                                f"comms_connect('<hub-host>', {c.port}).")
        return report

    def serve(self, port=6667, host="127.0.0.1", connect=True, password=""):
        c = self.client
        pw = (password or self.password) or None
        if self.embedded["running"]:
            cur = "passphrase-gated" if self.embedded.get("gated") else "OPEN (no passphrase)"
            if pw and not self.embedded.get("gated"):
                return (f"already serving on {self.embedded['addr']} as {cur}. A hub's passphrase "
                        f"is fixed at startup — call comms_disconnect to stop it, then "
                        f"comms_serve({port}, password=...) to bring up the gated hub.")
            msg = (f"already serving on {self.embedded['addr']} ({cur}); "
                   f"call comms_disconnect first to move to a different hub")
            return msg if not connect else msg + f"; connected={c.is_connected()}"
        ok, info, stop = ircd_mod.serve_in_thread(host, int(port), password=pw)
        if not ok:
            return (f"could not start a hub on {host}:{port} -> {info}. "
                    f"If the port is in use a hub may already be running — try "
                    f"comms_connect('127.0.0.1', {port}).")
        self.embedded["running"] = True
        self.embedded["addr"] = info
        self.embedded["gated"] = bool(pw)
        self.embedded["stop"] = stop
        out = f"IRC hub listening on {info}{' (passphrase-gated)' if pw else ''}."
        if connect:
            dial = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
            c.reconfigure(host=dial, port=int(port), password=pw)
            ok2 = self._wait_connected(3.0)
            out += f" This session {'connected' if ok2 else 'is connecting'} as {c.nick}."
        return out

    def connect(self, host, port, channel="", nick="", password=""):
        c = self.client
        if not self._tcp_reachable(host, port):
            return (f"{host}:{port} is not reachable. Is a hub running there? On that "
                    f"machine run comms_serve({port}, host='0.0.0.0').")
        pw = password or None
        c.reconfigure(host=host, port=int(port), channel=channel or None,
                      nick=nick or None, password=pw)
        ok = self._wait_connected(3.0)
        if not ok and c.auth_failed():
            return f"REJECTED by {host}:{port}: wrong or missing passphrase."
        return (f"{'connected to' if ok else 'connecting to'} {host}:{port} "
                f"as {c.nick} on {c.channel}")

    def disconnect(self):
        c = self.client
        c.pause()
        extra = ""
        if self.embedded["running"]:
            fn = self.embedded.get("stop")
            ok = bool(fn and fn())
            extra = (f" Stopped the embedded hub on {self.embedded['addr']}." if ok
                     else f" (Embedded hub on {self.embedded['addr']} did not stop cleanly.)")
            self.embedded.update({"running": False, "addr": None, "gated": False, "stop": None})
        return ("disconnected." + extra +
                " Start fresh with comms_serve(port[, password]) or comms_connect(host, port).")

    def join(self, channel):
        if not self.client.is_connected():
            return "NOT CONNECTED — connect first (run comms_doctor for help)."
        try:
            self.client.join(channel)
        except Exception as e:
            return f"join failed (link dropped?): {e}. Run comms_doctor."
        return f"joined {channel}"

    def whoami(self):
        c = self.client
        return {
            "nick": c.nick,
            "channel": c.channel,
            "server": f"{c.host}:{c.port}",
            "connected": c.is_connected(),
            "auth": "failed" if c.auth_failed() else ("on" if c.password else "off"),
            "session_id": self.session_id,
            "nick_source": self.sid_source,
            "state_dir": self.state_dir,
            "embedded_server": self.embedded["addr"] if self.embedded["running"] else None,
        }

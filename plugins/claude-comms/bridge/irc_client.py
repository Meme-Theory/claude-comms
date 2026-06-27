"""
irc_client.py — threaded IRC client used by the ClaudeComms MCP bridge.

This is the *transport seam*. The MCP bridge only depends on the public methods
below (send / read / peers / join / whoami-ish accessors). Swap this class for a
different transport (websocket, redis, plain TCP) implementing the same surface
and the bridge keeps working — that's what "transport-agnostic" buys us.

Design notes:
- Runs the socket loop on a daemon thread; all public methods are thread-safe.
- NOTHING is ever printed to stdout. stdout belongs to the MCP JSON-RPC channel
  when this module is imported by comms_bridge.py. All logs go to stderr.
- Inbound chat is buffered with a monotonic index so pull-based reads can resume
  from a cursor. The server does NOT echo your own PRIVMSG back, so read() only
  ever returns what *peers* said — exactly the semantics we want.
"""

import json
import os
import socket
import sys
import threading
import time


def _log(*a):
    print("[irc_client]", *a, file=sys.stderr, flush=True)


def parse_line(line):
    """Parse one IRC line into (prefix, command, args, trailing)."""
    prefix = None
    trailing = None
    s = line
    if s.startswith(":"):
        prefix, _, s = s[1:].partition(" ")
    if " :" in s:
        s, _, trailing = s.partition(" :")
    elif s.startswith(":"):
        trailing = s[1:]
        s = ""
    parts = s.split()
    cmd = parts[0].upper() if parts else ""
    args = parts[1:]
    return prefix, cmd, args, trailing


def nick_from_prefix(prefix):
    if not prefix:
        return ""
    return prefix.split("!", 1)[0]


def _chunks(s, n):
    return [s[i:i + n] for i in range(0, len(s), n)] if s else []


class IRCClient:
    def __init__(self, host, port, nick, channel, realname=None, max_buffer=2000,
                 inbox_file=None):
        self.host = host
        self.port = int(port)
        self.nick = nick
        self.channel = channel
        self.realname = realname or f"ClaudeComms {nick}"
        self.max_buffer = max_buffer
        self.inbox_file = inbox_file  # write-through log for hook-based delivery

        self._sock = None
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._inbox = []           # list of dicts: idx, ts, from, target, text
        self._idx = 0              # monotonic message counter
        self._cursor = 0           # last index returned by read(mark_read=True)
        self._members = {}         # channel -> set(nick)
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._paused = threading.Event()  # set = stop maintaining the connection
        self._thread = threading.Thread(target=self._run, name=f"irc-{nick}", daemon=True)

    # ---- lifecycle -------------------------------------------------------

    def start(self, wait=8.0):
        self._thread.start()
        if wait:
            self._connected.wait(timeout=wait)
        return self.is_connected()

    def stop(self):
        self._stop.set()
        try:
            self._raw_send("QUIT :bye")
        except Exception:
            pass
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def is_connected(self):
        return self._connected.is_set()

    def _close_sock(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def reconfigure(self, host=None, port=None, channel=None, nick=None):
        """Point at a new server/port/channel/nick and force a reconnect.
        Backs the comms_connect / comms_serve tools (runtime, segregated from
        the MCP server's own startup)."""
        with self._lock:
            if host:
                self.host = host
            if port:
                self.port = int(port)
            if channel:
                self.channel = channel
            if nick:
                self.nick = nick
        self._paused.clear()
        self._connected.clear()
        self._close_sock()
        if not self._thread.is_alive():
            try:
                self._thread.start()
            except RuntimeError:
                pass

    def pause(self):
        """Stop maintaining the connection until reconfigured/resumed (comms_disconnect)."""
        self._paused.set()
        self._connected.clear()
        self._close_sock()

    def resume(self):
        self._paused.clear()

    # ---- socket loop -----------------------------------------------------

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.3)
                backoff = 1.0
                continue
            try:
                self._connect_and_loop()
                backoff = 1.0
            except Exception as e:
                self._connected.clear()
                if self._stop.is_set() or self._paused.is_set():
                    continue
                _log(f"connection error ({e!r}); reconnecting in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _connect_and_loop(self):
        _log(f"connecting to {self.host}:{self.port} as {self.nick}")
        self._sock = socket.create_connection((self.host, self.port), timeout=10)
        self._sock.settimeout(None)
        self._raw_send(f"NICK {self.nick}")
        self._raw_send(f"USER {self.nick} 0 * :{self.realname}")

        buf = b""
        while not self._stop.is_set():
            data = self._sock.recv(4096)
            if not data:
                raise ConnectionError("server closed connection")
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if line:
                    self._handle_line(line)

    def _raw_send(self, line):
        with self._send_lock:
            if not self._sock:
                raise ConnectionError("not connected")
            self._sock.sendall((line + "\r\n").encode("utf-8"))

    def _append_inbox(self, rec):
        """Append one inbound message to the on-disk log the delivery hook reads.
        Best-effort: a failure here must never break the receive loop."""
        if not self.inbox_file:
            return
        try:
            d = os.path.dirname(self.inbox_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.inbox_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            _log("inbox write failed:", e)

    def _handle_line(self, line):
        prefix, cmd, args, trailing = parse_line(line)

        if cmd == "PING":
            token = trailing if trailing is not None else (args[0] if args else "")
            try:
                self._raw_send(f"PONG :{token}")
            except Exception:
                pass
            return

        if cmd == "001":  # RPL_WELCOME — registration complete
            self._raw_send(f"JOIN {self.channel}")
            self._connected.set()
            _log(f"registered; joined {self.channel}")
            return

        if cmd == "433":  # ERR_NICKNAMEINUSE
            self.nick = self.nick + "_"
            _log(f"nick in use; retrying as {self.nick}")
            self._raw_send(f"NICK {self.nick}")
            return

        if cmd == "PRIVMSG":
            sender = nick_from_prefix(prefix)
            target = args[0] if args else ""
            text = trailing if trailing is not None else ""
            with self._lock:
                self._idx += 1
                rec = {
                    "idx": self._idx,
                    "ts": round(time.time(), 3),
                    "from": sender,
                    "target": target,
                    "text": text,
                }
                self._inbox.append(rec)
                if len(self._inbox) > self.max_buffer:
                    self._inbox = self._inbox[-self.max_buffer:]
                self._append_inbox(rec)
            return

        if cmd == "JOIN":
            sender = nick_from_prefix(prefix)
            ch = (args[0] if args else (trailing or "")).strip()
            with self._lock:
                self._members.setdefault(ch, set()).add(sender)
            return

        if cmd == "PART":
            sender = nick_from_prefix(prefix)
            ch = (args[0] if args else (trailing or "")).strip()
            with self._lock:
                self._members.get(ch, set()).discard(sender)
            return

        if cmd == "QUIT":
            sender = nick_from_prefix(prefix)
            with self._lock:
                for members in self._members.values():
                    members.discard(sender)
            return

        if cmd == "NICK":
            old = nick_from_prefix(prefix)
            new = (args[0] if args else (trailing or "")).strip()
            with self._lock:
                for members in self._members.values():
                    if old in members:
                        members.discard(old)
                        members.add(new)
            return

        if cmd == "353":  # RPL_NAMREPLY
            ch = args[-1] if args else self.channel
            names = [n.lstrip("@+%&~") for n in (trailing or "").split()]
            with self._lock:
                self._members.setdefault(ch, set()).update(names)
            return

    # ---- public API (thread-safe) ---------------------------------------

    def send(self, text, channel=None):
        """Send chat. Newlines are split into separate PRIVMSGs; long lines are
        chunked to stay under the IRC line limit. Returns number of lines sent."""
        ch = channel or self.channel
        lines = []
        for raw in str(text).split("\n"):
            line = raw.rstrip("\r")
            pieces = _chunks(line, 400)
            if not pieces:
                continue  # skip blank lines
            lines.extend(pieces)
        if not lines:
            lines = ["(empty message)"]
        for piece in lines:
            self._raw_send(f"PRIVMSG {ch} :{piece}")
        return len(lines)

    def read(self, since=None, mark_read=True):
        """Return (messages, cursor). Without `since`, returns messages newer
        than the internal read-cursor and advances it. With `since`, returns
        messages with idx > since (and still advances the cursor if mark_read)."""
        with self._lock:
            start = self._cursor if since is None else int(since)
            msgs = [dict(m) for m in self._inbox if m["idx"] > start]
            cursor = self._inbox[-1]["idx"] if self._inbox else start
            if mark_read:
                self._cursor = max(self._cursor, cursor)
            return msgs, cursor

    def peers(self, channel=None, refresh=True):
        """Who is present in the channel. Issues NAMES and waits briefly for the
        reply so membership reflects late joiners."""
        ch = channel or self.channel
        if refresh and self.is_connected():
            try:
                self._raw_send(f"NAMES {ch}")
                time.sleep(0.3)
            except Exception:
                pass
        with self._lock:
            return sorted(self._members.get(ch, set()))

    def join(self, channel):
        self._raw_send(f"JOIN {channel}")
        time.sleep(0.2)
        return True

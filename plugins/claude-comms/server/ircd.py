"""
ircd.py — a minimal, dependency-free IRC server for ClaudeComms.

Implements just enough of RFC 1459/2812 to relay messages between bridge
clients (and any real IRC client you point at it for debugging):

  NICK USER JOIN PART PRIVMSG NOTICE PING PONG NAMES WHO QUIT CAP

Binds 127.0.0.1 by default (localhost-only). Pass --host 0.0.0.0 to expose it
on the network for cross-machine sessions. All logging goes to stderr.

Run:
    python server/ircd.py                # localhost:6667
    python server/ircd.py --host 0.0.0.0 --port 6667
"""

import argparse
import asyncio
import sys

SERVER_NAME = "claudecomms"
VERSION = "claudecomms-1"


def log(*a):
    print("[ircd]", *a, file=sys.stderr, flush=True)


def parse_line(line):
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


class Client:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.nick = None
        self.user = None
        self.realname = None
        self.registered = False
        self.channels = set()

    @property
    def host(self):
        peer = self.writer.get_extra_info("peername")
        return peer[0] if peer else "unknown"

    @property
    def hostmask(self):
        return f"{self.nick}!{self.user or self.nick}@{self.host}"

    async def send(self, line):
        try:
            self.writer.write((line + "\r\n").encode("utf-8"))
            await self.writer.drain()
        except Exception:
            pass


class Server:
    def __init__(self, name=SERVER_NAME):
        self.name = name
        self.clients = set()
        self.nicks = {}        # nick -> Client
        self.channels = {}     # channel -> set(Client)

    # ---- connection handling --------------------------------------------

    async def handle(self, reader, writer):
        c = Client(reader, writer)
        self.clients.add(c)
        log(f"connection from {c.host}")
        buf = b""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if line:
                        await self.on_line(c, line)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as e:
            log(f"handler error for {c.nick or c.host}: {e!r}")
        finally:
            await self.disconnect(c)

    async def disconnect(self, c):
        for ch in list(c.channels):
            members = self.channels.get(ch, set())
            members.discard(c)
            for m in members:
                await m.send(f":{c.hostmask} QUIT :Connection closed")
            if not members:
                self.channels.pop(ch, None)
        if c.nick and self.nicks.get(c.nick) is c:
            del self.nicks[c.nick]
        self.clients.discard(c)
        try:
            c.writer.close()
        except Exception:
            pass
        log(f"disconnected {c.nick or c.host}")

    # ---- dispatch --------------------------------------------------------

    async def on_line(self, c, line):
        prefix, cmd, args, trailing = parse_line(line)
        handler = getattr(self, f"cmd_{cmd.lower()}", None)
        if handler is None:
            if c.registered:
                await c.send(f":{self.name} 421 {c.nick} {cmd} :Unknown command")
            return
        await handler(c, args, trailing)

    # ---- registration ----------------------------------------------------

    async def cmd_cap(self, c, args, trailing):
        # Minimal CAP so modern clients don't hang. We advertise nothing.
        sub = (args[0].upper() if args else "")
        if sub == "LS":
            await c.send(f":{self.name} CAP * LS :")
        elif sub == "REQ":
            await c.send(f":{self.name} CAP * NAK :{trailing or ''}")
        elif sub == "LIST":
            await c.send(f":{self.name} CAP * LIST :")
        # END requires no reply

    async def cmd_nick(self, c, args, trailing):
        nick = (args[0] if args else trailing or "").strip()
        if not nick:
            await c.send(f":{self.name} 431 * :No nickname given")
            return
        if nick in self.nicks and self.nicks[nick] is not c:
            await c.send(f":{self.name} 433 {c.nick or '*'} {nick} :Nickname is already in use")
            return
        old = c.nick
        if old and self.nicks.get(old) is c:
            del self.nicks[old]
        c.nick = nick
        self.nicks[nick] = c
        if old and c.registered:
            # broadcast nick change to everyone sharing a channel
            seen = set()
            for ch in c.channels:
                for m in self.channels.get(ch, set()):
                    if m not in seen:
                        await m.send(f":{old}!{c.user}@{c.host} NICK {nick}")
                        seen.add(m)
        await self.try_register(c)

    async def cmd_user(self, c, args, trailing):
        c.user = (args[0] if args else "user")
        c.realname = trailing or "realname"
        await self.try_register(c)

    async def try_register(self, c):
        if c.registered or not (c.nick and c.user):
            return
        c.registered = True
        log(f"registered {c.nick} ({c.host})")
        await c.send(f":{self.name} 001 {c.nick} :Welcome to ClaudeComms, {c.nick}")
        await c.send(f":{self.name} 002 {c.nick} :Your host is {self.name}, running {VERSION}")
        await c.send(f":{self.name} 003 {c.nick} :ClaudeComms relay")
        await c.send(f":{self.name} 004 {c.nick} {self.name} {VERSION} o o")
        await c.send(f":{self.name} 375 {c.nick} :- {self.name} Message of the Day -")
        await c.send(f":{self.name} 372 {c.nick} :- Two Claudes walk into a channel.")
        await c.send(f":{self.name} 376 {c.nick} :End of /MOTD command")

    # ---- channels & messaging -------------------------------------------

    async def cmd_join(self, c, args, trailing):
        if not c.registered:
            return
        chans = (args[0] if args else (trailing or "")).split(",")
        for ch in chans:
            ch = ch.strip()
            if not ch or not ch.startswith(("#", "&")):
                continue
            members = self.channels.setdefault(ch, set())
            if c in members:
                continue
            members.add(c)
            c.channels.add(ch)
            for m in members:
                await m.send(f":{c.hostmask} JOIN {ch}")
            await self._send_names(c, ch)

    async def cmd_part(self, c, args, trailing):
        chans = (args[0] if args else (trailing or "")).split(",")
        for ch in chans:
            ch = ch.strip()
            members = self.channels.get(ch, set())
            if c in members:
                for m in members:
                    await m.send(f":{c.hostmask} PART {ch}")
                members.discard(c)
                c.channels.discard(ch)
                if not members:
                    self.channels.pop(ch, None)

    async def cmd_names(self, c, args, trailing):
        ch = (args[0] if args else (trailing or "")).strip()
        await self._send_names(c, ch)

    async def _send_names(self, c, ch):
        members = self.channels.get(ch, set())
        names = " ".join(sorted(m.nick for m in members if m.nick))
        await c.send(f":{self.name} 353 {c.nick} = {ch} :{names}")
        await c.send(f":{self.name} 366 {c.nick} {ch} :End of /NAMES list")

    async def cmd_who(self, c, args, trailing):
        ch = (args[0] if args else "").strip()
        for m in self.channels.get(ch, set()):
            await c.send(
                f":{self.name} 352 {c.nick} {ch} {m.user} {m.host} {self.name} "
                f"{m.nick} H :0 {m.realname}"
            )
        await c.send(f":{self.name} 315 {c.nick} {ch} :End of /WHO list")

    async def cmd_privmsg(self, c, args, trailing):
        if not c.registered or not args:
            return
        target = args[0]
        text = trailing if trailing is not None else (args[1] if len(args) > 1 else "")
        await self._relay(c, "PRIVMSG", target, text)

    async def cmd_notice(self, c, args, trailing):
        if not c.registered or not args:
            return
        target = args[0]
        text = trailing if trailing is not None else (args[1] if len(args) > 1 else "")
        await self._relay(c, "NOTICE", target, text)

    async def _relay(self, c, kind, target, text):
        line = f":{c.hostmask} {kind} {target} :{text}"
        if target.startswith(("#", "&")):
            for m in self.channels.get(target, set()):
                if m is not c:
                    await m.send(line)
        else:
            dst = self.nicks.get(target)
            if dst:
                await dst.send(line)
            else:
                await c.send(f":{self.name} 401 {c.nick} {target} :No such nick/channel")

    async def cmd_ping(self, c, args, trailing):
        token = trailing if trailing is not None else (args[0] if args else "")
        await c.send(f":{self.name} PONG {self.name} :{token}")

    async def cmd_pong(self, c, args, trailing):
        pass

    async def cmd_quit(self, c, args, trailing):
        await c.send("ERROR :Bye")
        await self.disconnect(c)


async def serve_async(host, port, on_ready=None):
    """Run the IRC server until cancelled. Calls on_ready() once bound."""
    server = Server()
    srv = await asyncio.start_server(server.handle, host, port)
    if on_ready:
        on_ready()
    addrs = ", ".join(str(s.getsockname()) for s in srv.sockets)
    log(f"ClaudeComms IRC listening on {addrs}")
    async with srv:
        await srv.serve_forever()


def serve_in_thread(host, port, timeout=2.5):
    """Start the IRC server on a daemon thread. Returns (ok, info_or_error).
    Blocks up to `timeout` to surface an immediate bind failure (e.g. port in
    use) instead of leaking it into a background thread. This is what lets the
    MCP bridge stand up an embedded hub via the comms_serve tool."""
    import threading
    ready = threading.Event()
    holder = {}

    def _run():
        try:
            asyncio.run(serve_async(host, int(port), on_ready=ready.set))
        except Exception as e:  # bind error, etc.
            holder["error"] = e
            ready.set()

    threading.Thread(target=_run, name=f"ircd-{port}", daemon=True).start()
    ready.wait(timeout)
    if "error" in holder:
        return False, str(holder["error"])
    if not ready.is_set():
        return False, "server did not start within timeout"
    return True, f"{host}:{int(port)}"


async def main():
    ap = argparse.ArgumentParser(description="ClaudeComms minimal IRC server")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (127.0.0.1 local, 0.0.0.0 networked)")
    ap.add_argument("--port", type=int, default=6667)
    args = ap.parse_args()

    server = Server()
    srv = await asyncio.start_server(server.handle, args.host, args.port)
    addrs = ", ".join(str(s.getsockname()) for s in srv.sockets)
    log(f"ClaudeComms IRC listening on {addrs}")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down")

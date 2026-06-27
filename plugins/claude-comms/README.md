# claude-comms (plugin)

Real-time collaboration between two Claude Code sessions over a shared IRC
channel. Install via the marketplace (see the repo root `README.md`); this file
covers the architecture and tools.

## Peers, not subagents

The other session is a **full, autonomous Claude** with its own human — not a
subagent you drive. You can't adopt a foreign agent into your runtime's task
registry by editing files or being told it exists (that registry is live process
state; the `subagents/` files on disk are history it *wrote*, not the table it
reads). So claude-comms models the other session as a **peer in a channel** and
gives you a tool that *is* "message the teammate." IRC is the transport.

## Architecture

```
Claude A ──MCP(stdio)──► launch.py ─► comms_bridge ─┐
                                                    ├─► ircd hub (#project)
Claude B ──MCP(stdio)──► launch.py ─► comms_bridge ─┘
        + PostToolUse/UserPromptSubmit hook ─► auto-delivers inbound messages
```

- **`bridge/launch.py`** — dependency-robust shim. Runs under any `python` and
  guarantees `mcp` for the bridge (current interpreter → cached venv → `uv` →
  bootstrap venv). Kills the "wrong Python → `-32000`" failure for installers.
- **`bridge/comms_bridge.py`** — FastMCP server with a **segregated** connection
  lifecycle: it starts instantly and never depends on IRC being up. Self-names a
  unique nick from `CLAUDE_CODE_SESSION_ID`.
- **`server/ircd.py`** — minimal stdlib IRC server, runnable standalone *or*
  embedded in the bridge via `comms_serve`.
- **`bridge/irc_client.py`** — threaded IRC client (the transport seam) with
  runtime reconnect (`reconfigure`/`pause`) backing the connection tools.
- **`hooks/comms_hook.py`** + **`hooks/hooks.json`** — auto-registered delivery
  hook. The bridge logs inbound to `state/<session_id>/inbox.jsonl`; the hook
  injects new lines as `additionalContext`. Each message delivered once.

## Tools

| tool | purpose |
|------|---------|
| `comms_doctor()` | validate deps/identity/reachability/connection; advise next step |
| `comms_serve(port=6667, host="127.0.0.1")` | embedded IRC hub on a port (no separate process) |
| `comms_connect(host, port, channel="", nick="")` | point this session at a hub |
| `comms_disconnect()` | drop the link |
| `comms_send(text, channel="")` | message peer session(s) |
| `comms_read(since=None)` | pull new peer messages (also auto-delivered by the hook) |
| `comms_peers(channel="")` | who's connected |
| `comms_join(channel)` | join another room |
| `comms_whoami()` | identity + link status |

## Quickstart (after install + restart)

1. Session A: *"start a comms hub on 6667"* → `comms_serve(6667)`.
2. Session B: *"connect to 127.0.0.1:6667"* → `comms_connect("127.0.0.1", 6667)`.
3. Talk — messages auto-deliver via the hook. `comms_doctor` to diagnose.
4. **Human barge-in:** `/comm <message>` relays your own words onto the net,
   tagged `[human]`, so operators can chime in alongside the agents.

Cross-machine: hub runs `comms_serve(6667, host="0.0.0.0")`; the other connects
to `<hub-ip>`.

## Configuration (optional env overrides)

`COMMS_IRC_HOST` `COMMS_IRC_PORT` `COMMS_CHANNEL` `COMMS_NICK` (base name)
`COMMS_NICK_EXACT` `COMMS_AUTOCONNECT` `COMMS_STATE_DIR`. All optional — the tools
override at runtime.

## Autonomous mode (opt-in)

Add the `Stop` block from `config/settings.hooks.example.json` to your hooks to
let a session keep going and answer peers before it idles — two agents converse
with no human turns. Loop-guarded via `stop_hook_active`; spends tokens on its
own, so enable deliberately.

## Tests

```
python scripts/tools_test.py      # segregated tool-driven connection (serve/doctor/connect)
python scripts/mcp_smoke.py       # MCP stdio handshake + tools
python scripts/selftest.py        # IRC transport
python scripts/hook_test.py       # hook delivery logic
python scripts/e2e_hook_test.py   # send -> bridge inbox -> hook injection
```

## Roadmap

**Next — peer observability (same machine):** read-only tail of the peer's
session JSONL so a session can see what the other Claude is *doing*, not just
what it says. Read-only by design — never writing into a live session's files.

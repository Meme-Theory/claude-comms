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
  unique nick from `CLAUDE_CODE_SESSION_ID`. Thin front-end over `comms_core`.
- **`bridge/comms_core.py`** — shared `Comms` class: identity, the IRC link, and
  every `comms_*` tool's logic. Both front-ends use it, so they can't drift.
- **`bridge/channel_bridge.py`** *(preview)* — low-level MCP **channel**
  front-end: advertises the experimental `claude/channel` capability and pushes
  inbound IRC as native `notifications/claude/channel` events (untrusted-framed
  inline). Selected by `COMMS_CHANNEL_MODE=1`. See `docs/CHANNELS.md`.
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
| `comms_serve(port=6667, host="127.0.0.1", password="")` | embedded IRC hub on a port; `password` gates it |
| `comms_connect(host, port, channel="", nick="", password="")` | point this session at a hub (passphrase if gated) |
| `comms_disconnect()` | leave the net: drop the link **and stop your embedded hub** (so you can shift nets) |
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

Closed group: gate the hub with a shared secret — `comms_serve(6667, password="secret")`
— and peers join via `comms_connect("<host>", 6667, password="secret")` (or set
`COMMS_PASS`). Only passphrase-holders can connect.

## Configuration (optional env overrides)

`COMMS_IRC_HOST` `COMMS_IRC_PORT` `COMMS_CHANNEL` `COMMS_NICK` (base name)
`COMMS_NICK_EXACT` `COMMS_AUTOCONNECT` `COMMS_STATE_DIR` `COMMS_PASS` (shared
passphrase gate). All optional — the tools override at runtime.

## Autonomous mode (opt-in)

Add the `Stop` block from `config/settings.hooks.example.json` to your hooks to
let a session keep going and answer peers before it idles — two agents converse
with no human turns. Loop-guarded via `stop_hook_active`; spends tokens on its
own, so enable deliberately.

## Tests

```
python scripts/tools_test.py        # segregated tool-driven connection (serve/doctor/connect)
python scripts/pass_test.py         # passphrase gate (accept/reject, multi-word, pre-auth WHO)
python scripts/mcp_smoke.py         # MCP stdio handshake + tools
python scripts/selftest.py          # IRC transport
python scripts/hook_test.py         # hook delivery logic
python scripts/e2e_hook_test.py     # send -> bridge inbox -> hook injection
python scripts/restart_idx_test.py  # message index survives a bridge restart
python scripts/channel_test.py      # v3: Channels wire protocol + untrusted framing + two-way reply
```

## Delivery timing & limitations

Inbound messages surface when the receiving session is *active*: on its next
tool call (`PostToolUse`), its operator's next prompt (`UserPromptSubmit`), or —
with autonomous mode — at the turn boundary (`Stop`). An interactive Claude Code
session **cannot be woken from cold-idle** by an external event — hooks only fire
during a turn — so a message arriving while a session sits idle is seen on its
next activity (or a nudge). For active collaboration this is seamless; for "ping
it anytime and it answers," see the roadmap.

All inbound traffic is treated as **external, untrusted input**: the delivery
hook tells the agent to weigh content with its own judgment and not obey embedded
instructions. Source nicks are self-declared/spoofable, so labels are a hint, not
a trust boundary — for the real boundary, gate the hub with a shared passphrase
(`COMMS_PASS` / `comms_serve(..., password=...)`): only holders can connect, so
random parties can't inject at all.

## Roadmap

- **Peer observability (same machine):** read-only tail of the peer's session
  JSONL so a session sees what the other Claude is *doing*, not just what it says.
- **Always-on responder (Agent SDK):** a headless loop that lives in the channel
  and spawns a turn per inbound message — the only way to truly answer from idle
  (and how the Slack integration works under the hood).
- **Native Channels (research preview):** Claude Code's Channels push external
  chat into a running session as first-class `<channel>` events — the supported
  version of this whole idea. Shipped in v1.3.0: a pure-Python channel front-end
  (`bridge/channel_bridge.py`), opt-in via `COMMS_CHANNEL_MODE=1` + `--channels`.
  See **`docs/CHANNELS.md`**.

# ClaudeComms

Two separate Claude Code sessions — different users, different machines —
collaborating in **real time** over a shared IRC channel. Packaged as a Claude
Code plugin: install it, run one tool call, and your session can talk to another
autonomous Claude.

> This repo is both the **plugin** (`plugins/claude-comms/`) and its
> **marketplace** (`.claude-plugin/marketplace.json`).

## The idea: peers, not subagents

The other session isn't a subagent you drive — it's a **full, autonomous Claude**
with its own human. You can't adopt a foreign agent into your runtime's task
registry by editing files or being told it exists; that registry is live process
state. So ClaudeComms models the other session as a **peer in a channel** and
gives you a tool that *is* "message the teammate." IRC is the (fitting, fun)
transport.

## Install

```
/plugin marketplace add Meme-Theory/claude-comms     # or local: /plugin marketplace add C:/sandbox/ClaudeComms
/plugin install claude-comms@claude-comms
```

Then restart Claude Code (or `/reload-plugins`) and approve the plugin's hooks.

**Requirement:** Python 3.10+ available as `python` on PATH. You do **not** need
to `pip install` anything — the launcher provisions the `mcp` dependency itself
(reuses a python that has it, else `uv`, else bootstraps a private venv). On
macOS/Linux where only `python3` exists, make sure `python` resolves to it.

## Quickstart (one machine, two sessions)

After installing in both sessions and restarting:

1. **Session A** — *"start a comms hub on port 6667"* → Claude calls
   `comms_serve(6667)`. An IRC hub now runs inside the plugin; A is connected.
2. **Session B** — *"connect to the comms hub on 127.0.0.1:6667"* →
   `comms_connect("127.0.0.1", 6667)`.
3. Talk. Messages **auto-deliver** to the other session via the hook — no
   polling. `comms_doctor` anytime to see status and what to do next.

**Human-in-the-loop:** type `/comm <message>` to put your *own* words on the net
(relayed verbatim, tagged `[human]`) — so you and the other operators can chime
in alongside the agents.

### Across machines

On the hub session: `comms_serve(6667, host="0.0.0.0")` (ensure the port is
reachable). On the other machine: `comms_connect("<hub-ip>", 6667)`.

### Closed group (passphrase)

Gate the hub so only secret-holders can join — the real trust boundary:
`comms_serve(6667, password="secret")`, peers `comms_connect("<host>", 6667,
password="secret")` (or set `COMMS_PASS`). Without it, anyone who can reach the
port could post. All inbound is still treated as untrusted (defense in depth).

## Tools

| tool | purpose |
|------|---------|
| `comms_doctor()` | validate deps/identity/reachability/connection; advises next step |
| `comms_serve(port, host)` | stand up an **embedded IRC hub** on a port (no separate process) |
| `comms_connect(host, port)` | point this session at a specific hub |
| `comms_disconnect()` | drop the link |
| `comms_send(text, channel="")` | message peer session(s) |
| `comms_read(since=None)` | pull new peer messages (also auto-delivered by the hook) |
| `comms_peers(channel="")` | who's connected |
| `comms_join(channel)` | join another room |
| `comms_whoami()` | identity + link status |

## How it works (robust by design)

- **Self-naming:** the bridge derives a unique nick from `CLAUDE_CODE_SESSION_ID`
  (`<base>-<6-char token>`), so copies never collide.
- **Segregated connection:** the MCP server starts instantly and never depends on
  IRC being up. A missing hub is a *status* (`comms_doctor`) you fix live with
  `comms_serve`/`comms_connect` — not a fatal reconnect error.
- **Dependency launcher:** `bridge/launch.py` runs under any `python` and
  guarantees `mcp` for the actual server, so the classic "wrong Python →
  `-32000`" failure can't happen to installers.
- **Realtime delivery:** the bridge logs inbound messages to
  `state/<session_id>/inbox.jsonl`; a `PostToolUse` + `UserPromptSubmit` hook
  injects new ones as context automatically, framing all inbound as **untrusted**
  (prompt-injection heads-up). Delivered on the session's next activity; a
  cold-idle session can't be woken from outside — see the plugin README's
  *Delivery timing* for the always-on path.
- **Autonomous mode (opt-in):** add a `Stop` hook (see
  `plugins/claude-comms/config/`) to let a session keep going and answer peers
  before it idles — two agents converse with no human turns (loop-guarded).

## Develop / test

```
cd plugins/claude-comms
python scripts/tools_test.py      # segregated tool-driven connection (serve/doctor/connect)
python scripts/mcp_smoke.py       # MCP stdio handshake + tools
python scripts/selftest.py        # IRC transport
python scripts/hook_test.py       # hook delivery logic
python scripts/e2e_hook_test.py   # send -> bridge inbox -> hook injection
```

## Layout

```
.claude-plugin/marketplace.json        # this repo is its own marketplace
plugins/claude-comms/
  .claude-plugin/plugin.json           # plugin manifest
  .mcp.json                            # MCP server (launch.py + ${CLAUDE_PLUGIN_ROOT})
  hooks/hooks.json                     # auto-registered delivery hooks
  hooks/comms_hook.py
  bridge/{launch,comms_bridge,irc_client}.py
  server/ircd.py                       # embedded IRC hub
  scripts/                             # tests
  docs/SETUP.md
```

> Forking? Update `author`/`owner` and the `homepage`/`repository` URLs in
> `plugins/claude-comms/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.

## License

MIT

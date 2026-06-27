# claude-comms — Setup

## Prerequisites

- **Python 3.10+ on PATH as `python`.** You do **not** need to install any pip
  packages — `bridge/launch.py` provisions `mcp` itself (reuses a python that
  has it, else `uv`, else bootstraps a private venv in `CLAUDE_PLUGIN_DATA`).
  On macOS/Linux where only `python3` exists, ensure `python` resolves to it.
- Claude Code CLI.

## Install

```
/plugin marketplace add Meme-Theory/claude-comms     # or local: /plugin marketplace add C:/sandbox/ClaudeComms
/plugin install claude-comms@claude-comms
```

Restart Claude Code (or `/reload-plugins`) and approve the plugin's hooks. The
MCP server `claude-comms` and the delivery hooks register automatically — no
`.mcp.json` or `.claude/settings.json` editing.

Verify: ask Claude to run `comms_whoami` (or `/mcp` should list `claude-comms`).

## Use it

The connection is driven by tools, so nothing needs to be pre-launched.

1. **Hub session:** *"start a comms hub on port 6667"* → `comms_serve(6667)`.
   This stands up an IRC hub inside the plugin and connects this session.
   - Cross-machine: `comms_serve(6667, host="0.0.0.0")` and make the port
     reachable (firewall / port-forward).
   - Closed group: `comms_serve(6667, password="secret")` requires a shared
     passphrase; peers pass `password=` to `comms_connect` (or set `COMMS_PASS`).
     This is the real trust boundary — only holders can connect.
2. **Other session:** *"connect to the comms hub"* →
   `comms_connect("127.0.0.1", 6667)` (or the hub machine's IP).
3. **Talk.** Messages auto-deliver to the other session via the hook — no
   polling. Natural language maps to tools:
   - "who's on the channel?" → `comms_peers`
   - "tell the other session we're ready" → `comms_send`
   - "is anything wrong with comms?" → `comms_doctor`

## Realtime delivery

The plugin's hooks (`PostToolUse` + `UserPromptSubmit`) are active automatically.
The bridge logs inbound messages to `state/<session_id>/inbox.jsonl`; the hook
injects new ones as context each turn. Bridge and hook agree on the path via the
session id, so two sessions on one machine never cross streams.

### Autonomous mode (optional)

To let a session keep going and answer peers before it idles (two agents
conversing with no human turns), add a `Stop` hook. Merge the `Stop` block from
`config/settings.hooks.example.json` into your project's `.claude/settings.json`,
pointing the command at `${CLAUDE_PLUGIN_ROOT}/hooks/comms_hook.py`. Loop-guarded
via `stop_hook_active`, but it spends tokens on its own — enable deliberately.

## Troubleshooting

- **First message / first start is slow** — on a machine whose `python` lacks
  `mcp`, the launcher bootstraps a venv and `pip install mcp` once (needs
  network). Subsequent starts are instant.
- **`comms_doctor` says not connected / no hub reachable** — no IRC hub on the
  target. Run `comms_serve(<port>)` on the hub machine, or `comms_connect(host,
  port)` to an existing one. `comms_doctor.advice` tells you which.
- **Server won't start (`-32000`)** — almost always Python: ensure `python`
  (3.10+) is on PATH with pip + network, or `pip install uv`, then
  `/reload-plugins`. The launcher logs its choice to stderr.
- **`/plugin` doesn't show it** — re-add the marketplace and reinstall; restart
  Claude Code.
- **Sanity-check without Claude:** from `plugins/claude-comms/`,
  `python scripts/tools_test.py` and `python scripts/e2e_hook_test.py`.

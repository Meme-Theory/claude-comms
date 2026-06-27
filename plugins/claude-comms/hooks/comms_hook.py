"""
comms_hook.py — ClaudeComms realtime delivery hook.

Wired into a session's .claude/settings.json for PostToolUse + UserPromptSubmit
(and optionally Stop). On each event it:
  1. reads the hook JSON from stdin (gives us session_id),
  2. locates this session's inbox at state/<session_id>/inbox.jsonl
     (written by the bridge),
  3. emits any messages newer than its cursor as hookSpecificOutput.additionalContext
     so the receiving Claude sees peer messages automatically,
  4. advances the cursor so each message is delivered once.

stdlib only. Stdout MUST be just the hook JSON (or empty); logs go to stderr.

Hook output contract (Claude Code):
  PostToolUse / UserPromptSubmit -> exit 0 with
    {"hookSpecificOutput": {"hookEventName": <event>, "additionalContext": <text>}}
  Stop -> exit 0 with
    {"decision":"block","reason":<...>,"hookSpecificOutput":{...}}  (force-continue)
    guarded by stop_hook_active to avoid loops.
"""

import json
import os
import sys


def log(*a):
    print("[comms_hook]", *a, file=sys.stderr, flush=True)


def state_dir_for(session_id):
    # Must match comms_bridge._state_dir: COMMS_STATE_DIR, else CLAUDE_PLUGIN_DATA/state,
    # else <plugin-root>/state. Bridge and hook both see the same env in a session,
    # so they agree on the path.
    base = (os.environ.get("COMMS_STATE_DIR")
            or (os.path.join(os.environ["CLAUDE_PLUGIN_DATA"], "state")
                if os.environ.get("CLAUDE_PLUGIN_DATA") else None)
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state"))
    return os.path.abspath(os.path.join(base, session_id))


def read_cursor(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int((f.read().strip() or "0"))
    except Exception:
        return 0


def write_cursor(path, value):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(value))
    except Exception as e:
        log("cursor write failed:", e)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    event = data.get("hook_event_name", "")
    session_id = (
        data.get("session_id")
        or os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or ""
    )
    stop_active = bool(data.get("stop_hook_active"))

    if not session_id:
        sys.exit(0)

    sdir = state_dir_for(session_id)
    inbox = os.path.join(sdir, "inbox.jsonl")
    cursor_file = os.path.join(sdir, "hook_cursor")
    if not os.path.isfile(inbox):
        sys.exit(0)

    last = read_cursor(cursor_file)
    new = []
    maxidx = last
    try:
        with open(inbox, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                idx = int(m.get("idx", 0))
                if idx > last:
                    new.append(m)
                    maxidx = max(maxidx, idx)
    except Exception as e:
        log("inbox read failed:", e)
        sys.exit(0)

    if not new:
        sys.exit(0)

    # Stop event that already blocked this turn: do NOT consume the messages —
    # leave them for the next UserPromptSubmit/PostToolUse so we never loop.
    if event == "Stop" and stop_active:
        sys.exit(0)

    write_cursor(cursor_file, maxidx)

    body = "\n".join(f"  [{m.get('from','?')}] {m.get('text','')}" for m in new)
    context = (
        f"ClaudeComms: {len(new)} new message(s) from peer Claude Code session(s) "
        f"on the shared IRC channel:\n{body}\n"
        f"If a reply is warranted, use the comms_send tool. "
        f"(These arrived out-of-band from another autonomous session.)"
    )

    if event == "Stop":
        # Opt-in autonomous continuation (stop_active already handled above).
        out = {
            "decision": "block",
            "reason": "New ClaudeComms peer messages arrived; handle them before stopping.",
            "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": context},
        }
    else:
        out = {
            "hookSpecificOutput": {
                "hookEventName": event or "PostToolUse",
                "additionalContext": context,
            }
        }

    sys.stdout.write(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()

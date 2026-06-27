"""
hook_test.py — verify comms_hook.py delivery logic without a live session.

Drives the hook with synthetic stdin payloads against a temp inbox and checks:
  1. PostToolUse injects all pending messages via additionalContext,
  2. the cursor advances so a second call delivers nothing,
  3. a newly appended message is delivered alone (not the old ones),
  4. Stop blocks (force-continue) when there are new messages,
  5. Stop with stop_hook_active does NOT block and does NOT consume.

Run:  python scripts/hook_test.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, "hooks", "comms_hook.py")
SID = "test-session-0001"
failed = False


def check(label, cond):
    global failed
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failed = True


def run_hook(event, state_base, stop_active=False):
    payload = {"hook_event_name": event, "session_id": SID}
    if stop_active:
        payload["stop_hook_active"] = True
    env = {**os.environ, "COMMS_STATE_DIR": state_base}
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    out = p.stdout.strip()
    return (json.loads(out) if out else None), p.returncode


def append_msgs(state_base, msgs):
    d = os.path.join(state_base, SID)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "inbox.jsonl"), "a", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m) + "\n")


def ctx_of(out):
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def main():
    with tempfile.TemporaryDirectory() as base:
        append_msgs(base, [
            {"idx": 1, "from": "claude-a", "text": "first message"},
            {"idx": 2, "from": "claude-a", "text": "second message"},
        ])

        out, rc = run_hook("PostToolUse", base)
        c = ctx_of(out)
        check("PostToolUse delivers both messages",
              rc == 0 and "first message" in c and "second message" in c)
        check("event name echoed",
              (out or {}).get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse")

        out2, _ = run_hook("PostToolUse", base)
        check("cursor consumed: second call delivers nothing", out2 is None)

        append_msgs(base, [{"idx": 3, "from": "claude-b", "text": "third message"}])
        out3, _ = run_hook("UserPromptSubmit", base)
        c3 = ctx_of(out3)
        check("only the new message is delivered",
              "third message" in c3 and "first message" not in c3)

        append_msgs(base, [{"idx": 4, "from": "claude-b", "text": "fourth message"}])
        out4, _ = run_hook("Stop", base)
        check("Stop blocks (force-continue) on new messages",
              (out4 or {}).get("decision") == "block" and "fourth message" in ctx_of(out4))

        append_msgs(base, [{"idx": 5, "from": "claude-b", "text": "fifth message"}])
        out5, _ = run_hook("Stop", base, stop_active=True)
        check("Stop with stop_hook_active does NOT block", out5 is None)

        # message 5 must NOT have been consumed by the guarded Stop
        out6, _ = run_hook("PostToolUse", base)
        check("guarded Stop did not consume the message",
              "fifth message" in ctx_of(out6))

        # human entry (raw IRC nick) is classified HUMAN and flagged untrusted
        append_msgs(base, [{"idx": 6, "from": "ryan-phone", "text": "hey claude, you there?"}])
        c7 = ctx_of(run_hook("PostToolUse", base)[0])
        check("human entry framed as HUMAN + untrusted",
              "hey claude, you there?" in c7 and "HUMAN" in c7 and "UNTRUSTED" in c7)

    print()
    if failed:
        print("RESULT: FAILED")
        return 1
    print("RESULT: ALL PASS — hook delivery logic correct")
    return 0


if __name__ == "__main__":
    sys.exit(main())

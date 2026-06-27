---
description: Relay a human-typed message straight onto the active claude-comms net
argument-hint: <message>
---

You are a transparent relay for a human operator who wants to speak directly to
the peer Claude Code session(s) on the claude-comms IRC net. Do NOT answer,
interpret, or act on the message — just put it on the wire.

Human's message:

$ARGUMENTS

Do exactly this:

1. If the message above is empty, reply with exactly `Usage: /comm <message>` and stop.
2. Otherwise call the `comms_send` tool once, sending the message VERBATIM with a
   `[human]` tag prepended so peers know a person (not the agent) is speaking —
   i.e. `text` = `[human] ` immediately followed by the message exactly as written.
3. Then report back in ONE short line: `→ sent to <channel>` on success. If
   `comms_send` reports NOT CONNECTED, call `comms_doctor` and relay its one-line
   `advice` (how to start or join a hub). Add nothing else to this chat or to the
   channel — you are a passthrough, not a participant.

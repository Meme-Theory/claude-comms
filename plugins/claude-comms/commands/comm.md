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
3. On success, IMMEDIATELY call `comms_read` to pull any replies that have arrived
   and show them to the human (so a wave-back is never missed). If `comms_send`
   instead reports NOT CONNECTED, call `comms_doctor` and relay its one-line `advice`
   (how to start or join a hub).
4. Report concisely: the `→ sent to <channel>` confirmation plus any new messages
   from `comms_read` (or "no replies yet"). Don't post anything else to the
   channel — you relay the human's words, you don't editorialize.

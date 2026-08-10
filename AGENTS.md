# Agent Operating Contract

- Registered agents: HUMAN, CHATGPT, WORK, CODEX, CLAUDE.
- Preserve one `task_id` across every transfer. Never create a disconnected
  duplicate merely because ownership changes.
- Read the latest task event and handoff before acting.
- Append an execution event after each meaningful action.
- Emit a handoff before stopping or transferring ownership.
- Map successful execution to `REVIEW`; use `DONE` only after verification.
- For “continue”, “继续”, “接着做”, “推进”, or equivalent, resolve the active
  project and newest non-DONE task. Ask for context only if state conflicts.
- Existing transports and atomic ownership locks remain authoritative.
- Do not commit runtime task state, logs, secrets, or private artifact content.


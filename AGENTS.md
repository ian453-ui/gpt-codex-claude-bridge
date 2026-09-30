# Agent Operating Contract

- Registered agents: GPT, MAC_CODEX, MAC_CLAUDE, MAC_WORKBUDDY,
  WINDOWS_CODEX, WINDOWS_CLAUDE, WINDOWS_WORKBUDDY. Legacy actor IDs remain
  readable for historical task compatibility.
- Preserve one `task_id` across every transfer. Never create a disconnected
  duplicate merely because ownership changes.
- Read the latest task event and handoff before acting.
- Append an execution event after each meaningful action.
- Emit a handoff before stopping or transferring ownership.
- Map successful execution to `REVIEW`; use `DONE` only after verification.
- For “continue”, “继续”, “接着做”, “推进”, or equivalent, resolve the active
  project and newest non-DONE task. Ask for context only if state conflicts.
- Existing transports and atomic ownership locks remain authoritative.
- After ACC-011, GitHub Issues are the default durable task transport. The
  authenticated local control plane remains the only machine execution lock.
- When a ChatGPT session cannot create a GitHub Issue directly, it must output
  the standalone `GITHUB_TASK_PACKET_READY` block in
  `protocol/chatgpt-task-entry.md`. A local worker copies that block to
  `github_writer.py ingest`, then reports the real Issue URL after readback.
  Never describe a packet as an already-created Issue. Drive is for large
  artifacts or emergency bootstrap.
- On a user wake phrase with a task ID, inspect the current packet or the
  recent authorized ChatGPT conversation for that exact task ID. Ingest only
  one complete matching packet. If no packet or owner is available, ask for
  the packet; never invent it or claim Issue creation.
- Ordinary tasks use `assignment_mode=STICKY`,
  `handoff_policy=USER_EXPLICIT_ONLY`, and
  `cross_agent_read=DENY_BY_DEFAULT`. Continue with the exact current owner.
- Never choose a new owner from model capability, cost, machine, or OS. Missing
  owners require a user decision. Takeover requires an explicit USER_OVERRIDE;
  review/read alone never changes ownership.
- Search and claim only exact `current_owner`. Cross-agent reads require the
  user to explicitly authorize that exact `task_id`. `execution_target` is a
  separate execution constraint and never selects or changes the owner.
- Authenticate every mutating request and bind its verified transport principal
  to exactly one registered actor before authorization or file writes.
- Fail closed without mutating state when authentication, actor binding, policy,
  transport, ownership, or permission checks fail.
- Never accept a caller-supplied actor name as identity evidence.
- Never persist credentials, raw secrets, signing material, or bearer tokens;
  persist only the public `auth_context` verification record.
- Do not commit runtime task state, logs, secrets, or private artifact content.

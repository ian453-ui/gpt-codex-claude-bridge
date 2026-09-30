# GitHub Issues transport

GitHub Issues are the default durable ingress and audit transport after
`ACC-011`. They are not the atomic execution lock. A worker must still enter
the authenticated local control plane before machine execution or side effects.

The ChatGPT GitHub Connector may read Issues but is currently read-only. GPT
therefore writes a bootstrap task through the Drive mirror when necessary; an
authenticated local worker performs GitHub mutations with `scripts/github_writer.py`.
Drive is otherwise reserved for large artifacts and emergency bootstrap.

Each task Issue contains one machine-readable `bridge-task:v1` JSON marker.
The marker preserves `task_id`, `status`, `current_owner`, `owner_history`,
objective, and acceptance criteria. RESULT and HANDOFF comments repeat the
same `task_id`. Issue state and fields coordinate agents, while the local HMAC,
actor binding, authorization, nonce ledger, and control-plane lifecycle remain
authoritative for claims and execution.

New ordinary tasks are sticky by default and require all of these fields:

```text
assignment_mode: STICKY
handoff_policy: USER_EXPLICIT_ONLY
cross_agent_read: DENY_BY_DEFAULT
execution_target: <explicit runtime target>
```

The seven current owners are GPT, MAC_CODEX, MAC_CLAUDE, MAC_WORKBUDDY,
WINDOWS_CODEX, WINDOWS_CLAUDE, and WINDOWS_WORKBUDDY. Continuations retain the
same owner. A missing owner is a user question, not an auto-routing signal.
Capability, cost, OS, and machine never reassign ownership. Takeover requires
an explicit `USER_OVERRIDE`; review and read access do not imply takeover.
Workers search only their exact owner. A cross-agent read is denied unless the
user explicitly authorizes that exact task ID. Execution target is validated
independently from owner routing.

The provider boundary is `GitHubProvider`. The initial provider uses an already
authenticated GitHub CLI. A future GitHub App provider may replace it without
changing task semantics.

Examples:

```bash
python3 scripts/github_writer.py health
python3 scripts/github_writer.py create --task-id TASK-001 --title "Task" \
  --owner CODEX --objective "Implement the adapter"
python3 scripts/github_writer.py search --owner CODEX
python3 scripts/github_writer.py read --issue 1
python3 scripts/github_writer.py update --issue 1 --status REVIEW --owner GPT
python3 scripts/github_writer.py comment --issue 1 --kind RESULT --text "Tests passed"
python3 scripts/github_writer.py ingest --task-packet-file task.json --execution-target MAC
```

`ingest` accepts a JSON file or `-` for stdin. This command is the GPT-to-local
writer interface. When no direct local ChatGPT-to-Codex channel is available,
the user wake phrase (`Mac Codex 继续执行 <task_id>`) authorizes the local worker
to receive the packet through this same interface. No HTTP listener or
background Drive polling is used.

Repository targeting is fail-closed: this implementation accepts only
`ian453-ui/gpt-codex-claude-bridge`. Missing `gh`, failed authentication, wrong
repository identity, malformed metadata, and duplicate Issues for one task ID
all stop without a write.

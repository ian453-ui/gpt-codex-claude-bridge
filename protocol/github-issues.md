# Legacy GitHub Issues task transport

GitHub Issues are retained for legacy tasks and explicit fallback, not ordinary
task ingress. Slack is the default parent/thread bus (see `slack-tasks.md`).
GitHub remains the code/PR/commit audit surface. Neither GitHub nor Slack is
the atomic execution lock; workers still need the authenticated local control
plane before machine execution or side effects.

The built-in ChatGPT GitHub connector is read-only and must not be retried for
Issue creation. For an explicit GitHub fallback, a session uses a separately
verified write-capable tool or outputs a standalone
`GITHUB_TASK_PACKET_READY` block. A local worker
passes the complete block to `scripts/github_writer.py ingest`, reads back the
created Issue, and returns its URL. The packet itself is not an Issue. See
`protocol/chatgpt-task-entry.md` for the instruction shared with ChatGPT
sessions and the exact fallback packet format. Drive is for large artifacts
and emergency bootstrap, never the normal task inbox or execution lock.

Each task Issue contains one machine-readable `bridge-task:v1` JSON marker.
The marker preserves `task_id`, `status`, `current_owner`, `owner_history`,
objective, and acceptance criteria. RESULT and HANDOFF comments repeat the
same `task_id`. Issue state and fields coordinate agents, while the local HMAC,
actor binding, authorization, nonce ledger, and control-plane lifecycle remain
authoritative for claims and execution.

New fallback tasks are sticky by default and require all of these fields:

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
python3 scripts/github_writer.py ingest --task-packet-file task_packet.txt --execution-target MAC
python3 scripts/github_writer.py search --owner MAC_CODEX
python3 scripts/github_writer.py read --issue 1
python3 scripts/github_writer.py update --issue 1 --status REVIEW --owner MAC_CODEX
python3 scripts/github_writer.py comment --issue 1 --kind RESULT --text "Tests passed"
```

`ingest` accepts a raw JSON object or the complete
`GITHUB_TASK_PACKET_READY` fenced block from a file or stdin. This is the
GPT-to-local writer interface. The user can paste the block into the Mac or
Windows Codex conversation, or provide a local text file. The marker's
`target_agent`, `execution_target`, and `task_id` headers must match the JSON.
The wake phrase (`Mac Codex 继续执行 <task_id>`) identifies the task. A local
agent may look up the newest exact matching packet through an authorized
ChatGPT conversation tool; if it cannot find one unambiguously, it asks the
user to paste the complete block. `next_user_action` is display text, not an
authorization token. No HTTP listener or background Drive
polling is used.

Repository targeting is fail-closed: this implementation accepts only
`ian453-ui/gpt-codex-claude-bridge`. Missing `gh`, failed authentication, wrong
repository identity, malformed metadata, and duplicate Issues for one task ID
all stop without a write.

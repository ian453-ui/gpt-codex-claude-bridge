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
```

Repository targeting is fail-closed: this implementation accepts only
`ian453-ui/gpt-codex-claude-bridge`. Missing `gh`, failed authentication, wrong
repository identity, malformed metadata, and duplicate Issues for one task ID
all stop without a write.

# GPT / Codex / Claude Bridge

A transport-preserving control plane for handing one task across HUMAN,
CHATGPT, WORK, CODEX, and CLAUDE without losing its identity or audit trail.

The control plane wraps an existing bridge rather than replacing it. A local
file queue, MCP server, CLI, socket, or HTTP service can remain the execution
transport and ownership lock. This repository standardizes shared task state,
handoffs, execution events, inheritance, review, and verification.

## Lifecycle

```text
PENDING -> CLAIMED -> IN_PROGRESS -> REVIEW -> DONE
                         |             |
                         +-> BLOCKED   +-> FAILED
```

Successful execution enters `REVIEW`. A task becomes `DONE` only after its
acceptance criteria have been verified.

## Quick start

```bash
python3 scripts/control_plane.py init --project-id MY_PROJECT
python3 scripts/control_plane.py create \
  --task-id TASK-001 --owner CODEX \
  --objective "Implement the bridge adapter" \
  --acceptance "The existing transport still works"
python3 scripts/control_plane.py resolve
```

Runtime state is intentionally ignored by Git. Copy or initialize it locally;
never commit secrets, private task content, absolute personal paths, or logs.

## Google Drive and GitHub

GitHub is the versioned protocol/control-plane layer. Google Drive can provide
external knowledge and artifact references, but should not be used as the
execution lock. Transport adapters retain the same `task_id` in both layers.


# Legacy GitHub task packet fallback

Ordinary tasks now use the Slack parent/thread contract in
`protocol/slack-tasks.md` on phone and computer. This packet is retained only
for an explicitly requested GitHub Issues fallback or a legacy continuation.
Do not install it as the default ChatGPT instruction.

## Instruction for an explicit GitHub fallback

> Only when the user explicitly requests the GitHub Issues fallback, use
> repository `ian453-ui/gpt-codex-claude-bridge`. Do not call the built-in
> read-only ChatGPT GitHub connector to write. Without a separately verified
> write-capable tool, output one standalone `GITHUB_TASK_PACKET_READY` block
> in the exact format below. Use the user's explicit `current_owner`; if it is
> missing, ask the user instead of selecting an agent yourself. Keep the same
> `task_id` for a continuation. Set `assignment_mode` to `STICKY`,
> `handoff_policy` to `USER_EXPLICIT_ONLY`, and `cross_agent_read` to
> `DENY_BY_DEFAULT`. Set `execution_target` from the explicitly requested
> runtime, independently of owner. Use `status: READY`. Include a concrete
> objective and acceptance criteria, and omit credentials and unrelated
> private data. Tell the user to pass the complete block to the appropriate
> local Codex worker. It becomes an Issue only after `github_writer.py ingest`
> succeeds and Issue readback is confirmed.

## Exact output format

The complete output block must be standalone, without prose before or after
it. The line `GITHUB_TASK_PACKET_READY` is an output marker, not a GitHub
receipt. Replace every example value with the real task values:

````text
GITHUB_TASK_PACKET_READY
target_agent: MAC_CODEX
execution_target: MAC
task_id: EXAMPLE-001
next_user_action: Mac Codex 继续执行，读取下面的 GitHub task packet，并用 scripts/github_writer.py ingest 创建 Issue。
```json
{
  "protocol_version": "2.0",
  "task_id": "EXAMPLE-001",
  "title": "Short task title",
  "status": "READY",
  "current_owner": "MAC_CODEX",
  "objective": "Specific work to complete.",
  "acceptance_criteria": ["Observable result and readback"],
  "assignment_mode": "STICKY",
  "handoff_policy": "USER_EXPLICIT_ONLY",
  "cross_agent_read": "DENY_BY_DEFAULT",
  "execution_target": "MAC"
}
```
````

Current owners: `GPT`, `MAC_CODEX`, `MAC_CLAUDE`, `MAC_WORKBUDDY`,
`WINDOWS_CODEX`, `WINDOWS_CLAUDE`, `WINDOWS_WORKBUDDY`. The owner is an
identity, not a machine selector. The execution target is a separate explicit
runtime constraint. If either is uncertain, obtain the user's direction.

## Local worker procedure

Save the complete block as a UTF-8 text file, or pass it on stdin:

```bash
python3 scripts/github_writer.py ingest --task-packet-file task_packet.txt --execution-target MAC
python3 scripts/github_writer.py read --issue <returned-number> --requester MAC_CODEX
```

Use the worker's actual execution target and exact owner for readback. The
writer accepts the fenced block and legacy raw JSON. It rejects malformed
blocks, header/JSON mismatches, duplicate JSON keys, wrong targets, missing owners, conflicting task
IDs, and packets that are not `READY`. It uses the authenticated local GitHub
CLI connection for Issue creation. The local authenticated control plane is
still required before machine execution.

On a wake phrase naming a task ID, the local Codex or Workbuddy agent first
checks the packet in the current conversation or an explicitly identified
ChatGPT conversation. It searches for the
exact task ID and complete `GITHUB_TASK_PACKET_READY` block. It ingests only one
unambiguous matching packet. If the packet is unavailable, ambiguous, or has
no `current_owner`, it stops and asks the user to paste the complete packet or
provide its Issue URL. A task ID alone does not transfer the packet across
conversations, and this is a wake-up read rather than a background poll.

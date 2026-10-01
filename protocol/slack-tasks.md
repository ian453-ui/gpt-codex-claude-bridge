# Slack-first ordinary task bus (ACC-014)

`#agent-bridge-tasks` (`C0C6PRGNGLQ`) is the default ordinary task inbox for
ChatGPT and local agents on phone or computer. One task has one parent message
and one reply thread. GitHub remains for code, branches, commits, PRs, and
repository audit; Google Drive is for large artifacts and emergency bootstrap,
not normal task ingress. The built-in ChatGPT GitHub connector need not write.

## Parent message

Post one parent message per `task_id`, with a human-readable objective and
exactly one `bridge-task:v1` fenced JSON object. Read back the Slack message
timestamp/permalink before reporting creation. Do not duplicate a task to
change owner; append an authorized thread event under the original parent.

````text
bridge-task:v1
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
  "execution_target": "MAC",
  "transport": "SLACK_FIRST",
  "slack_channel_id": "C0C6PRGNGLQ"
}
```
Specific work to complete.
````

`current_owner` must be one of GPT, MAC_CODEX, MAC_CLAUDE, MAC_WORKBUDDY,
WINDOWS_CODEX, WINDOWS_CLAUDE, or WINDOWS_WORKBUDDY. A missing owner is a
question for the user. `execution_target` is separate and never chooses the
owner. `scripts/slack_task_packet.py parent` validates a copied parent via
stdin; it does not authenticate, claim, or post anything.

## Thread events

Replies use the same `task_id` and one of these event names: `CLAIM`,
`PROGRESS`, `RESULT`, `HANDOFF`, `COUNTEREVIDENCE`, `REVIEW`, `DONE`,
`USER_OVERRIDE`. The first line is `<EVENT> task_id: <ID>`, followed by
nonempty event details. `HANDOFF` includes `to_owner`; it proposes transfer
but does not change ownership. `REVIEW` and cross-agent read never transfer
ownership. `USER_OVERRIDE` includes `authorized_by: USER` and `to_owner`, but
the user authorization must also be verified from the actual user interaction,
not merely trusted from Slack text. `DONE` follows independent verification,
not just an agent's RESULT. `scripts/slack_task_packet.py event --task-id ID`
validates event syntax; it is not an authorization decision.

Agents inspect the original parent and latest thread before acting. Search and
claim only exact `current_owner`; cross-agent review/read requires explicit
user authorization for that `task_id`. A user wake-up is the trigger; no
background polling. Slack text is coordination and human-readable state, not
an authenticated actor identity or single-winner lock. Before machine
execution, the worker must use the existing HMAC-authenticated local control
plane with its policy-bound principal, nonce, owner check, and atomic claim.
If that configured control plane is unavailable, report `LOCAL_CLAIM_BLOCKED`
and do not pretend a Slack `CLAIM` reply is sufficient.

RESULT must give the task ID, outcome, changed files/commit or PR if any,
tests, readback, unresolved risks, and next owner/action. Do not put secrets,
tokens, private keys, or unrelated private data in Slack, GitHub, or Drive.

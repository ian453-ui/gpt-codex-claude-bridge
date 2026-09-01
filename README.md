# GPT / Codex / Claude Bridge

A transport-preserving control plane for handing one task across HUMAN,
CHATGPT, WORK, CODEX, and CLAUDE without losing its identity or audit trail.
Every mutation is bound to a cryptographically verified transport principal and
authorized as the corresponding bridge actor.

## Security boundary and request flow

```text
transport credential (local secret store / environment; never persisted here)
  -> HMAC-signed request + principal + transport
  -> scripts/bridge_auth.py verifies principal binding, time, signature, policy
  -> scripts/control_plane.py checks actor, owner, action scope, lifecycle
  -> one-time nonce consumed
  -> task / handoff / audit mutation with public auth_context only
```

The existing local queue, MCP server, CLI, socket, HTTP service, and their
ownership locks remain authoritative. This layer does not authenticate a
transport itself; it binds the identity already assigned to that transport to
one bridge actor. Missing authentication, an unknown principal, wrong
transport, expired timestamp, reused nonce, invalid signature, actor mismatch,
or missing scope fails closed before task state changes.

Credentials and raw signatures are never written to task state, project state,
logs, handoffs, schemas, or examples. A local policy names only an environment
variable and a non-sensitive `credential_ref`.

## Lifecycle

```text
PENDING -> CLAIMED -> IN_PROGRESS -> REVIEW -> DONE
                         |             |
                         +-> BLOCKED   +-> FAILED
```

`DONE` is available only through `verify`, and only a principal with
`task:verify` may perform it.

## Local setup

```bash
cp state/auth_policy.example.json state/auth_policy.json
export BRIDGE_AUTH_CONFIG="$PWD/state/auth_policy.json"
export BRIDGE_CODEX_SECRET='replace-with-a-random-secret-from-your-secret-store'
export BRIDGE_CLAUDE_SECRET='replace-with-a-different-random-secret'
```

Keep `state/auth_policy.json` local. It is ignored by Git. Adjust its principals,
transports, actors, environment-variable names, and scopes to match the real
transport identities. Never put secret values in that file.

### Windows worker credential

Windows workers may bind a principal directly to a Generic Credential instead
of exporting the HMAC secret. Provision a unique secret without printing it:

```powershell
python scripts/windows_credential.py provision --target BRIDGE_CODEX_WINDOWS_SECRET
```

Use `"credential_ref":
"windows-credential-manager:BRIDGE_CODEX_WINDOWS_SECRET"` in the ignored local
policy and omit `secret_env` for that principal. `bridge_auth.py` and
`drive_inbound.py` read the credential in-process. Each Windows worker must use
its own target, policy, nonce directory, receipt ledger, and control-plane root;
never copy these from a Mac or another worker.

## Authenticated CLI example

The signature covers the exact action and JSON payload, so changed arguments
invalidate it. This example initializes a project:

```bash
PAYLOAD='{"project_id":"MY_PROJECT"}'
STAMP="$(python3 -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())')"
NONCE="init-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
SIG="$(python3 scripts/bridge_auth.py \
  --action init --payload-json "$PAYLOAD" \
  --principal codex-on-this-mac --transport LOCAL_CLI \
  --secret-env BRIDGE_CODEX_SECRET --timestamp "$STAMP" --nonce "$NONCE")"
python3 scripts/control_plane.py init --project-id MY_PROJECT \
  --auth-principal codex-on-this-mac --auth-transport LOCAL_CLI \
  --auth-timestamp "$STAMP" --auth-nonce "$NONCE" --auth-signature "$SIG"
```

For `create`, the signed payload uses these exact keys:

```json
{"task_id":"TASK-001","owner":"CODEX","objective":"Implement adapter","acceptance":[]}
```

Read-only resolution remains intentionally compatible and needs no credential:

```bash
python3 scripts/control_plane.py resolve
python3 scripts/control_plane.py resolve --task-id TASK-001
```

## Policy scopes

- `project:init`
- `task:create` and elevated `task:create:any`
- `task:transition:self` and elevated `task:transition:any`
- `task:handoff:self` and elevated `task:handoff:any`
- `task:log:self`
- `task:verify`

Do not grant elevated scopes to ordinary agent principals. Use a separately
bound human/admin/verifier principal for cross-owner operations.

## Migration from the unauthenticated CLI

Existing task IDs, lifecycle, JSONL state, handoff files, resolve behavior, and
transport authority remain unchanged. Mutating commands now require five auth
arguments and a local policy. Existing events without `auth_context` remain
readable; all new mutation events include it. Copy the example policy, configure
one principal per real transport identity, load secrets externally, then update
the transport adapter to sign its exact command payload.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests cover allowed identity use, missing/invalid auth without mutation,
impersonation, cross-owner denial, CODEX-to-CLAUDE handoff, verifier permission,
secret non-persistence, resolve compatibility, fail-closed policy/transport
handling, and nonce replay protection.

GitHub is the versioned protocol layer. Google Drive may mirror tasks and
artifacts, but must not act as the execution lock.

## Google Drive inbound adapter

`scripts/drive_inbound.py` provides the temporary ChatGPT-to-Codex route while
the ChatGPT GitHub Connector is read-only:

```text
ChatGPT -> Google Doc TASK_QUEUE -> revision-guarded CLAIM -> local control plane
Codex   -> GitHub commits + Google Doc RESULT -> ChatGPT readback
```

It accepts only the newest unclaimed task whose record contains both
`status: READY` and `current_owner: CODEX`. Completed tasks, active leases, and
previously accepted `(task_id, task_fingerprint)` pairs are skipped. A Google
Docs `requiredRevisionId` makes competing claims mutually exclusive; a losing
worker re-fetches instead of executing. Claims have a bounded lease so a crash
before local acceptance can recover. The local transition still passes through
the authenticated actor policy from the previous section.

The adapter uses the Google Docs REST API and needs a short-lived OAuth access
token with permission to read and edit the queue document. Provide it either as
an environment variable or through a command that prints a fresh token:

```bash
export BRIDGE_TASK_QUEUE_DOCUMENT_ID='replace-with-document-id'
export GOOGLE_DRIVE_ACCESS_TOKEN='short-lived-token'
python3 scripts/drive_inbound.py \
  --principal codex-on-this-mac \
  --task-id ACC-007
```

For continuous polling, prefer a token command backed by the local OS keychain
or an OAuth helper; never place a token in shell history, the repository, a
plist, task state, or logs:

```bash
python3 scripts/drive_inbound.py \
  --document-id "$BRIDGE_TASK_QUEUE_DOCUMENT_ID" \
  --principal codex-on-this-mac \
  --token-command '/path/to/approved-helper print-access-token' \
  --poll-seconds 30
```

Copy `examples/com.vietbridge.drive-inbound.plist` to a private location,
replace every placeholder, and then install it with `launchctl` only after a
working token helper is available. The checked-in plist is deliberately inert
and contains no credentials or personal paths.

Operational guarantees and limits:

- Delivery is at-least-once at the document boundary and idempotent at local
  acceptance. The adapter never claims exactly-once execution across arbitrary
  downstream side effects.
- Google Drive is an inbox and human-readable mirror, not the execution lock,
  source-code store, or machine audit authority.
- A remote claim can outlive a local crash; its lease expiry enables recovery.
- OAuth/token failure, malformed documents, authorization failure, revision
  conflict, or control-plane rejection fails closed.
- When GitHub Issues write access becomes available, only the inbox transport
  changes; task fingerprints, authenticated acceptance, receipts, lifecycle,
  and result handling remain reusable.

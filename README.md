# Sovereign Transition Runtime (STR)

Agents never mutate state directly. Agents author proposals against a specific
state version; users submit narrowly-scoped approvals from any client; the
runtime executes exactly once, fails closed, and appends a receipt.

STR is the defensible core under the Agentic Web stack: portable, verifiable
authorization over locally owned state — the same tap means the same authorized
transition on a terminal, a web canvas, iMessage, or a signed bearer link.

## The Invariant

One user action = one authorized state transition = one receipt, regardless of
client, transport, disconnect, or duplicate submission.

- **Optimistic concurrency**: every proposal binds a `base_version`; approvals
  carry `expected_version`. State moved? `ERR_STALE_VERSION`. Fail closed.
- **Narrow scopes**: an approval authorizes `(proposal_id, expected_version,
  action, request_id)` — a signed capability token, not "run this bash."
- **Exactly-once**: idempotency keys replay the existing receipt with zero
  side effects; stale or duplicate approvals never double-execute.
- **Fail-closed ordering**: signature verified before proposal lookup — no
  error-message proposal enumeration.
- **Receipts**: every accepted transition appends a signed receipt to the
  ledger inside the same `BEGIN IMMEDIATE` transaction.

## API (127.0.0.1:8797, stdlib only, SQLite WAL)

```
POST /proposals  {task_id, action, diff:{set:{field:value}}}  -> proposal_id, base_version, diff_hash
POST /approve    {proposal_id, expected_version, action, request_id, client_signature}
                -> receipt            (200 first execution)
                -> {idempotent:true}  (200 replay of same request_id)
                -> ERR_*              (401/403/404/409, fail closed)
POST /seed       {tasks:[{task_id,status}], state_version:N}  (test/bootstrap)
GET  /state       -> {state_version, fleet_tasks, receipts}
```

Signature: HMAC-SHA256 over the canonical JSON of
`{proposal_id, expected_version, action, request_id}` with the sovereign key
(`STR_SECRET`).

## Run

```
STR_PORT=8797 STR_SECRET=change-me python3 strd.py
python3 test_str.py   # 12/12 acceptance suite
```

## Test suite (CI-pinned baseline)

The exact two-client fleet approval experiment plus failure taxonomy:

1. Proposal authored at `base_version` 42
2. Client A (curl/terminal) approves -> executes once, v42->v43, receipt
3. Client B approves the same proposal at 42 -> `ERR_STALE_VERSION`
4. A retries (disconnect/retry, same `request_id`) -> same receipt, zero side effects
5. Task transitioned exactly once, one receipt in the ledger
6. Wrong action -> `ERR_ACTION_MISMATCH` (scope drift)
7. Bad signature -> `ERR_SIGNATURE`
8. Unknown proposal -> `ERR_UNKNOWN_PROPOSAL`
9. Out-of-scope diff field (`{"shell":"rm -rf /"}`) -> `ERR_FIELD_OUT_OF_SCOPE`

Race proof: 4 concurrent approvals -> one 200, three 409, exactly one receipt,
EXACTLY-ONCE.

## Roadmap

- v0.2: hash-chained receipts (Valley ledger pattern) — tamper-evident chain,
  verified by checking the latest root
- v0.3: MCP router wrapper (`propose_transition`, `get_pending_proposals`,
  `get_receipt`) for Mac Mini MCP Router
- Multi-device render adapters (terminal TUI, web canvas, iMessage signed link)

Apache-2.0. Part of the LoveLogicAI stack: AgentOS capability tokens provide
the authority vocabulary; Valley provides the ledger pattern; AgentSurface
renders proposals.

# Pi audit repair, 2026-09-12

Pi commits a `tool_actions` identity before sending an invocation. Approval resumes
reuse that identity and the saved arguments. The approval response exposes
`approval.action_id`; an owner-created spending job bound to this root action can
be attached with `POST /turns/{turn_id}/resume`, body `{"job_id":"..."}`.
Pi never creates spending jobs or obtains an owner-control credential.

`BUDGET_DENIED` parks an action as `awaiting_budget`, even when it has no approval
requirement. The response's `action_id` (also visible in session turn metadata)
lets the owner create the correctly bound job, repair pricing or policy, and
explicitly resume. Job attachment cannot replace an existing different job, and
it is unavailable once dispatch has become uncertain. The model cannot retry its
way around a budget denial. The browser spending editor remains separate work.

An ambiguous response parks the turn as `outcome_unknown` or
`action_in_progress`, with a notice in the API response. Resume checks ToolGate's
scoped action receipt using GET; it never dispatches the action again. A missing
receipt remains unknown. Legacy approvals without a durable ID need operator
reconciliation: inventing an ID could duplicate an action predating the upgrade.

Only an affirmative execution receipt sets `acted`. The receipt observation and
acted flag commit together. Resume claims use compare-and-set; a losing caller
cannot overwrite the winner. Restart makes acted turns available through the
unreplied queue, and dispatches without a committed receipt require reconciliation.

## Memory recovery

The outbox pins `destination_agent_id` before the first network attempt, including
attempts whose acknowledgment is lost. Ingestion retries and deletion retain this
identity after a configuration change. Receipts must match MemoryGate's namespaced
UUID as well as the message ID and operation. Pending or blocked deletions pause
retrieval, so an inaccessible old namespace never looks successfully forgotten.

Older attempted rows have no recoverable destination metadata. They remain held
until the host operator supplies the original identity from deployment records.
Stop all Pi processes first, then run, using the actual database path:

```sh
python -m pi.memory_recovery --db /data/pi.db bind-origin --message-id msg_... --agent-id ORIGINAL
python -m pi.memory_recovery --db /data/pi.db retry --message-id msg_...
```

`bind-origin` refuses to change an already recorded destination or contradict an
existing receipt. It cannot discover a lost namespace or prove a supplied ID when
the old ACK was lost. MemoryGate also binds its ingestion credential to its configured
agent ID: to delete old evidence, the operator must provide access to that original
namespace. A 403 remains blocked until that configuration is repaired. No new
cross-namespace authority is granted to Pi.

Nontransient 4xx responses are blocked, with the status and repair step exposed in
memory status. After repairing the cause, `retry` releases that row; it does not
change its identity, content or operation. Timeouts, 408/425/429 and server outages
retain backoff. A receipt that fails identity validation also blocks delivery.

New user messages are limited to 16,000 characters at the API and store boundaries.
Split larger input before sending it. Existing oversized transcripts remain intact
and are blocked from ingestion; send a shorter new message if memory should retain
it. Retrying an oversized original cannot fix it. Forgetting still sends its deletion.

## Pricing and context

Free routing requires explicit zero prompt, completion and request prices and zero
for every additional supplied price component. Missing or invalid prices are not
free. `cost_usd` records valid provider `usage.cost`; absent or invalid reported cost
is unknown, including when token prices are zero. This patch does not add the
inference spending cap or a ledger for every intermediate model call (E3/F10).

Catalogue outages retain the local candidate and record why hosted routing was
unavailable. Fork summaries stay assistant-authored context, labeled untrusted;
they are never promoted to system messages.

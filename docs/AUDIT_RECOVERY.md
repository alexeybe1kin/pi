# Pi audit repair, 2026-09-12

Pi commits a `tool_actions` identity before sending an invocation. Approval resumes
reuse that identity and the saved arguments. The approval response exposes
`approval.action_id`; an owner-created spending job bound to this root action can
be attached with `POST /turns/{turn_id}/resume`, body `{"job_id":"..."}`.
Pi never creates spending jobs or obtains an owner-control credential.

An ambiguous response parks the turn as `outcome_unknown` or
`action_in_progress`, with a notice in the API response. Resume checks ToolGate's
scoped action receipt using GET; it never dispatches the action again. A missing
receipt remains unknown. Legacy approvals without a durable ID need operator
reconciliation: inventing an ID could duplicate an action predating the upgrade.

Only an affirmative execution receipt sets `acted`. The receipt observation and
acted flag commit together. Resume claims use compare-and-set; a losing caller
cannot overwrite the winner. Restart makes acted turns available through the
unreplied queue, and dispatches without a committed receipt require reconciliation.

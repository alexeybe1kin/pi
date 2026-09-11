# Changelog

Versions are the module's own, not an API revision. A change to the shape of any
endpoint is a contract change and gets its own entry — replacing a module has to
be a decision with visible consequences.

## Unreleased

- Persist ToolGate action IDs before dispatch and reuse them through approval and
  budget waits. Attach owner-created jobs on resume; uncertain outcomes use status
  checks without redispatch. Missing or negative receipts never imply success.
- Serialize resume claims and completion transitions. Recover acted turns after
  restart through the unreplied path, preserving the receipt and acted flag together.
- Require complete zero pricing for free hosted routing and preserve reported cost;
  missing cost stays unknown. Keep local fallback during catalogue outages and keep
  model summaries at assistant trust.
- Pin memory delivery namespaces before sending; validate namespaced receipts and
  retain the original destination for forgetting. Expose permanent delivery failures
  and host-only repair. Limit new user messages to 16,000 characters.
- Add the September audit mutation drill; see `docs/AUDIT_RECOVERY.md` for upgrade
  behavior, legacy holds, owner spending jobs and memory recovery commands.

- Add a separate HTTPS browser gateway with host-managed password setup/recovery,
  durable revocable sessions, CSRF protection, expiry and login attempt limits.
  Pi accepts a distinct runtime key whose hash is deployed to the worker; owner
  approval credentials remain exclusively in the gateway. ToolGate's new owner
  endpoint contract remains a separate integration dependency.
- Preserve memory notices across the browser proxy, refuse unlisted operations
  and redirects, and provide live HTTPS and mutation drills. Gateway TLS identity
  persists across restarts and renews only through an explicit host command.

- Commit user evidence and its delivery outbox atomically; retry stable IDs without
  duplicate MemoryGate records. Serialize concurrent message sequence allocation.
- Retrieve and retain exact per-turn memory context; expose pending ingestion and
  retrieval gaps in turn/session responses and authenticated `GET /memory`.
- Propagate offline forgetting as durable deletions, clear cached contexts and
  suppress retrieval until MemoryGate acknowledges deletion.

- Add offline, owner-operated forgetting of sessions and their fork descendants,
  including summaries and approval provenance held by Pi. Runtime history stays
  append-only; immutable content-free receipts and message envelopes survive.
- Add authenticated `GET /messages/{id}` for evidence citations. Forgotten sources
  return explicit tombstones with deletion time and receipt ID. Sessions expose
  the same tombstones and a new `forgotten` status; their turns cannot resume.
- Refuse deletion while Pi is running and refuse startup after interrupted
  database cleanup until the owner retries. Preserve execution outcomes and
  accounting without retaining conversation payloads.

## 0.3.0

The action boundary, and the approval round trip.

- **Pi can act, and only through ToolGate.** Pi executes nothing itself. It is
  given a *scoped* execution key and asks ToolGate what that key may reach on
  every turn rather than caching it, because the owner can change scope at any
  moment and a cached list would let Pi offer a tool it no longer has.
- **A tool call is a line of JSON the loop owns**, not a provider's native
  tool-calling schema. Pi routes across local, free hosted and paid models and
  their formats disagree; a format the loop owns behaves identically everywhere.
  The parser is anchored to a whole line, so prose *about* a tool call is not
  acted on as one.
- **A tool outside the key's scope is never forwarded.** ToolGate would refuse
  it, but forwarding would put an unscoped tool id in its audit trail on Pi's
  authority, which is not Pi's to spend.
- **A gated tool parks the turn** as `awaiting_approval` with the action stored
  whole, rather than failing it. The owner has not said no; they have not been
  asked yet. A restart does not withdraw the question, and `GET /approvals` is
  one queue across all sessions — an approval nobody sees is an action that
  silently never happens.
- **Resuming replays the stored action**, never one rebuilt from the
  conversation, so an approval cannot be spent on a different action than the
  one the owner was shown. A stale approval re-parks the turn on the *new*
  request instead of leaving it behind a dead nonce it could never clear.
- **An action that happened is never recorded as one that did not.** A tool can
  succeed and the model can then fail to say so. Those turns are recorded as
  `acted_no_reply`, not `failed`, and resuming asks only for the missing reply
  without running the action a second time. Neither route answers with an error
  status in that case: an error code invites a retry, and retrying would do the
  thing twice. `GET /turns/unreplied` lists them. Found by a live round trip
  against ToolGate, where the tool ran, the local model timed out afterwards,
  and the turn claimed the action had failed.
- **Timeouts are configuration, and generous locally.** `PI_LOCAL_TIMEOUT_S`
  defaults to 600s. The previous fixed 120s ceiling fired on a local model that
  was still thinking and turned *slow* into *failed*.
- `GET /tools`, `GET /approvals`, `GET /turns/unreplied` and
  `POST /turns/{id}/resume` are new; `/health` gains an `action_boundary` check.
  Turns gain `acted`. A database created by an earlier version is migrated in
  place.
- **`/health` reported `0.1.0` while the module was at `0.2.0`.** Fixed, and it
  is the same class of defect as the one above: a status that is quietly wrong.

## 0.2.0

Provider adapters and routing.

- **OpenRouter adapter with discovered models.** A hardcoded list is stale
  within weeks; OpenRouter carried 431 models and 22 free ones the day this was
  written, and both numbers moved by the next run.
- **Free by default, enforced in code.** Paid models are refused unless
  `PI_ALLOW_PAID_MODELS` is set, a model outside the catalogue is refused rather
  than called blind, and an unreadable price counts as *not free* - treating it
  as zero is the assumption that produces a bill.
- **Only text-in, text-out models are routed to.** Some zero-priced entries are
  audio or image generators, and a conversation routed into one fails in a way
  that is very hard to read from the answer.
- **Escalation on explicit signals**, each recorded on the turn with its reason.
  The router does not read the message: a classifier nobody evaluates deciding
  what every turn costs is worse than a crude rule that can be measured.
- **Candidate fallthrough.** Listed and priced at zero does not mean callable -
  some free models are gated to particular clients and answer 403. Pi tries the
  next candidate and records what it skipped, so a model that always refuses is
  visible rather than showing only as latency. A bad key or exhausted quota is
  deliberately not treated this way; those fail on every candidate alike.
- `GET /models` reports what Pi can route to, and `turns` gain `route_tier` and
  `route_reason`. A database created by 0.1.0 is migrated in place.

## 0.1.0

First release: sessions and the turn loop.

- **Append-only history, enforced by the database.** No function updates or
  deletes a message and the schema refuses both with triggers, so a caller
  reaching past the API still cannot rewrite what was said.
- **Sessions survive restart**, and a turn that was running when the process
  stopped is marked `interrupted` with its reason rather than vanishing.
- **Forking instead of truncation** when history outgrows the window: the
  session closes with a summary and a child opens seeded by it, pointing back
  at the parent. If the summary itself cannot be written the fork still happens
  and says so, rather than leaving a child claiming context it does not have.
- **A failed turn keeps the message that was sent.** It was said.
- Turns record provider, model, tokens, cached tokens, cost and latency. A
  provider that reports no price yields `null`, never `0`.
- `GET /health` in the module contract shape, probing the store and the provider.
- Refuses to start without an admin key of at least 16 characters.
- One provider adapter, Ollama, so the loop can be exercised end to end at no
  cost. Routing and the rest of the adapters are #28.

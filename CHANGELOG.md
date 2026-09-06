# Changelog

Versions are the module's own, not an API revision. A change to the shape of any
endpoint is a contract change and gets its own entry — replacing a module has to
be a decision with visible consequences.

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

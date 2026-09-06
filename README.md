# Pi

Conker's runtime. Agent turns, sessions, jobs, model routing, execution history.

Pi is the only service the browser talks to. Everything the owner sees passes through here, which
is what keeps provider keys, admin keys and host paths off the client.

## Its boundary

**Pi coordinates and never owns.** It holds **no store beyond session state and execution history**.

- Memory belongs to **MemoryGate**. Pi owns transcripts; what crosses into MemoryGate is derived
  evidence, never these rows ([ADR-0002](https://github.com/alexeybe1kin/conker/blob/main/docs/adr/0002-transcripts-and-evidence.md)).
- Actions belong to **ToolGate**. If it touches anything outside Pi's own store, it goes through
  ToolGate — no local shell, no file access
  ([ADR-0005](https://github.com/alexeybe1kin/conker/blob/main/docs/adr/0005-toolgate-is-the-only-action-path.md)).
- Machine truth belongs to **SystemGate**, read-only.

Pi is built in-house rather than adopted because no existing runtime could leave the action
boundary whole ([ADR-0001](https://github.com/alexeybe1kin/conker/blob/main/docs/adr/0001-build-pi-in-house.md)).

## Two rules that are structural, not stylistic

**History is append-only.** There is no function that updates or deletes a message, and the schema
refuses both with triggers — so a caller reaching past the API still cannot rewrite what was said.
Current models bind reasoning blocks to the producing model and reject edited history; by the time
that surfaces, the offending code is everywhere.

**Long conversations fork, they are never truncated.** When history outgrows the window, the session
closes with a summary and a child opens seeded by it, pointing back at the parent. Dropping the
middle would silently lose what was said; rewriting it would break the first rule. Forking keeps the
lineage walkable, which is also how MemoryGate treats evidence.

## Run

```bash
cp .env.example .env
echo "PI_ADMIN_KEY=$(openssl rand -base64 24)" >> .env
docker network create conker_net   # if it does not exist yet
docker compose up -d --build
```

API: `http://127.0.0.1:8050`. Every route except `/health` requires `X-Pi-Key: <PI_ADMIN_KEY>`.

Pi **refuses to start** without a key of at least 16 characters, and says how to fix it. It never
falls back to open.

## Configure

Precedence is **environment → file → default**.

| Variable | Default | Meaning |
|---|---|---|
| `PI_ADMIN_KEY` | *(required)* | At least 16 characters, or Pi will not start. |
| `PI_DB_PATH` | `/data/pi.db` | Sessions and execution history. **Back this up** — it is the transcript, and MemoryGate's evidence cites message ids from it. |
| `PI_OLLAMA_URL` | `http://ollama:11434` | Where the local model is. |
| `PI_MODEL` | `qwen3:4b` | The local model, used for ordinary conversation. |
| `PI_OPENROUTER_KEY` | *(empty)* | Optional. Without it Pi answers locally and says so. |
| `PI_ALLOW_PAID_MODELS` | *(off)* | Opt in to models that cost money. Off means **free only**, enforced in code. |
| `PI_SYSTEM_PROMPT` | *(empty)* | Prepended to every conversation. |

## API

| Route | Auth | |
|---|---|---|
| `GET /health` | none | Module contract shape. Probes the store and the provider. |
| `POST /sessions` | key | Open a session. |
| `GET /sessions` | key | List sessions, newest first. |
| `GET /sessions/{id}` | key | The session with its messages and turns. |
| `POST /sessions/{id}/turns` | key | Run one turn. |
| `POST /sessions/{id}/fork` | key | Close with a summary and open a child. |

A turn returns the session that **answered**, which may not be the one you asked: if history had
outgrown the window it forked first, and `forked_from` says so rather than leaving you to assume.

`POST /turns` answers **503** when the provider does not answer. The message you sent is stored
either way — it was said, and a transcript that drops what was said because the answer failed is not
a transcript.

## Turns are recorded, not just run

Every turn stores provider, model, input and output tokens, cached tokens, cost and latency. A
provider that does not report a price yields `null`, which renders as **unknown** — never `0`, which
would read as free.

**A turn that was running when the process stopped is marked `interrupted` at the next startup**,
with the reason. Saying nothing would leave the owner looking at a request that simply vanished.

## Status vocabulary

`/health` reports `ok`, `degraded`, `unavailable`, `not_configured` or `unknown` per check.
`not_configured` is **not** a failure. Nothing is ever `ok` because it was configured — every check
is probed.

## Routing

Routing is not a feature - it is the cost structure of a system that runs all day. A **local model
carries ordinary conversation**, and Pi escalates only on explicit signals:

| Signal | Reason recorded |
|---|---|
| The owner asked for a stronger model | `owner_asked` |
| The turn needs tools | `tools_required` |
| A previous attempt failed | `retry_after_failure` |
| The work is analysis, not conversation | `analysis` |
| History has grown large | `long_context` |

Every route is recorded on the turn with its **reason**, because a policy nobody measures drifts
into always escalating - and `features.md` A6 names the trap directly: a cheap model that fails and
then escalates has cost both.

The router is deliberately **not** a classifier reading the message. That would be a model nobody
evaluates deciding what every turn costs. It sees only facts the loop already has, and the caller
passes what it genuinely knows.

**Free by default.** Models are **discovered, not hardcoded** - a static list is stale within weeks.
Paid models are refused unless `PI_ALLOW_PAID_MODELS` is set, and a model outside the catalogue is
refused rather than called blind, so a typo cannot become a bill. An unreadable price counts as
**not free**, because treating it as zero is exactly the assumption that produces one.

Only text-in, text-out models are routed to. Some zero-priced entries are audio or image generators
- Google's Lyria outputs `["text", "audio"]` - and routing a conversation into one is a strange
failure to diagnose from the answer alone.

**Listed and free does not mean callable.** Some free models are gated to particular clients and
answer `403`. Pi walks its candidate list rather than failing the turn, and **records what it
skipped** - so a model that always refuses is visible in the record rather than only as latency. A
bad key or an exhausted quota is *not* treated this way: those fail identically on every candidate,
and walking the catalogue would be slow and would blame the models.

`GET /models` shows what Pi can route to right now, free and paid, with the count discovered.

## Not here yet

Tool calls through ToolGate are [#29](https://github.com/alexeybe1kin/conker/issues/29). Jobs and
cron come with C2.

## Licence

MIT.

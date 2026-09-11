# Durable conversation memory

Pi commits each user message and an outbox entry together. The entry contains a
message ID, not another copy of the text. A background worker delivers quoted
owner evidence to MemoryGate under that ID. Assistant replies, tool results and
system instructions are not admitted as owner evidence. Existing user messages
are queued on upgrade; existing forgetting tombstones queue deletions instead.

MemoryGate acknowledges its SQL commit. A timeout after that commit is safe:
retrying the same ID and content returns the same receipt. Pi retains failed
deliveries across restarts, retries with backoff capped at 60 seconds, and exposes
the pending count and a concrete diagnostic. Admission as evidence is distinct
from promotion into memory: receipts report `admitted`, `filtered` or `deleted`.

Before a new turn calls a provider, Pi retrieves a bounded context package and
commits exactly that package against the turn ID. Tool continuations and resumed
turns use the same package. The package includes confidence, source citations,
statement dates and retrieval limitations. It is marked as untrusted evidence in
the prompt and cannot grant execution permission. Retrieval has a five-second
HTTP timeout by default (a local service probe took 2.29 seconds cold and 0.65
seconds warm during this patch); errors and oversized packages produce a recorded gap
and the turn continues. Semantic degradation and pending vector writes remain
visible even when literal matching returns useful results.

## Configure

Issue a read key in MemoryGate's Settings, or through its authenticated
`POST /auth/agent-keys` endpoint, for the intended agent. Give Pi that
read key, never MemoryGate's admin key. Generate a separate random ingestion key
of at least 16 characters and set it on both services:

```dotenv
# Pi
PI_MEMORYGATE_URL=http://memorygate-api:8020
PI_MEMORYGATE_AGENT_ID=agent_pi_operator
PI_MEMORYGATE_READ_KEY=<read key for agent_pi_operator>
PI_MEMORYGATE_INGEST_KEY=<separate random ingestion key>
PI_MEMORYGATE_TIMEOUT_S=5

# MemoryGate
MEMORYGATE_CONVERSATION_KEY=<same ingestion key>
MEMORYGATE_CONVERSATION_AGENT_ID=agent_pi_operator
```

Standalone Compose loads these from `.env`. The umbrella installer/Compose must
pass these variables explicitly and publish/pin both updated images; this patch
does not change the umbrella repository. Do not change agent identity to retry a
queue: it is part of the receiver's deduplication namespace. Missing configuration
is reported as degraded memory, and partial configuration refuses startup with
the missing variable names. The transcript remains useful without MemoryGate.

The existing MemoryGate bootstrap read-key helper can reactivate revoked keys.
Use a separately issued key and remove its bootstrap configuration; correcting
that helper is a separate security fix.

## Interface contract

Turn responses, including parked approvals and acted-without-reply responses,
carry `memory`. `GET /sessions/{id}` reports current delivery status and the saved
context for each turn. `GET /memory` provides authenticated global progress.
Provider failures also carry memory status in their error detail. Error `detail`
on turn/resume failures is now an object with `message` and `memory`, rather than
a string. The `notices`
array is authored by Pi, independently of the model: “Conversation saved.
Long-term memory is pending” and a separate context-gap notice when applicable.
The browser must render these notices beside the turn and refresh delivery
status; no browser application is implemented in this repository. `/health`
checks MemoryGate reachability and reports queued work as degraded.

## Forgetting and recovery

The offline forgetting command still requires every Pi process to stop. In the
same redaction transaction it converts affected outbox entries into deletions
and clears **all** saved context packages, since externally supplied lineage can
be incomplete. The preview names this additional effect. Shutdown joins the
outbox worker before releasing the offline-operation lease.

While any remote deletion is unacknowledged, new turns use no MemoryGate context
and show why. MemoryGate accepts a deletion even before first ingestion and
retains a tombstone that rejects late uploads. It deletes the quoted memory and
owned analysis/revisions, blanks source evidence, and queues vector removal.
SQL retrieval cannot return a deleted row while vector cleanup is pending.
`GET /messages/{id}` continues to resolve the original citation as `forgotten`.

This removes the new pipeline's stored source and cached context. It does not
erase arbitrary paraphrases in other conversations, external exports, provider
logs, independently authored records, or old backups. Existing backup/restore
procedures must retain and reapply deletion records. MemoryGate deletion is
logical redaction, not erasure of PostgreSQL WAL or backup media.

## Verification

```sh
python -m pytest tests/test_memory.py
python scripts/memory_mutation_drill.py
```

The drill breaks eight guarantees in isolated copies and requires behavioral
test failures. Import failures, skipped tests and a failing baseline are errors,
not successful mutation catches. The cross-service English/Russian drill lives
in MemoryGate's `integrations/test_pi_memory.py`; see its memory documentation.

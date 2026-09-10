# Forgetting a Pi conversation

Forgetting removes the content of one session and its fork descendants from
Pi's active database. The preview lists the exact affected identifiers. This
scope includes descendants because their summaries and replies can retain what
was said in the parent. Pi does not yet track the provenance needed to safely
forget an individual message while keeping its derived replies.

## Owner operation

This is an offline host operation, authorised by the owner's OS/database access.
There is no deletion endpoint, model tool, or turn-loop capability. Pi's shared
API key cannot distinguish the owner from a runtime caller, so it cannot grant
deletion authority. The confirmation below confirms scope; it is not a password.
An administrator capable of arbitrary Python or SQLite DDL already has database
authority; SQLite triggers are not a sandbox against such an administrator.

Use the updated Pi code. Stop **every** Pi process that uses this database,
including any older version, and stop external database writers. Older Pi
versions do not participate in the new runtime lease. Do not start an older
version against a database with forgotten sessions.

From the Pi repository, with its Python dependencies installed:

```bash
python -m pi.forgetting --db /absolute/path/pi.db preview --session ses_example
python -m pi.forgetting --db /absolute/path/pi.db forget --session ses_example --confirm CONFIRMATION_FROM_PREVIEW
```

For the standalone Pi Compose file, the equivalent commands use its data volume:

```bash
docker compose stop pi
docker compose run --rm --no-deps pi python -m pi.forgetting --db /data/pi.db preview --session ses_example
docker compose run --rm --no-deps pi python -m pi.forgetting --db /data/pi.db forget --session ses_example --confirm CONFIRMATION_FROM_PREVIEW
docker compose up -d pi
```

Build the updated image before these commands if it is not installed yet. The
command never contacts a model, ToolGate, or MemoryGate. It prints JSON: the
preview contains identifiers and a confirmation; successful forgetting returns
a receipt. Review the session IDs before confirming. If messages, turns or fork
descendants were added after preview, obtain and review a new preview.

Only restart after a successful exit. If cleanup was interrupted, rerun the same
forget command. It finishes cleanup without changing the original receipt or
deletion time. If the original command was lost, `preview` on that same session
returns the confirmation again. Pi refuses startup while cleanup is unfinished.
An error never reports success.

## What is removed and retained

Removed: message payloads, affected session titles and summaries, turn approval
intent and arguments, routing explanations, and error/details text. Completed
actions are not undone. Turn outcome, `acted`, accounting, tool/approval IDs and
timestamps remain as execution facts. The session becomes `forgotten` and
cannot accept messages, fork, or resume any turn. Its parked turns disappear
from the resumable queues.

Messages retain their original IDs, session IDs, sequence numbers, roles and
timestamps. Sequences are never renumbered and IDs cannot be replaced. Immutable
receipts record deletion ID, time, affected session IDs, message/turn counts
and a confirmation derived solely from identifiers. They contain neither the
removed text nor its hash. If a child was already forgotten, its original
receipt survives a later deletion of its parent.

Normal SQL UPDATE and DELETE of messages still fail. The offline command holds
an exclusive lifetime lease, removes the UPDATE trigger only inside the
redaction transaction, reinstates it before commit, and records the receipt
in that same transaction. Failure rolls all of that back. The DELETE trigger
stays active throughout. INSERT OR REPLACE cannot overwrite a message or receipt.

Pi holds a shared lease for each Store's full lifetime, including provider calls
between database connections. The lease lives in `pi.db.access.sqlite`, beside
the transcript, and relies on local filesystem SQLite locks. Do not use hard-link
aliases or independently mounted lock files for the same database. The lease
file contains no conversation content and can be recreated when all processes
are stopped. The durable unfinished-cleanup marker is in **pi.db**, so a database
backup retains it.

The command enables SQLite secure deletion, truncates the WAL, vacuums the live
database and checkpoints again before clearing that marker. It does not promise
forensic erasure from filesystem snapshots, storage-controller copies or swap.

## Evidence citations

Authenticated `GET /messages/{message_id}` returns the original live message, or
HTTP 200 with its stable envelope and an explicit tombstone:

```json
{
  "id": "msg_example",
  "session_id": "ses_example",
  "seq": 7,
  "role": "user",
  "created_at": 1789000000,
  "content": null,
  "content_status": "forgotten",
  "receipt_id": "del_example",
  "forgotten_at": 1789010000
}
```

Unknown IDs return 404. `GET /sessions/{id}` uses the same tombstones in its
message list. A citation holder should display **“Source forgotten by owner on
[date]; original content is unavailable.”** A tombstone cannot verify a claim,
and must not be presented as supporting evidence or invented replacement text.

This patch defines and serves that contract in Pi; it does not change MemoryGate
to fetch it, invalidate derived claims or delete its copies. Sparse citations,
including missing Russian evidence, do not alter the meaning of a tombstone.

## Limits that remain

This removes content from Pi's current store, not from everywhere it has been.
MemoryGate evidence, ToolGate approval/provenance records, provider retention,
exports, browser caches and copies in unrelated sessions need their own deletion
paths. ToolGate approval revocation is also separate; Pi refuses to resume the
forgotten turn but does not revoke that approval in ToolGate.

Backups made before deletion still contain the content. A restore must replay
subsequent deletions before exposing a restored database. Cross-service deletion
propagation and receipt replay during restore remain integration work; do not
describe this command as system-wide forgetting. Fine-grained message deletion
and an authenticated browser owner interface are also deferred.

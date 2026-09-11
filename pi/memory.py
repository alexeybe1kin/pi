"""Bounded memory retrieval and a restartable, at-least-once delivery worker."""

import json
import logging
import threading
import time

import httpx

from . import memory_store

log = logging.getLogger("pi.memory")


class MemoryClient:
    def __init__(
        self, url, ingest_key, read_key, agent_id="default", *, timeout=5.0, transport=None
    ):
        self.http = httpx.Client(
            base_url=url.rstrip("/"), timeout=timeout, follow_redirects=False, transport=transport
        )
        self.ingest_headers = {"X-MemoryGate-Conversation-Key": ingest_key, "X-Agent-Id": agent_id}
        self.read_headers = {"X-MemoryGate-Key": read_key, "X-Agent-Id": agent_id}

    def deliver(self, operation, message):
        url = "/runtime/conversation/" + message["id"]
        if operation == "delete":
            response = self.http.delete(url, headers=self.ingest_headers)
        else:
            response = self.http.put(
                url,
                headers=self.ingest_headers,
                json={
                    "session_id": message["session_id"],
                    "content": message["content"],
                    "created_at": message["created_at"],
                },
            )
        response.raise_for_status()
        receipt = response.json()
        allowed = {"deleted"} if operation == "delete" else {"admitted", "filtered", "deleted"}
        if receipt.get("message_id") != message["id"] or receipt.get("state") not in allowed:
            raise ValueError("MemoryGate did not acknowledge the expected message")
        return {
            key: receipt.get(key)
            for key in ("id", "message_id", "state", "memory_id", "value_score")
        }

    def retrieve(self, query):
        response = self.http.post(
            "/runtime/context",
            headers=self.read_headers,
            json={"query": query[:16000], "max_items": 8, "include_evidence": False},
        )
        response.raise_for_status()
        package = response.json()
        if (
            not isinstance(package, dict)
            or not isinstance(package.get("memories"), list)
            or not isinstance(package.get("retrieval"), dict)
        ):
            raise ValueError("Invalid context package")
        # Store exactly what is supplied to the model, bounded independently of
        # the remote service. Oversize context is a visible gap, never truncation.
        if len(json.dumps(package, ensure_ascii=False).encode()) > 32768:
            raise ValueError("Memory context exceeds the turn budget")
        return package

    def close(self):
        self.http.close()

    def health(self):
        try:
            response = self.http.get("/health")
            response.raise_for_status()
            state = response.json().get("status")
            if state in {"ok", "degraded", "unavailable"}:
                return {"status": state}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        return {"status": "unavailable", "reason": "MemoryGate health could not be verified"}


class Memory:
    def __init__(self, store, client=None):
        self.store, self.client = store, client
        self.stop_event = threading.Event()
        self.thread = None

    def prepare(self, turn_id, query):
        state, package = "not_configured", None
        if memory_store.pending_deletions(self.store):
            state = "unavailable"
        elif self.client:
            try:
                package = self.client.retrieve(query)
                state = (
                    "ok"
                    if package["retrieval"].get("semantic", {}).get("status") == "ok"
                    and not package["retrieval"].get("pending_conversation_index")
                    else "degraded"
                )
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                state = "unavailable"
        memory_store.save_context(self.store, turn_id, state, package)

    def status(self, session_id=None, turn_id=None):
        return memory_store.status(
            self.store, session_id, turn_id, configured=self.client is not None
        )

    def health(self):
        if not self.client:
            return {"status": "degraded", "reason": "Long-term memory is not configured"}
        state = self.client.health()
        delivery = self.status()
        if state["status"] == "ok" and (
            delivery["pending_ingestion"] or delivery["pending_deletion"]
        ):
            return {"status": "degraded", "reason": "Memory delivery or deletion is pending"}
        return state

    def drain_once(self, limit=20):
        if not self.client:
            return 0
        with self.store._connect() as db:
            jobs = db.execute(
                "SELECT * FROM memory_outbox WHERE state='pending' AND next_at<=?"
                " ORDER BY operation='delete' DESC,next_at,message_id LIMIT ?",
                (time.time(), limit),
            ).fetchall()
        completed = 0
        for job in jobs:
            if self.stop_event.is_set():
                break
            message = self.store.get_message(job["message_id"])
            operation = job["operation"]
            if message.get("content_status") == "forgotten":
                operation = "delete"
            try:
                receipt = self.client.deliver(operation, message)
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                with self.store._connect() as db:
                    db.execute(
                        "UPDATE memory_outbox SET attempts=attempts+1,next_at=?,error=?"
                        " WHERE message_id=? AND operation=? AND state='pending'",
                        (
                            time.time() + min(60, 2 ** min(job["attempts"] + 1, 6)),
                            type(exc).__name__
                            + ": check MemoryGate connectivity and credentials; retry scheduled",
                            job["message_id"],
                            job["operation"],
                        ),
                    )
            else:
                with self.store._connect() as db:
                    db.execute(
                        "UPDATE memory_outbox SET state='sent',receipt=?,error=''"
                        " WHERE message_id=? AND operation=? AND state='pending'",
                        (json.dumps(receipt), job["message_id"], job["operation"]),
                    )
                completed += 1
        return completed

    def start(self):
        if self.client and self.thread is None:
            self.thread = threading.Thread(target=self._run, name="pi-memory-outbox", daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.drain_once()
            except Exception:
                log.exception(
                    "Memory delivery paused; check Pi database access. Pending rows will retry."
                )
            self.stop_event.wait(1)

    def close(self):
        self.stop_event.set()
        if self.thread:
            # The offline forgetting lease must outlive every possible upload.
            self.thread.join()
        if self.client:
            self.client.close()

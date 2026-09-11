"""Reintroduce the September audit defects in isolated copies, never the checkout."""

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXEC = "tests/test_audit_execution.py"
MODELS = "tests/test_audit_models.py"
MEMORY = "tests/test_audit_memory.py"
# Each mutation must still collect and run; syntax/import failures are not evidence.
CASES = [
    (
        "F1-missing-action-id",
        "pi/toolgate.py",
        '"action_id": action_id, "job_id": job_id',
        '"legacy_action_id": action_id, "job_id": job_id',
        EXEC,
    ),
    (
        "F1-resume-new-identity",
        "pi/loop.py",
        "action = actions.latest(self.store, turn_id)",
        'action = {**actions.latest(self.store, turn_id), "id": ToolGateClient.new_action_id()}',
        EXEC,
    ),
    ("F1-dropped-owner-job", "pi/toolgate.py", '"job_id": job_id}', '"job_id": None}', EXEC),
    (
        "F1-budget-denial-enters-model-loop",
        "pi/loop.py",
        'if refusal.code == "BUDGET_DENIED":',
        "if False:",
        EXEC,
    ),
    (
        "F2-ignores-request-and-extra-fees",
        "pi/openrouter.py",
        "for value in pricing.values()",
        'for value in (pricing["prompt"], pricing["completion"])',
        MODELS,
    ),
    (
        "F2-incomplete-prices-allowed",
        "pi/openrouter.py",
        'required = {"prompt", "completion", "request"}',
        "required = set()",
        MODELS,
    ),
    (
        "F2-unknown-cost-is-zero",
        "pi/openrouter.py",
        "value = pricing.get(key)",
        "value = pricing.get(key) or 0.0",
        MODELS,
    ),
    (
        "F2-real-charge-erased",
        "pi/openrouter.py",
        'cost = _price(usage, "cost")',
        "cost = 0.0",
        MODELS,
    ),
    (
        "F2b-missing-ok-is-success",
        "pi/toolgate.py",
        'return ToolPending("outcome_unknown",\n'
        '                           "No affirmative or negative execution receipt", action_id)',
        "return ToolResult(True, result, tool_id)",
        EXEC,
    ),
    (
        "F2b-in-progress-is-success",
        "pi/toolgate.py",
        'return ToolPending("action_in_progress",\n'
        '                               "Dispatch recorded; outcome pending", action_id)',
        "return ToolResult(True, None, tool_id)",
        EXEC,
    ),
    ("F2b-negative-receipt-marks-acted", "pi/actions.py", "int(outcome.ok)", "1", EXEC),
    (
        "F2b-unknown-redispatches",
        "pi/loop.py",
        'outcome = self.toolgate.check_action(action["id"], action["tool_id"])',
        'outcome = self.toolgate.invoke(action["tool_id"], action["args"], action_id=action["id"])',
        EXEC,
    ),
    (
        "F4-two-resume-owners",
        "pi/loop.py",
        'if not self.store.claim_turn(turn_id, turn["status"]):',
        "if False:",
        EXEC,
    ),
    (
        "F4-stale-finish-overwrites-success",
        "pi/store.py",
        'WHERE id=? AND status=?",\n'
        '                (status, time.time(), *fields.values(), turn_id, expected_status)',
        'WHERE id=? AND ? IS NOT NULL",\n'
        '                (status, time.time(), *fields.values(), turn_id, expected_status)',
        EXEC,
    ),
    (
        "F4-restart-hides-acted-turn",
        "pi/store.py",
        "WHEN acted=1 THEN 'acted_no_reply'",
        "WHEN acted=1 THEN 'failed'",
        EXEC,
    ),
    (
        "F4-legacy-acted-omitted",
        "pi/store.py",
        "OR (status='interrupted' AND acted=1)",
        "OR (status='interrupted' AND acted=0)",
        EXEC,
    ),
    ("F4-receipt-observation-lost", "pi/actions.py", "db.commit()", "pass", EXEC),
    (
        "F5-current-namespace-for-deletion",
        "pi/memory.py",
        'headers = {**self.ingest_headers, "X-Agent-Id": agent_id}',
        "headers = self.ingest_headers",
        MEMORY,
    ),
    (
        "F5-destination-not-pinned",
        "pi/memory_store.py",
        "(destination, message_id))",
        "(None, message_id))",
        MEMORY,
    ),
    (
        "F5-wrong-namespace-receipt-accepted",
        "pi/memory.py",
        'receipt.get("id") != expected_id',
        "False",
        MEMORY,
    ),
    (
        "F5-legacy-origin-guessed",
        "pi/memory_store.py",
        "if row[0] is None and row[1]:",
        "if False:",
        MEMORY,
    ),
    ("F8-api-unbounded", "pi/api.py", "min_length=1, max_length=16000,", "min_length=1,", MEMORY),
    (
        "F8-permanent-errors-retry",
        "pi/memory.py",
        '"blocked" if permanent else "pending",',
        '"pending",',
        MEMORY,
    ),
    (
        "F8-blocked-deletion-allows-recall",
        "pi/memory_store.py",
        "operation='delete' AND state!='sent'",
        "operation='delete' AND state='pending'",
        MEMORY,
    ),
    (
        "F9-catalogue-removes-local",
        "pi/routing.py",
        "except ProviderUnavailable as exc:",
        "except KeyError as exc:",
        MODELS,
    ),
    (
        "F11-summary-is-system",
        "pi/loop.py",
        'Message("assistant", "Untrusted model summary',
        'Message("system", "Untrusted model summary',
        MODELS,
    ),
]


def main():
    scratch = ROOT / ".test-tmp"
    scratch.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="audit-mutations-", dir=scratch))
    for name, filename, old, new, selected in [("baseline", None, None, None, None), *CASES]:
        target = run / name
        for directory in ("pi", "gateway", "tests"):
            shutil.copytree(
                ROOT / directory, target / directory, ignore=shutil.ignore_patterns("__pycache__")
            )
        if filename:
            file = target / filename
            source = file.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(f"{name}: mutation site changed; update the drill")
            file.write_text(source.replace(old, new), encoding="utf-8")
        report = target / "results.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *([selected] if selected else [EXEC, MODELS, MEMORY]),
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(target / "temp"),
                "--junitxml",
                str(report),
            ],
            cwd=target,
            env={**os.environ, "PYTHONPATH": str(target)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        (target / "output.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        suites = list(ET.parse(report).getroot().iter("testsuite")) if report.exists() else []
        failed = sum(int(suite.get("failures", 0)) for suite in suites)
        invalid = sum(int(suite.get(key, 0)) for suite in suites for key in ("errors", "skipped"))
        valid = (
            bool(suites)
            and not invalid
            and (
                result.returncode == 0
                if name == "baseline"
                else result.returncode == 1 and failed > 0
            )
        )
        if not valid:
            print(f"FAILED: {name}; inspect {target / 'output.txt'}", flush=True)
            return 1
        print(f"{name}: {'passed' if name == 'baseline' else 'caught'}", flush=True)
    print(f"All {len(CASES)} mutants caught. Evidence: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

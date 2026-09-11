"""Break each guarantee in an isolated copy; collection errors never count as a catch."""

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("message-without-outbox", "pi/memory_store.py", "WHEN NEW.role='user'", "WHEN 0"),
    ("sequence-race", "pi/store.py", 'db.execute("BEGIN IMMEDIATE")', 'db.execute("SELECT 1")'),
    (
        "lost-ack-drops-evidence",
        "pi/memory.py",
        "SET attempts=attempts+1,next_at=?,error=?",
        "SET state='sent',attempts=attempts+1,next_at=?,error=?",
    ),
    ("context-not-used", "pi/loop.py", 'if saved["package"]:', "if False:"),
    (
        "forgetting-uploads-text",
        "pi/forgetting.py",
        'memory_store.redact(db, plan["session_ids"])',
        "pass",
    ),
    (
        "stale-recall-during-deletion",
        "pi/memory.py",
        "if memory_store.pending_deletions(self.store):",
        "if False:",
    ),
    ("hidden-memory-gap", "pi/memory_store.py", '"notices": notices,', '"notices": [],'),
    ("shutdown-releases-inflight-upload", "pi/memory.py", "self.thread.join()", "pass"),
]
SOURCE = "pi"
TEST = "tests/test_memory.py"
IMPORT = "."
SCRATCH = ".test-tmp"


def main():
    scratch = ROOT / SCRATCH
    scratch.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="memory-mutations-", dir=scratch))
    for name, filename, old, new in [("baseline", None, None, None), *CASES]:
        target = run / name
        shutil.copytree(
            ROOT / SOURCE, target / SOURCE, ignore=shutil.ignore_patterns("__pycache__")
        )
        (target / TEST).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / TEST, target / TEST)
        if filename:
            file = target / filename
            source = file.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise RuntimeError(
                    f"Mutant {name} no longer matches exactly one site; update the drill"
                )
            file.write_text(source.replace(old, new), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(target / IMPORT) + os.pathsep + env.get("PYTHONPATH", "")
        report = target / "results.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                TEST,
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(target / "temp"),
                "--junitxml",
                str(report),
            ],
            cwd=target,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        (target / "output.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        suites = ET.parse(report).getroot().iter("testsuite") if report.exists() else []
        suites = list(suites)
        failed = sum(int(suite.get("failures", 0)) for suite in suites)
        errors = sum(int(suite.get("errors", 0)) for suite in suites)
        skipped = sum(int(suite.get("skipped", 0)) for suite in suites)
        valid = (
            bool(suites)
            and not errors
            and not skipped
            and (
                result.returncode == 0
                if name == "baseline"
                else result.returncode == 1 and failed > 0
            )
        )
        if not valid:
            print(f"FAILED: {name}; inspect {target / 'output.txt'}")
            return 1
        print(f"{name}: {'passed' if name == 'baseline' else 'caught'}", flush=True)
    print(f"All {len(CASES)} mutants caught. Evidence: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

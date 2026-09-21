"""Run pytest in a disposable ERP environment and preserve complete artifacts.

This is intentionally a small process wrapper: it does not interpret pytest
results or change the selected tests.  It only records the exact child process,
streams its merged stdout/stderr to a fresh log, and writes result.json after
the process has terminated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import UTC, datetime


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def build_environment(report_dir: Path) -> tuple[dict[str, str], Path]:
    test_root = report_dir / "pytest-isolated"
    test_root.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    environment.update(
        {
            "ERP_ENVIRONMENT": "test",
            "ERP_DATABASE_PATH": str(test_root / "carton_erp.sqlite3"),
            "ERP_BACKUP_DIR": str(test_root / "backups"),
            "ERP_SECRET_KEY_FILE": str(test_root / "session_secret.key"),
            "ERP_SECRET_KEY": "pytest-wrapper-isolated-only",
            "PYTHONUTF8": "1",
        }
    )
    return environment, test_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to pytest; use -- before the first pytest argument.",
    )
    args = parser.parse_args()
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    if not pytest_args:
        parser.error("supply pytest arguments after --")

    report_dir = args.report_dir.resolve()
    if report_dir.exists():
        parser.error(f"report directory already exists: {report_dir}")
    report_dir.mkdir(parents=True)
    log_path = report_dir / "pytest.log"
    junit_path = report_dir / "pytest.junit.xml"
    environment, test_root = build_environment(report_dir)
    command = [sys.executable, "-m", "pytest", f"--junitxml={junit_path}", *pytest_args]
    started_at = utc_now()
    started_monotonic = time.monotonic()

    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        log.write("command=" + json.dumps(command, ensure_ascii=False) + "\n")
        log.write("started_at=" + started_at + "\n")
        log.write("erp_database_path=" + environment["ERP_DATABASE_PATH"] + "\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            log.write(line)
        exit_code = process.wait()
        ended_at = utc_now()
        duration_seconds = round(time.monotonic() - started_monotonic, 3)
        log.write(f"\nended_at={ended_at}\nexit_code={exit_code}\n")

    result = {
        "schema_version": 1,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "pid": process.pid,
        "exit_code": exit_code,
        "command": command,
        "project_root": str(PROJECT_ROOT),
        "environment": {
            "ERP_ENVIRONMENT": environment["ERP_ENVIRONMENT"],
            "ERP_DATABASE_PATH": environment["ERP_DATABASE_PATH"],
            "ERP_BACKUP_DIR": environment["ERP_BACKUP_DIR"],
            "ERP_SECRET_KEY_FILE": environment["ERP_SECRET_KEY_FILE"],
        },
        "test_root_exists_after_run": test_root.exists(),
        "artifacts": {
            "log": {"path": str(log_path), "sha256": sha256(log_path)},
            "junit": {"path": str(junit_path), "sha256": sha256(junit_path)},
        },
    }
    (report_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

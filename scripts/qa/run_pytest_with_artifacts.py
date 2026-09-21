"""Run pytest in a disposable ERP environment and preserve complete artifacts.

This is intentionally a small process wrapper: it does not interpret pytest
results or change the selected tests.  It only records the exact child process,
streams its merged stdout/stderr to a fresh log, and writes result.json after
the process has terminated.
"""

from __future__ import annotations

import argparse
import ast
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


def build_environment(report_dir: Path, *, strict: bool = False) -> tuple[dict[str, str], Path]:
    test_root = report_dir / "pytest-isolated"
    test_root.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    if strict:
        # This is an offline pytest harness, not a UAT server launcher. Refuse
        # local dotenv inheritance and never start the application lifespan.
        if (PROJECT_ROOT / ".env").exists():
            raise RuntimeError("strict offline tests require a checkout without .env")
        environment = {k: v for k, v in environment.items() if not k.startswith("ERP_")}
        tree = ast.parse((PROJECT_ROOT / "app/core/uat_isolation.py").read_text(encoding="utf-8"))
        specs_node = next(n for n in tree.body if isinstance(n, ast.AnnAssign)
                          and isinstance(n.target, ast.Name) and n.target.id == "PATH_SPECS")
        for entry in specs_node.value.elts:
            logical = ast.literal_eval(entry.elts[0])
            env_node = entry.elts[1]
            env_name = (ast.literal_eval(env_node) if isinstance(env_node, ast.Constant)
                        else "ERP_UAT_ATTESTATION_PATH")
            kind = ast.literal_eval(entry.elts[2])
            target = test_root / logical
            if kind == "directory":
                target.mkdir()
            environment[env_name] = str(target)
        temporary = test_root / "temp"
        temporary.mkdir()
        environment.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                           ERP_WORKERS="1", ERP_BIND_HOST="127.0.0.1")
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
    parser.add_argument("--strict-isolation", action="store_true")
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
    environment, test_root = build_environment(report_dir, strict=args.strict_isolation)
    if args.strict_isolation:
        if any(value.startswith("--basetemp") for value in pytest_args):
            parser.error("strict harness owns --basetemp")
        environment["TM_PHASE3_ASTRA_RESULT_DIR"] = str(report_dir / "measurements")
    command = [sys.executable, "-m", "pytest", f"--junitxml={junit_path}",
               *([f"--basetemp={test_root / 'cases'}"] if args.strict_isolation else []), *pytest_args]
    source_paths = subprocess.check_output(
        ["git", "ls-files", "-z", "app", "tests", "scripts/qa"], cwd=PROJECT_ROOT
    ).decode("utf-8").split("\0")
    source_hashes = {p: hashlib.sha256((PROJECT_ROOT / p).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                     for p in source_paths if p and (PROJECT_ROOT / p).is_file()}
    source_tree_sha256 = hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()
    (report_dir / "source-hashes.json").write_text(json.dumps(source_hashes, indent=2), encoding="utf-8")
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
        "source_tree_sha256": source_tree_sha256,
        "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT).decode().strip(),
        "source_hash_rule": "tracked app/tests/scripts/qa bytes normalized CRLF to LF, sorted JSON mapping",
        "strict_isolation": args.strict_isolation,
        "isolated_paths": {k: v for k, v in environment.items()
                           if k.startswith("ERP_") and str(test_root) in v},
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

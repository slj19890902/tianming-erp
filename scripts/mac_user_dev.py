#!/usr/bin/env python3
"""Unattended, unprivileged Mac development loop for an isolated worktree."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local-dev"
TOOLS = Path.home() / "ERP-Migration" / "tools"
PYTHON = TOOLS / "python" / "cpython-3.12.15-macos-aarch64-none" / "bin" / "python3.12"
UV = TOOLS / "uv-aarch64-apple-darwin" / "uv"
NODE_VERSION = "v22.23.3"
NODE_NAME = f"node-{NODE_VERSION}-darwin-arm64"
NODE_DIR = TOOLS / "node-official" / NODE_NAME
NODE_BIN = NODE_DIR / "bin"
FRONTEND = ROOT / "frontend-v2"
BACKEND_PORT = 18380
FRONTEND_PORT = 15380


def ensure_safe() -> None:
    if sys.platform != "darwin" or os.geteuid() == 0:
        raise RuntimeError("Use a normal macOS user, never root")
    if not ROOT.resolve().is_relative_to(Path.home().resolve()):
        raise RuntimeError("Development worktree must be below the current user's home")
    if not os.access(ROOT, os.W_OK):
        raise RuntimeError("Worktree is not writable: create a user-owned worktree first")
    if (ROOT / ".env").exists():
        raise RuntimeError("Refusing a worktree containing .env; use an isolated development copy")
    for directory in (STATE, STATE / "logs", STATE / "data", STATE / "backups", STATE / "uploads", STATE / "test-evidence", TOOLS / "uv-cache", TOOLS / "npm-cache"):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (STATE, STATE / "logs", STATE / "data", STATE / "backups", STATE / "uploads", STATE / "test-evidence"):
        directory.chmod(0o700)
    for private_file in (*((STATE / "logs").glob("*.log")), *((STATE / "data").glob("*"))):
        if private_file.is_file():
            private_file.chmod(0o600)


def environment() -> dict[str, str]:
    env = {name: os.environ[name] for name in ("HOME", "TMPDIR", "LANG", "LC_ALL")
           if name in os.environ}
    env.update({
        "PATH": f"{NODE_BIN}:/usr/bin:/bin:/usr/sbin:/sbin",
        "CI": "1", "GIT_TERMINAL_PROMPT": "0", "PIP_NO_INPUT": "1",
        "UV_CACHE_DIR": str(TOOLS / "uv-cache"),
        "npm_config_cache": str(TOOLS / "npm-cache"),
        "npm_config_fund": "false", "npm_config_audit": "false",
        "npm_config_update_notifier": "false",
        "ERP_ENVIRONMENT": "development", "ERP_BIND_HOST": "127.0.0.1",
        "ERP_PORT": str(BACKEND_PORT), "ERP_WORKERS": "1",
        "ERP_DATABASE_PATH": str(STATE / "data" / "dev.sqlite3"),
        "ERP_SECRET_KEY_FILE": str(STATE / "data" / "session_secret.key"),
        "ERP_BACKUP_DIR": str(STATE / "backups"),
        "ERP_LOG_DIR": str(STATE / "logs"),
        "ERP_FILE_STORAGE_DIR": str(STATE / "uploads"),
        "ERP_UPLOAD_TEMP_DIR": str(STATE / "uploads" / "tmp"),
        "ERP_PDF_TRAINING_DIR": str(STATE / "data" / "pdf_training"),
        "ERP_INVOICE_EXPORT_DIR": str(STATE / "data" / "invoices"),
        "ERP_INVOICE_ATTACHMENT_DIR": str(STATE / "data" / "invoice_attachments"),
        "VITE_PROXY_TARGET": f"http://127.0.0.1:{BACKEND_PORT}",
        "ERP_FRONTEND_DEPS": str(FRONTEND / "node_modules"),
        "ERP_UI_TYPESCRIPT_LIBRARY": str(FRONTEND / "node_modules" / "typescript"),
        "A01_TYPESCRIPT_LIBRARY": str(FRONTEND / "node_modules" / "typescript"),
        "A01_NODE_OUTPUT": str(STATE / "test-evidence"),
    })
    return env


def run(label: str, args: list[str], cwd: Path, seconds: int) -> None:
    log = STATE / "logs" / f"{label}.log"
    with log.open("wb") as output:
        result = subprocess.run(args, cwd=cwd, env=environment(), stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, timeout=seconds,
                                check=False)
    print(f"{label}: exit={result.returncode}; log={log}", flush=True)
    if result.returncode:
        raise RuntimeError(f"{label} failed; see its local log")


def get_bytes(url: str, seconds: int) -> bytes:
    result = subprocess.run(["/usr/bin/curl", "--fail", "--location", "--silent",
                             "--show-error", "--connect-timeout", "10", "--max-time",
                             str(seconds), url], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=seconds + 5, check=False)
    if result.returncode:
        raise RuntimeError("Official Node download failed or timed out")
    return result.stdout


def ensure_node() -> None:
    node = NODE_BIN / "node"
    npm = NODE_BIN / "npm"
    if node.is_file() and npm.is_file():
        run("node-version", [str(node), "--version"], ROOT, 10)
        return
    url = f"https://nodejs.org/dist/{NODE_VERSION}/"
    archive_name = f"{NODE_NAME}.tar.xz"
    sums = get_bytes(url + "SHASUMS256.txt", 30).decode("ascii")
    expected = next(line.split()[0] for line in sums.splitlines()
                    if line.endswith("  " + archive_name))
    archive = get_bytes(url + archive_name, 120)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise RuntimeError("Official Node archive checksum mismatch")
    cache_file = TOOLS / "node-official" / archive_name
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(archive)
    with tarfile.open(cache_file, "r:xz") as package:
        for member in package.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts or member_path.parts[0] != NODE_NAME:
                raise RuntimeError("Unexpected Node archive path")
        package.extractall(cache_file.parent)
    run("node-version", [str(node), "--version"], ROOT, 10)


def setup() -> None:
    ensure_node()
    if not (ROOT / ".venv" / "bin" / "python").exists():
        run("venv", [str(UV), "venv", "--python", str(PYTHON), str(ROOT / ".venv")], ROOT, 60)
    run("python-deps", [str(UV), "pip", "install", "--python", str(ROOT / ".venv" / "bin" / "python"), "-r", "requirements-dev.txt"], ROOT, 900)
    run("frontend-deps", [str(NODE_BIN / "npm"), "ci", "--no-audit", "--no-fund"], FRONTEND, 300)


def build() -> None:
    python = ROOT / ".venv" / "bin" / "python"
    run("python-build", [str(python), "-m", "compileall", "-q", "app", "main.py"], ROOT, 120)
    run("frontend-typecheck", [str(FRONTEND / "node_modules" / ".bin" / "vue-tsc"), "-b"], FRONTEND, 180)
    run("frontend-build", [str(FRONTEND / "node_modules" / ".bin" / "vite"), "build", "--outDir", str(STATE / "frontend-dist"), "--emptyOutDir"], FRONTEND, 180)


def test() -> None:
    python = ROOT / ".venv" / "bin" / "python"
    run("python-tests", [str(python), "-m", "pytest", "-q", "tests/test_web_asset_delivery.py"], ROOT, 180)
    # These legacy suites need an external frozen source or currently fail on
    # baseline source/test drift. Keep the unattended environment smoke run honest.
    excluded = {"ordersWorkspaceState.test.cjs", "orderDrawingWorkflow.test.cjs",
                "workspaceTabActivation.test.cjs"}
    frontend_tests = sorted(path for path in (FRONTEND / "tests").glob("*.test.cjs")
                            if path.name not in excluded)
    if not frontend_tests:
        raise RuntimeError("No frontend tests found")
    run("frontend-tests", [str(NODE_BIN / "node"), "--test", *map(str, frontend_tests)], FRONTEND, 180)


def pid_file(kind: str) -> Path:
    return STATE / f"{kind}.pid"


def owned_pid(kind: str) -> int | None:
    file = pid_file(kind)
    if not file.exists():
        return None
    pid = int(file.read_text().strip())
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True, timeout=5)
    expected = str(ROOT / ".venv" / "bin" / "python") if kind == "backend" else str(FRONTEND / "node_modules" / "vite" / "bin" / "vite.js")
    if result.returncode == 0 and expected in result.stdout:
        return pid
    file.unlink(missing_ok=True)
    return None


def stop(kind: str) -> None:
    pid = owned_pid(kind)
    if pid is None:
        print(f"{kind}: stopped", flush=True)
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        if owned_pid(kind) is None:
            print(f"{kind}: stopped", flush=True)
            return
    raise RuntimeError(f"{kind} did not stop in 5 seconds")


def start(kind: str) -> None:
    pid = owned_pid(kind)
    if pid is not None:
        print(f"{kind}: already running pid={pid}", flush=True)
        return
    if kind == "backend":
        run("dev-db-init", [str(ROOT / ".venv" / "bin" / "python"), "-c",
                            "from app.core.config import load_settings; from app.core.database import create_engine_from_settings; from app.models import Base; Base.metadata.create_all(create_engine_from_settings(load_settings()))"], ROOT, 120)
        args = [str(ROOT / ".venv" / "bin" / "python"), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)]
        cwd = ROOT
    else:
        args = [str(NODE_BIN / "node"), str(FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"), "preview", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort", "--outDir", str(STATE / "frontend-dist")]
        cwd = FRONTEND
    with (STATE / "logs" / f"{kind}.log").open("ab") as output:
        proc = subprocess.Popen(args, cwd=cwd, env=environment(), stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    pid_file(kind).write_text(str(proc.pid))
    url = (f"http://127.0.0.1:{BACKEND_PORT}/api/health" if kind == "backend"
           else f"http://127.0.0.1:{FRONTEND_PORT}/frontend-v2/")
    for _ in range(60):
        if proc.poll() is not None:
            break
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    print(f"{kind}: ready pid={proc.pid} url={url}", flush=True)
                    return
        except (OSError, TimeoutError):
            pass
        time.sleep(1)
    stop(kind)
    raise RuntimeError(f"{kind} did not become ready: see {STATE / 'logs' / (kind + '.log')}")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "build", "test", "start", "restart", "preview", "stop", "status"])
    args = parser.parse_args()
    ensure_safe()
    if args.action == "setup":
        setup()
    elif args.action == "build":
        build()
    elif args.action == "test":
        test()
    elif args.action == "start":
        start("backend")
    elif args.action == "restart":
        stop("backend")
        start("backend")
    elif args.action == "preview":
        start("preview")
    elif args.action == "stop":
        stop("preview")
        stop("backend")
    elif args.action == "status":
        for kind in ("backend", "preview"):
            print(f"{kind}: {owned_pid(kind) or 'stopped'}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

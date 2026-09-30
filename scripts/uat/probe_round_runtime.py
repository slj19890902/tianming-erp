"""Executed by the restored test runtime; never by the factory runtime."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess

root = Path(os.environ["ERP_ROUND_TEST_ROOT"]).resolve()
assert root.parent == Path("D:/tm-uat").resolve() and root.name.startswith("round-upgrade-")
assert (root / f"runtime-guard-{os.getpid()}.txt").is_file()
from app.core.config import settings
from app.core.uat_isolation import collect_actual_uat_consumers

assert settings.environment == "test"
consumers = collect_actual_uat_consumers(settings)
shared = root / "restored/shared"
for name, value in consumers.items():
    assert Path(value).resolve().is_relative_to(shared), (name, value)

blocked = []
def check_blocked(name, operation):
    try:
        operation()
    except PermissionError:
        blocked.append(name)
    else:
        raise AssertionError(f"Guard failed: {name}")

check_blocked("outside_file_write", lambda: (root.parent / "round-guard-must-not-exist.txt").write_text("guard failure"))
check_blocked("outside_database", lambda: sqlite3.connect(root.parent / "round-guard-must-not-exist.sqlite3"))
with socket.socket() as client:
    check_blocked("external_network", lambda: client.connect(("192.0.2.1", 443)))
# Different loopback ports are blocked too; probe an unused synthetic port,
# never a production listener. The audit hook rejects before the system call.
other_port = 19998 if int(os.environ["ERP_PORT"]) != 19998 else 19997
with socket.socket() as client:
    check_blocked("other_local_service", lambda: client.connect(("127.0.0.1", other_port)))
check_blocked("subprocess", lambda: subprocess.run(["cmd", "/c", "exit", "0"], check=True))
print(json.dumps({"loaded_consumers": consumers, "blocked_operations": blocked}, ensure_ascii=False))

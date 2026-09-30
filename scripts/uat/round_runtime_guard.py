"""Test-package sitecustomize: fail closed outside an explicitly named UAT root."""
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

root = Path(os.environ.get("ERP_ROUND_TEST_ROOT", "invalid")).resolve()
allowed_parents = {Path("D:/tm-uat").resolve(), Path("C:/ERP-OPT10-20260930").resolve()}
if root.parent not in allowed_parents or not root.name.startswith("round-upgrade-"):
    os._exit(78)  # sitecustomize exceptions alone do not stop Python startup.
if os.environ.get("ERP_ENVIRONMENT") != "test":
    os._exit(78)
port = int(os.environ.get("ERP_PORT", "0"))
if not 18001 <= port <= 19999:
    os._exit(78)
sys.dont_write_bytecode = True


def inside(value):
    if isinstance(value, int) or value in (None, "", ":memory:"):
        return True
    value = os.fsdecode(value)
    if value.startswith("file:"):
        value = unquote(urlsplit(value).path)
        if len(value) > 2 and value[0] == "/" and value[2] == ":":
            value = value[1:]
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).resolve().is_relative_to(root)


def guard(event, args):
    if event == "open":
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            if not inside(args[0]):
                raise PermissionError("UAT_WRITE_BLOCKED: " + str(args[0]))
    elif event == "sqlite3.connect":
        if not inside(args[0]):
            raise PermissionError("UAT_DATABASE_BLOCKED")
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"):
        if not inside(args[0]):
            raise PermissionError("UAT_FILESYSTEM_BLOCKED: " + str(args[0]))
    elif event in ("os.rename", "os.replace"):
        if not inside(args[0]) or not inside(args[1]):
            raise PermissionError("UAT_REPLACE_BLOCKED")
    elif event in ("socket.connect", "socket.connect_ex", "socket.bind"):
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
            raise PermissionError("UAT_EXTERNAL_NETWORK_BLOCKED")
        frame = sys._getframe(1)
        socket_module = sys.modules.get("socket")
        internal_pair = bool(socket_module and
            frame.f_code.co_filename == socket_module.__file__ and
            frame.f_code.co_name in ("socketpair", "_fallback_socketpair"))
        if address[1] != port and not internal_pair:
            raise PermissionError("UAT_OTHER_LOCAL_SERVICE_BLOCKED")
    elif event in ("subprocess.Popen", "os.system"):
        raise PermissionError("UAT_EXTERNAL_PROCESS_BLOCKED")


sys.addaudithook(guard)
(root / f"runtime-guard-{os.getpid()}.txt").write_text("test; own loopback port only; writes inside run root", encoding="utf8")

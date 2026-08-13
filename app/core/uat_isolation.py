from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO


SCHEMA_VERSION = 1
UAT_ROOT_ENV = "ERP_UAT_ROOT"
ATTESTATION_ENV = "ERP_UAT_ATTESTATION_PATH"
PROTECTED_PATHS_ENV = "ERP_UAT_PROTECTED_PATHS_JSON"
FORBIDDEN_ROOTS_ENV = "ERP_UAT_FORBIDDEN_ROOTS_JSON"
ISOLATION_ID_ENV = "ERP_UAT_ISOLATION_ID"
LAUNCH_NONCE_ENV = "ERP_UAT_LAUNCH_NONCE"

_UAT_OVERRIDE_NAMES = frozenset(
    {
        UAT_ROOT_ENV,
        ATTESTATION_ENV,
        PROTECTED_PATHS_ENV,
        FORBIDDEN_ROOTS_ENV,
        ISOLATION_ID_ENV,
        LAUNCH_NONCE_ENV,
        "ERP_ENVIRONMENT",
        "ERP_DATABASE_PATH",
        "ERP_DATABASE_URL",
        "ERP_BIND_HOST",
        "ERP_PORT",
        "ERP_WORKERS",
        "ERP_HEALTH_URL",
        "ERP_BROWSER_URL",
        "ERP_ALLOWED_ORIGINS",
        "ERP_TRUSTED_HOSTS",
        "ERP_TRUSTED_PROXY_IPS",
        "ERP_PRODUCTION_TRANSPORT",
        "ERP_SESSION_COOKIE_NAME",
        "ERP_SESSION_COOKIE_SECURE",
        "ERP_SESSION_EXPIRE_MINUTES",
        "ERP_SQLITE_BUSY_TIMEOUT_MS",
        "ERP_SLOW_REQUEST_MS",
        "ERP_SECRET_KEY",
        "ERP_SECRET_KEY_FILE",
        "ERP_LOG_DIR",
        "ERP_BACKUP_DIR",
        "ERP_FILE_STORAGE_DIR",
        "ERP_UPLOAD_TEMP_DIR",
        "ERP_INVOICE_EXPORT_DIR",
        "ERP_INVOICE_ATTACHMENT_DIR",
        "ERP_PDF_TRAINING_DIR",
        "ERP_TWIN_LAYOUT_RUNTIME_PATH",
        "ERP_TWIN_LAYOUT_DRAFT_PATH",
        "ERP_TWIN_LAYOUT_BACKUP_DIR",
        "ERP_LEGACY_UPLOAD_DIR",
        "ERP_DELIVERY_PRINT_SETTINGS_PATH",
        "ERP_FACTORY_TWIN_DATABASE_PATH",
        "ERP_UAT_LEASE_PATH",
        "ERP_UAT_PID_PATH",
        "ERP_UAT_STDOUT_PATH",
        "ERP_UAT_STDERR_PATH",
        "ERP_UAT_GIT_PATH",
    }
)

PATH_SPECS: tuple[tuple[str, str, str, bool], ...] = (
    ("database", "ERP_DATABASE_PATH", "file", True),
    ("log_dir", "ERP_LOG_DIR", "directory", True),
    ("backup_dir", "ERP_BACKUP_DIR", "directory", True),
    ("private_upload_dir", "ERP_FILE_STORAGE_DIR", "directory", True),
    ("upload_temp_dir", "ERP_UPLOAD_TEMP_DIR", "directory", True),
    ("invoice_export_dir", "ERP_INVOICE_EXPORT_DIR", "directory", True),
    (
        "invoice_attachment_dir",
        "ERP_INVOICE_ATTACHMENT_DIR",
        "directory",
        True,
    ),
    ("pdf_training_dir", "ERP_PDF_TRAINING_DIR", "directory", True),
    (
        "layout_runtime_file",
        "ERP_TWIN_LAYOUT_RUNTIME_PATH",
        "file",
        True,
    ),
    ("layout_draft_file", "ERP_TWIN_LAYOUT_DRAFT_PATH", "file", False),
    (
        "layout_backup_dir",
        "ERP_TWIN_LAYOUT_BACKUP_DIR",
        "directory",
        True,
    ),
    ("legacy_upload_dir", "ERP_LEGACY_UPLOAD_DIR", "directory", True),
    (
        "delivery_print_settings_file",
        "ERP_DELIVERY_PRINT_SETTINGS_PATH",
        "file",
        False,
    ),
    (
        "factory_twin_database",
        "ERP_FACTORY_TWIN_DATABASE_PATH",
        "file",
        False,
    ),
    ("secret_file", "ERP_SECRET_KEY_FILE", "file", True),
    ("attestation_file", ATTESTATION_ENV, "file", False),
    ("lease_file", "ERP_UAT_LEASE_PATH", "file", False),
    ("pid_file", "ERP_UAT_PID_PATH", "file", False),
    ("stdout_log", "ERP_UAT_STDOUT_PATH", "file", False),
    ("stderr_log", "ERP_UAT_STDERR_PATH", "file", False),
)


class UatIsolationError(RuntimeError):
    """A UAT path or process configuration failed closed."""


def _trusted_windows_directory() -> Path:
    """Resolve Windows itself without trusting inherited SystemRoot aliases."""

    if os.name != "nt":
        return Path(os.sep)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    getter = kernel32.GetWindowsDirectoryW
    getter.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    getter.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    written = getter(buffer, len(buffer))
    if not written or written >= len(buffer):
        raise UatIsolationError("Cannot resolve the trusted Windows directory")
    return Path(buffer.value)


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def _creation_token_from_handle(handle: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    getter = kernel32.GetProcessTimes
    getter.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    getter.restype = ctypes.c_int
    created, exited, kernel, user = (_FILETIME() for _ in range(4))
    if not getter(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise UatIsolationError(
            f"Cannot read Windows process creation identity (error={ctypes.get_last_error()})"
        )
    return f"{created.high:08x}{created.low:08x}"


def process_creation_token(pid: int) -> str | None:
    """Return a PID-reuse-resistant process identity, or None once it is gone."""

    if pid <= 0:
        return None
    if os.name != "nt":
        proc_stat = Path(f"/proc/{pid}/stat")
        try:
            fields = proc_stat.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise UatIsolationError(f"Cannot read process identity for PID {pid}") from error
        return fields[21] if len(fields) > 21 else None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    opener = kernel32.OpenProcess
    opener.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    opener.restype = ctypes.c_void_p
    closer = kernel32.CloseHandle
    closer.argtypes = [ctypes.c_void_p]
    closer.restype = ctypes.c_int
    handle = opener(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code in {87, 1168}:  # invalid parameter / not found
            return None
        raise UatIsolationError(
            f"Cannot open process identity for PID {pid} (error={error_code})"
        )
    try:
        return _creation_token_from_handle(int(handle))
    finally:
        closer(handle)


def _spawned_process_creation_token(process: subprocess.Popen[Any]) -> str:
    if os.name == "nt" and getattr(process, "_handle", None):
        return _creation_token_from_handle(int(process._handle))
    token = process_creation_token(int(process.pid))
    if not token:
        raise UatIsolationError("Spawned UAT process exited before ownership was recorded")
    return token


def _terminate_spawned_process(process: subprocess.Popen[Any]) -> None:
    """Terminate the exact Popen handle; never rediscover it by a reusable PID."""

    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    except Exception as error:
        raise UatIsolationError(
            f"Cannot terminate spawned UAT process PID {process.pid}"
        ) from error


def _terminate_owned_windows_process(pid: int, expected_token: str) -> bool:
    """Terminate the exact kernel process object after creation-time matching."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    opener = kernel32.OpenProcess
    opener.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    opener.restype = ctypes.c_void_p
    terminator = kernel32.TerminateProcess
    terminator.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    terminator.restype = ctypes.c_int
    waiter = kernel32.WaitForSingleObject
    waiter.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    waiter.restype = ctypes.c_ulong
    closer = kernel32.CloseHandle
    closer.argtypes = [ctypes.c_void_p]
    closer.restype = ctypes.c_int
    handle = opener(
        0x0001 | 0x1000 | 0x00100000,  # TERMINATE | QUERY_LIMITED | SYNCHRONIZE
        False,
        pid,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code in {87, 1168}:
            return False
        raise UatIsolationError(
            f"Cannot open owned UAT process PID {pid} (error={error_code})"
        )
    try:
        actual_token = _creation_token_from_handle(int(handle))
        if actual_token != expected_token:
            raise UatIsolationError(
                "UAT PID was reused by another process; refusing to terminate"
            )
        if not terminator(handle, 1):
            error_code = ctypes.get_last_error()
            if error_code != 5 or waiter(handle, 0) != 0:
                raise UatIsolationError(
                    f"Cannot terminate owned UAT process PID {pid} (error={error_code})"
                )
        if waiter(handle, 10_000) != 0:
            raise UatIsolationError(f"Owned UAT process PID {pid} did not stop")
        return True
    finally:
        closer(handle)


def _normcase(value: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def _absolute_lexical(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(os.path.abspath(candidate))
    return Path(os.path.normpath(candidate))


def _windows_handle_final_path(handle: int) -> str:
    """Return a stable final path for an open Windows file or directory handle."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    getter = kernel32.GetFinalPathNameByHandleW
    getter.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    getter.restype = ctypes.c_ulong
    # Prefer the kernel/NT namespace: it normalizes local drive aliases and, on
    # mapped shares that reject normalized DOS/GUID queries (WinError 1005),
    # still binds the handle to its redirector/device identity.  GUID and DOS
    # modes remain fallbacks for older filesystems.
    for flags in (0x2, 0x1, 0x0):
        size = getter(handle, None, 0, flags)
        if not size:
            continue
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = getter(handle, buffer, len(buffer), flags)
        if written and written < len(buffer):
            return buffer.value
    raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")


def _windows_final_path(path: Path) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    invalid_handle = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        0,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000,
        None,
    )
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed: {path}")
    try:
        return _windows_handle_final_path(int(handle))
    finally:
        close_handle(handle)


def identity_path(path: Path | str) -> str:
    """Canonicalize aliases using the deepest existing ancestor's file handle."""

    candidate = _absolute_lexical(path)
    missing: list[str] = []
    existing = candidate
    while not existing.exists():
        if existing.parent == existing:
            return _normcase(candidate)
        missing.append(existing.name)
        existing = existing.parent
    try:
        resolved = (
            _windows_final_path(existing)
            if os.name == "nt"
            else str(existing.resolve(strict=True))
        )
    except OSError as error:
        raise UatIsolationError(
            f"Cannot resolve final file identity for {existing}: {error}"
        ) from error
    for part in reversed(missing):
        resolved = os.path.join(resolved, part)
    return os.path.normcase(os.path.normpath(resolved))


def identity_is_within(candidate: Path | str, root: Path | str) -> bool:
    candidate_value = identity_path(candidate)
    root_value = identity_path(root)
    try:
        return os.path.commonpath((candidate_value, root_value)) == root_value
    except ValueError:
        return False


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        details = path.stat(follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return False
    attributes = int(getattr(details, "st_file_attributes", 0))
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(marker and attributes & marker)


def assert_no_reparse_components(path: Path | str, *, label: str) -> Path:
    """Reject symlink/junction traversal before resolving a writable path."""

    candidate = _absolute_lexical(path)
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current /= part
        if _is_reparse_point(current):
            raise UatIsolationError(f"{label} contains a symlink/junction: {current}")
    return candidate


def canonical_path(path: Path | str, *, label: str) -> Path:
    lexical = assert_no_reparse_components(path, label=label)
    try:
        return lexical.resolve(strict=False)
    except OSError as error:
        raise UatIsolationError(f"Cannot resolve {label}: {lexical}: {error}") from error


def is_within(candidate: Path | str, root: Path | str) -> bool:
    candidate_value = _normcase(_absolute_lexical(candidate))
    root_value = _normcase(_absolute_lexical(root))
    try:
        return os.path.commonpath((candidate_value, root_value)) == root_value
    except ValueError:
        return False


def same_file_or_path(first: Path | str, second: Path | str) -> bool:
    first_path = _absolute_lexical(first)
    second_path = _absolute_lexical(second)
    if _normcase(first_path) == _normcase(second_path):
        return True
    try:
        if identity_path(first_path) == identity_path(second_path):
            return True
    except UatIsolationError:
        raise
    try:
        return first_path.exists() and second_path.exists() and first_path.samefile(
            second_path
        )
    except OSError:
        return False


def _assert_private_file(path: Path, *, label: str, required: bool) -> None:
    assert_no_reparse_components(path, label=label)
    if not path.exists():
        if required:
            raise UatIsolationError(f"Required UAT {label} does not exist: {path}")
        if not path.parent.is_dir():
            raise UatIsolationError(f"Parent for UAT {label} does not exist: {path.parent}")
        return
    if not path.is_file():
        raise UatIsolationError(f"UAT {label} must be a regular file: {path}")
    links = int(getattr(path.stat(), "st_nlink", 1))
    if links != 1:
        raise UatIsolationError(
            f"UAT {label} must not be a hardlink (link count={links}): {path}"
        )


def _assert_directory(path: Path, *, label: str) -> None:
    assert_no_reparse_components(path, label=label)
    if not path.is_dir():
        raise UatIsolationError(f"Required UAT {label} directory does not exist: {path}")


def _json_path_list(raw: str | None, *, env_name: str) -> list[Path]:
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise UatIsolationError(f"{env_name} must be a JSON string array") from error
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise UatIsolationError(f"{env_name} must be a JSON string array")
    return [_absolute_lexical(item) for item in values]


def isolation_id(root: Path | str, database: Path | str, port: int) -> str:
    material = "|".join(
        (_normcase(_absolute_lexical(root)), _normcase(_absolute_lexical(database)), str(port))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _layout(
    root: Path,
    database: Path,
    port: int,
    launch_nonce: str | None = None,
) -> dict[str, Any]:
    identifier = isolation_id(root, database, port)
    runtime_root = root / ".erp-uat" / identifier
    launch_suffix = launch_nonce or "not-started"
    paths = {
        "database": database,
        "log_dir": runtime_root / "logs",
        "backup_dir": runtime_root / "backups",
        "private_upload_dir": runtime_root / "uploads" / "private",
        "upload_temp_dir": runtime_root / "uploads" / "temporary",
        "invoice_export_dir": runtime_root / "invoices" / "exports",
        "invoice_attachment_dir": runtime_root / "invoices" / "attachments",
        "pdf_training_dir": runtime_root / "pdf_training_samples",
        "layout_runtime_file": runtime_root / "warehouse" / "runtime" / "twin_layout_v1.json",
        "layout_draft_file": runtime_root / "warehouse" / "drafts" / "twin_layout_v1.draft.json",
        "layout_backup_dir": runtime_root / "warehouse" / "backups",
        "legacy_upload_dir": runtime_root / "legacy_uploads",
        "delivery_print_settings_file": runtime_root / "settings" / "delivery_print_settings.json",
        "factory_twin_database": runtime_root / "factory_twin" / "factory_twin.sqlite3",
        "secret_file": runtime_root / "session" / "session_secret.key",
        "attestation_file": (
            runtime_root / "attestations" / f"startup_{port}_{launch_suffix}.json"
        ),
        "lease_file": runtime_root / f"startup_{port}.lease.json",
        "pid_file": root / f"erp_uat_{port}.pid",
        "stdout_log": (
            runtime_root / "logs" / f"startup_{port}_{launch_suffix}.stdout.log"
        ),
        "stderr_log": (
            runtime_root / "logs" / f"startup_{port}_{launch_suffix}.stderr.log"
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "isolation_id": identifier,
        "cookie_name": f"erp_uat_{port}_{identifier}",
        "root": root,
        "runtime_root": runtime_root,
        "paths": paths,
    }


def _assert_root_boundaries(root: Path, forbidden_roots: Iterable[Path]) -> None:
    for forbidden in forbidden_roots:
        forbidden_path = _absolute_lexical(forbidden)
        if (
            identity_is_within(root, forbidden_path)
            or identity_is_within(forbidden_path, root)
        ):
            raise UatIsolationError(
                f"UAT root overlaps a forbidden production/code root: {root} <> {forbidden_path}"
            )


def _assert_protected_identity(
    targets: Iterable[Path], protected_paths: Iterable[Path]
) -> None:
    protected = list(protected_paths)
    for target in targets:
        for protected_path in protected:
            if same_file_or_path(target, protected_path):
                raise UatIsolationError(
                    f"UAT target is the same file/directory as a protected path: "
                    f"{target} <> {protected_path}"
                )


def _publish_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish a same-volume file while refusing an existing target."""

    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
        move_file.restype = ctypes.c_int
        if not move_file(str(source), str(destination), 0):
            error_code = ctypes.get_last_error()
            if error_code in {80, 183}:
                raise FileExistsError(destination)
            raise OSError(error_code, f"MoveFileExW failed: {source} -> {destination}")
        return
    os.link(source, destination)
    source.unlink()


def _bytes_from_windows_handle(handle: int) -> bytes:
    """Read the exact file object referenced by an already-open Windows handle."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_pointer = kernel32.SetFilePointerEx
    set_pointer.argtypes = [
        ctypes.c_void_p,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        ctypes.c_ulong,
    ]
    set_pointer.restype = ctypes.c_int
    read_file = kernel32.ReadFile
    read_file.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
    ]
    read_file.restype = ctypes.c_int
    if not set_pointer(handle, 0, None, 0):  # FILE_BEGIN
        raise UatIsolationError(
            f"Cannot rewind the verified SQLite handle (error={ctypes.get_last_error()})"
        )
    chunks: list[bytes] = []
    buffer = ctypes.create_string_buffer(1024 * 1024)
    while True:
        read = ctypes.c_ulong()
        if not read_file(handle, buffer, len(buffer), ctypes.byref(read), None):
            raise UatIsolationError(
                f"Cannot read the verified SQLite handle (error={ctypes.get_last_error()})"
            )
        if read.value == 0:
            break
        chunks.append(buffer.raw[: read.value])
    return b"".join(chunks)


def _windows_lock_verified_file(path: Path) -> tuple[int, bytes] | None:
    """Bind verified bytes to a handle that denies every subsequent writer."""

    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000 | 0x00010000,  # GENERIC_READ | DELETE
        0x1 | 0x4,  # FILE_SHARE_READ | FILE_SHARE_DELETE; never share write access
        None,
        3,
        0,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise UatIsolationError(
            f"Cannot exclusively bind the verified SQLite copy: {path} "
            f"(error={ctypes.get_last_error()})"
        )
    try:
        return int(handle), _bytes_from_windows_handle(int(handle))
    except Exception:
        closer = kernel32.CloseHandle
        closer.argtypes = [ctypes.c_void_p]
        closer.restype = ctypes.c_int
        closer(handle)
        raise


def _close_windows_handle(locked: tuple[int, bytes] | None) -> None:
    if locked is None:
        return
    handle, _ = locked
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    closer = kernel32.CloseHandle
    closer.argtypes = [ctypes.c_void_p]
    closer.restype = ctypes.c_int
    closer(handle)


def _windows_locked_file_final_path(locked: tuple[int, bytes] | None) -> str | None:
    if locked is None:
        return None
    return os.path.normcase(os.path.normpath(_windows_handle_final_path(locked[0])))


def _verified_sqlite_connection(connection: sqlite3.Connection, *, label: str) -> dict[str, Any]:
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    if integrity != "ok" or foreign_keys:
        raise UatIsolationError(
            f"{label} verification failed: integrity={integrity}, "
            f"foreign_key_violations={foreign_keys}"
        )
    return {
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
    }


def _verified_sqlite_bytes(content: bytes, *, label: str) -> dict[str, Any]:
    """Run SQLite semantic verification on bytes captured from a locked handle."""

    # Python builds before 3.11 do not expose Connection.deserialize().
    # Materialize the already-locked bytes into a private temporary file and
    # run the same read-only PRAGMA checks through the portable sqlite API.
    deserialize = getattr(sqlite3.Connection, "deserialize", None)
    if not callable(deserialize):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".uat-verify-", suffix=".sqlite3"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            with closing(sqlite3.connect(f"{temporary.as_uri()}?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                return _verified_sqlite_connection(connection, label=label)
        except sqlite3.Error as error:
            raise UatIsolationError(f"{label} verification failed") from error
        finally:
            temporary.unlink(missing_ok=True)

    with closing(sqlite3.connect(":memory:")) as connection:
        try:
            connection.deserialize(content)
        except sqlite3.Error as error:
            raise UatIsolationError(f"{label} cannot be deserialized") from error
        connection.execute("PRAGMA query_only=ON")
        try:
            return _verified_sqlite_connection(connection, label=label)
        except sqlite3.Error as error:
            raise UatIsolationError(f"{label} verification failed") from error


def create_sqlite_copy(
    source: Path | str,
    destination: Path | str,
    *,
    transform: Callable[[sqlite3.Connection], Any] | None = None,
) -> dict[str, Any]:
    """Build privately, verify, then atomically publish without replacement."""

    source_path = canonical_path(source, label="SQLite copy source")
    destination_path = _absolute_lexical(destination)
    assert_no_reparse_components(destination_path.parent, label="SQLite copy destination")
    if not source_path.is_file():
        raise UatIsolationError(f"SQLite copy source does not exist: {source_path}")
    if same_file_or_path(source_path, destination_path):
        raise UatIsolationError("SQLite copy destination must differ from its source")
    if destination_path.exists():
        raise UatIsolationError(
            f"Refusing to overwrite an existing SQLite file: {destination_path}"
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_reparse_components(destination_path.parent, label="SQLite copy destination")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".private-copy",
        dir=destination_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        descriptor_identity = os.fstat(descriptor)
        current_identity = temporary.stat()
        if (descriptor_identity.st_dev, descriptor_identity.st_ino) != (
            current_identity.st_dev,
            current_identity.st_ino,
        ):
            raise UatIsolationError("Private SQLite copy identity changed before open")
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            source_connection.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(temporary)) as target_connection:
                source_connection.backup(target_connection)
                transform_result = transform(target_connection) if transform else None
                target_connection.commit()
                verification = _verified_sqlite_connection(
                    target_connection,
                    label="SQLite copy",
                )
        os.close(descriptor)
        descriptor = -1
        _assert_private_file(temporary, label="private database copy", required=True)
        with closing(sqlite3.connect(f"{temporary.as_uri()}?mode=ro", uri=True)) as final:
            final.execute("PRAGMA query_only=ON")
            verification = _verified_sqlite_connection(
                final,
                label="SQLite copy final",
            )
            # ``Connection.serialize`` is only available in newer Python
            # sqlite builds.  The factory runtime may not expose it; the
            # immutable temporary file is still the exact artifact that will
            # be locked and published, so read those bytes as the portable
            # equivalent and re-check them under the publish lock below.
            serialize = getattr(final, "serialize", None)
            verified_content = (
                serialize() if callable(serialize) else temporary.read_bytes()
            )
        verification_lock = _windows_lock_verified_file(temporary)
        try:
            # The exclusive read/delete handle blocks all new writers and name
            # replacement from this point through MoveFileEx.  Verify identity
            # again after the lock so these are the bytes checked just above.
            locked_identity = temporary.stat()
            if (locked_identity.st_dev, locked_identity.st_ino) != (
                current_identity.st_dev,
                current_identity.st_ino,
            ):
                raise UatIsolationError("Private SQLite copy identity changed before publish")
            if verification_lock:
                locked_content = verification_lock[1]
                if locked_content != verified_content:
                    raise UatIsolationError(
                        "Private SQLite copy changed after final validation"
                    )
                verification = _verified_sqlite_bytes(
                    locked_content,
                    label="locked SQLite copy",
                )
                locked_digest = hashlib.sha256(locked_content).hexdigest()
            else:
                # Non-Windows fallback has no Win32 share lock; immediately
                # re-verify the exact bytes that are about to be linked.
                locked_content = temporary.read_bytes()
                if locked_content != verified_content:
                    raise UatIsolationError(
                        "Private SQLite copy changed after final validation"
                    )
                verification = _verified_sqlite_bytes(
                    locked_content,
                    label="SQLite copy before publish",
                )
                locked_digest = hashlib.sha256(locked_content).hexdigest()
            expected_destination_identity = identity_path(destination_path)
            try:
                _publish_no_replace(temporary, destination_path)
            except FileExistsError as error:
                raise UatIsolationError(
                    f"Refusing to overwrite an existing SQLite file: {destination_path}"
                ) from error
            locked_final_path = _windows_locked_file_final_path(verification_lock)
            if locked_final_path is not None and locked_final_path != expected_destination_identity:
                raise UatIsolationError(
                    "Published SQLite object is not the verified locked file"
                )
        finally:
            _close_windows_handle(verification_lock)
        _assert_private_file(destination_path, label="database copy", required=True)
        if _sha256_file(destination_path) != locked_digest:
            raise UatIsolationError("Published SQLite copy digest changed during rename")
        return {
            "path": str(destination_path),
            **verification,
            "transform_result": transform_result,
        }
    except Exception:
        # Never remove the destination on an error.  A racing actor may have
        # created or rebound that name after our initial check.  The private
        # random temp is the only path this call owns unambiguously.
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_exclusive_copy(source: Path, destination: Path) -> None:
    content = source.read_bytes()
    try:
        with destination.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _isolate_pdf_training_samples_in_connection(
    connection: sqlite3.Connection,
    target_dir: Path,
) -> dict[str, int]:
    """Copy immutable PDF sources and rewrite paths in an already private DB."""

    _assert_directory(target_dir, label="PDF training root")
    copied = 0
    rebound = 0
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='pdf_order_training_samples'"
    ).fetchone()
    if table is None:
        return {"copied": 0, "rebound": 0}
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(pdf_order_training_samples)"
        ).fetchall()
    }
    if not {"id", "file_path", "file_sha256"}.issubset(columns):
        raise UatIsolationError(
            "PDF training sample table is missing file isolation columns"
        )
    rows = connection.execute(
        "SELECT id, file_path, file_sha256 FROM pdf_order_training_samples "
        "WHERE file_path IS NOT NULL AND trim(file_path) <> ''"
    ).fetchall()
    updates: list[tuple[str, int]] = []
    for sample_id, raw_path, raw_digest in rows:
        expected = str(raw_digest or "").strip().lower()
        if len(expected) != 64 or any(
            character not in "0123456789abcdef" for character in expected
        ):
            raise UatIsolationError(
                f"PDF training sample {sample_id} has an invalid SHA-256"
            )
        source = canonical_path(raw_path, label=f"PDF sample {sample_id}")
        if not source.is_file():
            raise UatIsolationError(
                f"PDF training sample source is missing: id={sample_id}, path={source}"
            )
        if _sha256_file(source) != expected:
            raise UatIsolationError(
                f"PDF training sample source SHA-256 mismatch: id={sample_id}"
            )
        destination = target_dir / f"{expected}.pdf"
        if not destination.exists():
            _write_exclusive_copy(source, destination)
            copied += 1
        _assert_private_file(
            destination,
            label=f"PDF training sample copy {sample_id}",
            required=True,
        )
        if _sha256_file(destination) != expected:
            raise UatIsolationError(
                f"Isolated PDF training sample SHA-256 mismatch: id={sample_id}"
            )
        if _normcase(source) != _normcase(destination):
            updates.append((str(destination), int(sample_id)))
    if updates:
        connection.executemany(
            "UPDATE pdf_order_training_samples SET file_path=? WHERE id=?",
            updates,
        )
        rebound = len(updates)
    return {"copied": copied, "rebound": rebound}


def _replace_existing_uat_database(
    database: Path,
    target_dir: Path,
) -> dict[str, int]:
    """Rebind an existing UAT DB via a private copy; never reopen it writable."""

    replacement = database.with_name(f".{database.name}.{uuid.uuid4().hex}.rebound")
    original_identity = database.stat().st_dev, database.stat().st_ino
    result = create_sqlite_copy(
        database,
        replacement,
        transform=lambda connection: _isolate_pdf_training_samples_in_connection(
            connection,
            target_dir,
        ),
    )
    try:
        current_identity = database.stat().st_dev, database.stat().st_ino
        if current_identity != original_identity:
            raise UatIsolationError(
                "Existing UAT database identity changed while preparing its private copy"
            )
        os.replace(replacement, database)
    except Exception:
        replacement.unlink(missing_ok=True)
        raise
    _assert_private_file(database, label="rebound ERP database", required=True)
    return dict(result.get("transform_result") or {"copied": 0, "rebound": 0})


def _pdf_samples_need_rebinding(database: Path, target_dir: Path) -> bool:
    """Read only: decide whether an existing UAT DB needs a private rewrite."""

    uri = f"{database.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='pdf_order_training_samples'"
        ).fetchone()
        if table is None:
            return False
        rows = connection.execute(
            "SELECT file_path FROM pdf_order_training_samples "
            "WHERE file_path IS NOT NULL AND trim(file_path) <> ''"
        ).fetchall()
    for (raw_path,) in rows:
        try:
            candidate = canonical_path(raw_path, label="existing UAT PDF sample")
        except UatIsolationError:
            return True
        if not is_within(candidate, target_dir):
            return True
    return False


def _ensure_bound_secret(path: Path, identifier: str) -> None:
    prefix = f"uat-{identifier}-"
    if path.exists():
        _assert_private_file(path, label="session secret", required=True)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise UatIsolationError(f"Cannot read UAT session secret: {path}") from error
        if not value.startswith(prefix) or len(value) < len(prefix) + 48:
            raise UatIsolationError(
                "UAT session secret is not bound to this database/root/port"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_reparse_components(path.parent, label="session secret directory")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix + secrets.token_urlsafe(48) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _ensure_bound_secret(path, identifier)


def _environment_paths(layout: dict[str, Any]) -> dict[str, str]:
    path_values: dict[str, Path] = layout["paths"]
    by_logical_name = {logical: env_name for logical, env_name, _, _ in PATH_SPECS}
    return {
        by_logical_name[logical]: str(path)
        for logical, path in path_values.items()
        if logical in by_logical_name
    }


def _public_layout(layout: dict[str, Any], *, database_created: bool) -> dict[str, Any]:
    paths: dict[str, Path] = layout["paths"]
    twin = paths["factory_twin_database"]
    return {
        "schema_version": SCHEMA_VERSION,
        "isolation_id": layout["isolation_id"],
        "cookie_name": layout["cookie_name"],
        "root": str(layout["root"]),
        "runtime_root": str(layout["runtime_root"]),
        "database_created": database_created,
        "factory_twin_status": "isolated_copy" if twin.is_file() else "missing_fail_closed",
        "paths": {name: str(path) for name, path in paths.items()},
        "environment": _environment_paths(layout),
    }


def prepare_uat_layout(
    *,
    root: Path,
    database: Path,
    port: int,
    layout_source: Path,
    source_database: Path | None = None,
    twin_database_source: Path | None = None,
    protected_paths: Iterable[Path] = (),
    forbidden_roots: Iterable[Path] = (),
    launch_nonce: str | None = None,
) -> dict[str, Any]:
    """Prepare only run-local files; never overwrite a database or map copy."""

    root_path = _absolute_lexical(root)
    database_path = _absolute_lexical(database)
    protected = [_absolute_lexical(item) for item in protected_paths]
    forbidden = [_absolute_lexical(item) for item in forbidden_roots]
    assert_no_reparse_components(root_path, label="UAT root")
    if _normcase(database_path.parent) != _normcase(root_path):
        raise UatIsolationError("The UAT database must be directly inside its run root")
    _assert_root_boundaries(root_path, forbidden)
    if launch_nonce is not None and (
        len(launch_nonce) < 16
        or any(character not in "0123456789abcdefABCDEF" for character in launch_nonce)
    ):
        raise UatIsolationError("UAT launch nonce must be at least 16 hexadecimal characters")
    layout = _layout(root_path, database_path, port, launch_nonce)
    target_paths = list(layout["paths"].values())
    _assert_protected_identity(target_paths, protected)
    # Check the complete runtime prefix before the first mkdir.  Otherwise an
    # attacker-controlled .erp-uat junction could receive directories before
    # a leaf-level check notices it.
    assert_no_reparse_components(layout["runtime_root"], label="UAT runtime root")

    if source_database is not None and database_path.exists():
        raise UatIsolationError(
            f"Refusing to overwrite an existing UAT database: {database_path}"
        )
    if source_database is None and not database_path.is_file():
        raise UatIsolationError(
            "UAT database does not exist; provide an explicit source to create a copy"
        )
    existing_database_identity = (
        (database_path.stat().st_dev, database_path.stat().st_ino)
        if source_database is None
        else None
    )
    twin_target: Path = layout["paths"]["factory_twin_database"]
    if twin_database_source is not None and twin_target.exists():
        raise UatIsolationError(
            f"Refusing to overwrite an existing factory-twin UAT database: {twin_target}"
        )

    root_path.mkdir(parents=True, exist_ok=True)
    assert_no_reparse_components(root_path, label="UAT root")
    directory_names = {
        logical
        for logical, _, kind, required in PATH_SPECS
        if kind == "directory" and required
    }
    for logical in directory_names:
        path = layout["paths"][logical]
        path.mkdir(parents=True, exist_ok=True)
        _assert_directory(path, label=logical)
    temporary_dir = layout["runtime_root"] / "temporary"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    _assert_directory(temporary_dir, label="temporary")
    database_created = False
    if source_database is not None:
        copy_result = create_sqlite_copy(
            source_database,
            database_path,
            transform=lambda connection: _isolate_pdf_training_samples_in_connection(
                connection,
                layout["paths"]["pdf_training_dir"],
            ),
        )
        database_created = True
        pdf_isolation = dict(
            copy_result.get("transform_result") or {"copied": 0, "rebound": 0}
        )
    else:
        pdf_isolation = {"copied": 0, "rebound": 0}
        current_identity = database_path.stat().st_dev, database_path.stat().st_ino
        if current_identity != existing_database_identity:
            raise UatIsolationError(
                "Existing UAT database identity changed during layout preparation"
            )
    _assert_private_file(database_path, label="ERP database", required=True)
    _assert_protected_identity((database_path,), protected)

    for logical in (
        "layout_runtime_file",
        "layout_draft_file",
        "factory_twin_database",
        "attestation_file",
        "lease_file",
        "pid_file",
        "stdout_log",
        "stderr_log",
        "delivery_print_settings_file",
    ):
        layout["paths"][logical].parent.mkdir(parents=True, exist_ok=True)
        assert_no_reparse_components(
            layout["paths"][logical].parent,
            label=f"{logical} parent",
        )

    source_layout = canonical_path(layout_source, label="tracked layout source")
    if not source_layout.is_file():
        raise UatIsolationError(f"Tracked layout source does not exist: {source_layout}")
    runtime_layout: Path = layout["paths"]["layout_runtime_file"]
    _write_exclusive_copy(source_layout, runtime_layout)
    _assert_private_file(runtime_layout, label="layout runtime copy", required=True)

    if twin_database_source is not None:
        create_sqlite_copy(twin_database_source, twin_target)
    if twin_target.exists():
        _assert_private_file(twin_target, label="factory twin database", required=True)

    _ensure_bound_secret(layout["paths"]["secret_file"], layout["isolation_id"])
    if not database_created and _pdf_samples_need_rebinding(
        database_path,
        layout["paths"]["pdf_training_dir"],
    ):
        pdf_isolation = _replace_existing_uat_database(
            database_path,
            layout["paths"]["pdf_training_dir"],
        )
    _assert_protected_identity(target_paths, protected)
    result = _public_layout(layout, database_created=database_created)
    result["temp_dir"] = str(temporary_dir)
    result["pdf_training_samples"] = pdf_isolation
    return result


def _required_environment_path(env_name: str) -> Path:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        raise UatIsolationError(f"UAT requires an explicit {env_name}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UatIsolationError(f"UAT requires an absolute {env_name}: {raw}")
    return canonical_path(path, label=env_name)


def validate_uat_environment(settings: Any | None = None) -> dict[str, Any]:
    """Validate the environment that the running app actually inherited."""

    root = _required_environment_path(UAT_ROOT_ENV)
    _assert_directory(root, label="root")
    protected = _json_path_list(os.getenv(PROTECTED_PATHS_ENV), env_name=PROTECTED_PATHS_ENV)
    forbidden = _json_path_list(os.getenv(FORBIDDEN_ROOTS_ENV), env_name=FORBIDDEN_ROOTS_ENV)
    _assert_root_boundaries(root, forbidden)

    paths: dict[str, Path] = {}
    for logical, env_name, kind, required in PATH_SPECS:
        path = _required_environment_path(env_name)
        if not identity_is_within(path, root):
            raise UatIsolationError(f"{env_name} escapes UAT root: {path} <> {root}")
        if kind == "directory":
            _assert_directory(path, label=logical)
        else:
            _assert_private_file(path, label=logical, required=required)
        paths[logical] = path

    database = paths["database"]
    if _normcase(database.parent) != _normcase(root):
        raise UatIsolationError("The running UAT database is not directly inside UAT root")
    _assert_protected_identity(paths.values(), protected)

    try:
        port = int(os.getenv("ERP_PORT", ""))
    except ValueError as error:
        raise UatIsolationError("ERP_PORT must be an integer in UAT") from error
    expected_id = isolation_id(root, database, port)
    if os.getenv(ISOLATION_ID_ENV, "") != expected_id:
        raise UatIsolationError("ERP_UAT_ISOLATION_ID does not match root/database/port")
    expected_cookie = f"erp_uat_{port}_{expected_id}"
    if os.getenv("ERP_SESSION_COOKIE_NAME", "") != expected_cookie:
        raise UatIsolationError("UAT session cookie is not unique to this run")
    if os.getenv("ERP_SESSION_COOKIE_SECURE", "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        raise UatIsolationError("Loopback HTTP UAT must disable secure-only cookies")
    if os.getenv("ERP_SECRET_KEY", "").strip():
        raise UatIsolationError("UAT must not inherit ERP_SECRET_KEY; use its bound file")
    _ensure_bound_secret(paths["secret_file"], expected_id)

    if os.getenv("ERP_ENVIRONMENT", "").strip().lower() != "test":
        raise UatIsolationError("UAT requires ERP_ENVIRONMENT=test")
    if os.getenv("ERP_BIND_HOST", "").strip() != "127.0.0.1":
        raise UatIsolationError("UAT requires ERP_BIND_HOST=127.0.0.1")
    if os.getenv("ERP_WORKERS", "").strip() != "1":
        raise UatIsolationError("UAT requires exactly one worker")

    if settings is not None:
        if not same_file_or_path(settings.database_path, database):
            raise UatIsolationError("Loaded application database differs from UAT manifest")
        if not same_file_or_path(settings.backup_dir, paths["backup_dir"]):
            raise UatIsolationError("Loaded application backup directory differs from UAT manifest")
        if settings.session_cookie_name != expected_cookie:
            raise UatIsolationError("Loaded application cookie differs from UAT manifest")
        if settings.environment != "test" or settings.bind_host != "127.0.0.1":
            raise UatIsolationError("Loaded application network settings are not isolated")

    return {
        "schema_version": SCHEMA_VERSION,
        "isolation_id": expected_id,
        "root": str(root),
        "database": str(database),
        "port": port,
        "bind_host": "127.0.0.1",
        "workers": 1,
        "environment": "test",
        "cookie_name": expected_cookie,
        "factory_twin_status": (
            "isolated_copy"
            if paths["factory_twin_database"].is_file()
            else "missing_fail_closed"
        ),
        "paths": {name: str(path) for name, path in paths.items()},
    }


def validate_uat_process_ownership() -> dict[str, Any]:
    """Bind app startup to the nonce/PID/creation token recorded by spawn."""

    nonce = os.getenv(LAUNCH_NONCE_ENV, "")
    if len(nonce) < 16 or any(
        character not in "0123456789abcdefABCDEF" for character in nonce
    ):
        raise UatIsolationError("UAT launch nonce is missing or invalid")
    pid_path = _required_environment_path("ERP_UAT_PID_PATH")
    ownership_path = pid_path.with_name(f".{pid_path.name}.{nonce}.child.json")
    ownership = _read_json_object(ownership_path, label="UAT child ownership")
    pid = os.getpid()
    creation_token = process_creation_token(pid)
    if (
        ownership.get("nonce") != nonce
        or int(ownership.get("pid") or 0) != pid
        or not creation_token
        or ownership.get("process_creation_token") != creation_token
    ):
        raise UatIsolationError(
            "Running UAT process does not own the PID/nonce record: "
            f"current_pid={pid}, recorded_pid={ownership.get('pid')}, "
            f"current_token={creation_token}, "
            f"recorded_token={ownership.get('process_creation_token')}, "
            f"nonce_match={ownership.get('nonce') == nonce}"
        )
    return ownership


def collect_actual_uat_consumers(settings: Any) -> dict[str, str]:
    """Collect path values from the loaded application modules, not from env."""

    from app.api import invoice_tasks, pdf_training
    from app.core.database import engine
    from app.services import (
        delivery_print_settings,
        secure_uploads,
        warehouse_twin_layout,
        warehouse_twin_layout_editor,
        warehouse_twin_production,
    )

    engine_database = Path(str(engine.url.database or "")).resolve(strict=False)
    return {
        "database": str(Path(settings.database_path).resolve(strict=False)),
        "sqlalchemy_engine_database": str(engine_database),
        "backup_dir": str(Path(settings.backup_dir).resolve(strict=False)),
        "private_upload_dir": str(secure_uploads.private_upload_root()),
        "upload_temp_dir": str(secure_uploads.temporary_upload_root()),
        "invoice_export_dir": str(invoice_tasks._invoice_export_dir().resolve(strict=False)),
        "invoice_attachment_dir": str(invoice_tasks._attachment_root().resolve(strict=False)),
        "pdf_training_dir": str(pdf_training._SAMPLE_DIR.resolve(strict=False)),
        "layout_runtime_file": str(
            warehouse_twin_layout.TWIN_LAYOUT_RUNTIME_PATH.resolve(strict=False)
        ),
        "layout_editor_runtime_file": str(
            warehouse_twin_layout_editor.TWIN_LAYOUT_PATH.resolve(strict=False)
        ),
        "layout_draft_file": str(
            warehouse_twin_layout_editor.TWIN_LAYOUT_DRAFT_PATH.resolve(strict=False)
        ),
        "layout_backup_dir": str(
            warehouse_twin_layout_editor.TWIN_LAYOUT_BACKUP_DIR.resolve(strict=False)
        ),
        "legacy_upload_dir": str(
            secure_uploads.legacy_upload_root()
        ),
        "delivery_print_settings_file": str(
            delivery_print_settings.delivery_print_settings_path().resolve(strict=False)
        ),
        "factory_twin_database": str(
            warehouse_twin_production.factory_twin_database_path()
        ),
        "secret_file": str(Path(os.environ["ERP_SECRET_KEY_FILE"]).resolve(strict=False)),
        "stdout_log": str(Path(os.environ["ERP_UAT_STDOUT_PATH"]).resolve(strict=False)),
        "stderr_log": str(Path(os.environ["ERP_UAT_STDERR_PATH"]).resolve(strict=False)),
    }


def validate_actual_uat_consumers(settings: Any, manifest: dict[str, Any]) -> dict[str, str]:
    consumers = collect_actual_uat_consumers(settings)
    expected_paths: dict[str, str] = manifest["paths"]
    comparisons = {
        "database": "database",
        "sqlalchemy_engine_database": "database",
        "backup_dir": "backup_dir",
        "private_upload_dir": "private_upload_dir",
        "upload_temp_dir": "upload_temp_dir",
        "invoice_export_dir": "invoice_export_dir",
        "invoice_attachment_dir": "invoice_attachment_dir",
        "pdf_training_dir": "pdf_training_dir",
        "layout_runtime_file": "layout_runtime_file",
        "layout_editor_runtime_file": "layout_runtime_file",
        "layout_draft_file": "layout_draft_file",
        "layout_backup_dir": "layout_backup_dir",
        "legacy_upload_dir": "legacy_upload_dir",
        "delivery_print_settings_file": "delivery_print_settings_file",
        "factory_twin_database": "factory_twin_database",
        "secret_file": "secret_file",
        "stdout_log": "stdout_log",
        "stderr_log": "stderr_log",
    }
    for consumer_name, logical_name in comparisons.items():
        actual = Path(consumers[consumer_name])
        expected = Path(expected_paths[logical_name])
        if not same_file_or_path(actual, expected):
            raise UatIsolationError(
                f"Actual UAT consumer {consumer_name} differs from manifest: "
                f"{actual} <> {expected}"
            )
    return consumers


def validate_uat_database_references(database: Path | str) -> None:
    """Reject copied rows that retain absolute PDF paths outside UAT storage."""

    if not os.getenv(UAT_ROOT_ENV):
        return
    database_path = canonical_path(database, label="UAT database")
    pdf_root = _required_environment_path("ERP_PDF_TRAINING_DIR")
    uri = f"{database_path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='pdf_order_training_samples'"
            ).fetchone()
            if table is None:
                return
            rows = connection.execute(
                "SELECT id, file_path, file_sha256 FROM pdf_order_training_samples "
                "WHERE file_path IS NOT NULL AND trim(file_path) <> ''"
            ).fetchall()
    except sqlite3.Error as error:
        raise UatIsolationError(
            f"Cannot verify UAT PDF training references: {database_path}"
        ) from error
    escaped: list[str] = []
    for sample_id, raw_path, raw_digest in rows:
        try:
            candidate = canonical_path(raw_path, label=f"PDF sample {sample_id}")
        except UatIsolationError:
            escaped.append(str(sample_id))
            continue
        expected = str(raw_digest or "").strip().lower()
        if (
            not is_within(candidate, pdf_root)
            or not candidate.is_file()
            or len(expected) != 64
            or _sha256_file(candidate) != expected
        ):
            escaped.append(str(sample_id))
    if escaped:
        shown = ", ".join(escaped[:10])
        suffix = "..." if len(escaped) > 10 else ""
        raise UatIsolationError(
            "UAT database contains PDF sample paths outside its isolated copy "
            f"(sample ids: {shown}{suffix})"
        )


def assert_uat_managed_path(path: Path | str, env_name: str, *, label: str) -> Path:
    """Reject copied-database records that still point at formal files."""

    if not os.getenv(UAT_ROOT_ENV):
        return _absolute_lexical(path).resolve(strict=False)
    candidate = canonical_path(path, label=label)
    root = _required_environment_path(env_name)
    if not identity_is_within(candidate, root):
        raise UatIsolationError(f"{label} escapes isolated {env_name}: {candidate}")
    assert_no_reparse_components(candidate, label=label)
    return candidate


def write_uat_attestation(settings: Any) -> dict[str, Any] | None:
    if not os.getenv(UAT_ROOT_ENV):
        return None
    manifest = validate_uat_environment(settings)
    validate_uat_database_references(settings.database_path)
    consumers = validate_actual_uat_consumers(settings, manifest)
    target = Path(manifest["paths"]["attestation_file"])
    payload = {
        **manifest,
        "pid": os.getpid(),
        "nonce": os.environ.get("ERP_UAT_LAUNCH_NONCE", ""),
        "actual_consumers": consumers,
        "process_creation_token": process_creation_token(os.getpid()),
        "attested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def verify_uat_attestation(
    path: Path,
    expected_pid: int,
    expected_nonce: str,
) -> dict[str, Any]:
    manifest = validate_uat_environment()
    target = canonical_path(path, label="UAT attestation")
    if not same_file_or_path(target, manifest["paths"]["attestation_file"]):
        raise UatIsolationError("UAT attestation path differs from the environment")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UatIsolationError(f"Cannot read UAT attestation: {target}") from error
    expected = {
        "schema_version": SCHEMA_VERSION,
        "nonce": expected_nonce,
        "isolation_id": manifest["isolation_id"],
        "root": manifest["root"],
        "database": manifest["database"],
        "paths": manifest["paths"],
        "cookie_name": manifest["cookie_name"],
        "actual_consumers": payload.get("actual_consumers"),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise UatIsolationError(f"UAT attestation mismatch for {key}")
    if not isinstance(payload.get("actual_consumers"), dict):
        raise UatIsolationError("UAT attestation lacks actual consumer paths")
    process_token = str(payload.get("process_creation_token") or "")
    serving_pid = int(payload.get("pid") or 0)
    if serving_pid <= 0 or not process_token or process_creation_token(serving_pid) != process_token:
        raise UatIsolationError("UAT attestation process identity mismatch")
    expected_consumers = {
        "database": manifest["paths"]["database"],
        "sqlalchemy_engine_database": manifest["paths"]["database"],
        "backup_dir": manifest["paths"]["backup_dir"],
        "private_upload_dir": manifest["paths"]["private_upload_dir"],
        "upload_temp_dir": manifest["paths"]["upload_temp_dir"],
        "invoice_export_dir": manifest["paths"]["invoice_export_dir"],
        "invoice_attachment_dir": manifest["paths"]["invoice_attachment_dir"],
        "pdf_training_dir": manifest["paths"]["pdf_training_dir"],
        "layout_runtime_file": manifest["paths"]["layout_runtime_file"],
        "layout_editor_runtime_file": manifest["paths"]["layout_runtime_file"],
        "layout_draft_file": manifest["paths"]["layout_draft_file"],
        "layout_backup_dir": manifest["paths"]["layout_backup_dir"],
        "legacy_upload_dir": manifest["paths"]["legacy_upload_dir"],
        "delivery_print_settings_file": manifest["paths"]["delivery_print_settings_file"],
        "factory_twin_database": manifest["paths"]["factory_twin_database"],
        "secret_file": manifest["paths"]["secret_file"],
        "stdout_log": manifest["paths"]["stdout_log"],
        "stderr_log": manifest["paths"]["stderr_log"],
    }
    for name, expected_path in expected_consumers.items():
        actual_path = payload["actual_consumers"].get(name)
        if not actual_path or not same_file_or_path(actual_path, expected_path):
            raise UatIsolationError(f"UAT attestation consumer mismatch for {name}")
    return payload


def case_insensitive_environment(
    entries: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Collapse Windows environment aliases without a case-collision dictionary."""

    result: dict[str, str] = {}
    actual_keys: dict[str, str] = {}
    for raw_name, raw_value in entries:
        name = str(raw_name)
        value = str(raw_value)
        folded = name.casefold()
        previous = actual_keys.get(folded)
        if previous is not None:
            result.pop(previous, None)
        # Keep Windows' conventional spelling for the executable search path.
        selected = "Path" if folded == "path" else name
        result[selected] = value
        actual_keys[folded] = selected
    return result


def _decode_base64_json_object(payload: str, *, label: str) -> dict[str, str]:
    try:
        raw = base64.b64decode(payload, validate=True).decode("utf-8")
        pairs = json.loads(raw, object_pairs_hook=lambda values: values)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UatIsolationError(f"{label} must be base64 UTF-8 JSON") from error
    if not isinstance(pairs, list):
        raise UatIsolationError(f"{label} must contain a JSON object")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise UatIsolationError(f"{label} must contain a flat JSON object")
        raw_name, raw_value = pair
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise UatIsolationError(f"{label} keys and values must be strings")
        folded = raw_name.casefold()
        if folded in seen:
            raise UatIsolationError(f"{label} contains a duplicate key: {raw_name}")
        seen.add(folded)
        result[raw_name] = raw_value
    return result


def _decode_base64_json_array(payload: str, *, label: str) -> list[str]:
    try:
        value = json.loads(base64.b64decode(payload, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UatIsolationError(f"{label} must be base64 UTF-8 JSON") from error
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise UatIsolationError(f"{label} must contain a non-empty string array")
    return value


def _validated_environment_overrides(overrides: dict[str, str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    seen: set[str] = set()
    allowed = {name.casefold() for name in _UAT_OVERRIDE_NAMES}
    for raw_name, raw_value in (overrides or {}).items():
        name = str(raw_name)
        value = str(raw_value)
        folded = name.casefold()
        if (
            not name
            or "\x00" in name
            or "=" in name
            or "\x00" in value
            or folded in seen
        ):
            raise UatIsolationError(f"Invalid or duplicate UAT environment key: {name!r}")
        if folded not in allowed:
            raise UatIsolationError(f"UAT environment key is not allowlisted: {name}")
        if folded == "erp_secret_key" and value.strip():
            raise UatIsolationError("UAT must not receive an inline ERP_SECRET_KEY")
        seen.add(folded)
        result[name] = value
    return result


def isolated_child_environment(
    overrides: dict[str, str] | None = None,
    *,
    python: Path | str | None = None,
    git: Path | str | None = None,
    temp_dir: Path | str | None = None,
) -> dict[str, str]:
    """Build a child-only environment; never mutate or trust the parent Path."""

    normalized = case_insensitive_environment(os.environ.items())
    for name in list(normalized):
        folded = name.casefold()
        if (
            folded == "path"
            or folded in {"temp", "tmp", "tmpdir", "systemroot", "windir"}
            or folded.startswith("erp_")
            or folded.startswith("python")
            or folded.startswith("git_")
            or folded.startswith("uvicorn_")
        ):
            normalized.pop(name, None)

    trusted_path_parts: list[str] = []
    if python is not None:
        python_path = canonical_path(python, label="trusted Python")
        if not python_path.is_file():
            raise UatIsolationError(f"Trusted Python does not exist: {python_path}")
        trusted_path_parts.append(str(python_path.parent))
    if git is not None:
        git_path = canonical_path(git, label="trusted Git")
        if not git_path.is_file():
            raise UatIsolationError(f"Trusted Git does not exist: {git_path}")
        trusted_path_parts.append(str(git_path.parent))
    system_root = _trusted_windows_directory()
    if os.name == "nt":
        normalized["SystemRoot"] = str(system_root)
        normalized["WINDIR"] = str(system_root)
    trusted_path_parts.extend(
        str(path)
        for path in (system_root / "System32", system_root)
        if str(path) not in trusted_path_parts
    )
    normalized["Path"] = os.pathsep.join(trusted_path_parts)

    for name, value in _validated_environment_overrides(overrides).items():
        normalized[name] = value
    if temp_dir is not None:
        temporary = canonical_path(temp_dir, label="UAT temporary directory")
        _assert_directory(temporary, label="UAT temporary directory")
        normalized["TEMP"] = str(temporary)
        normalized["TMP"] = str(temporary)
        normalized["TMPDIR"] = str(temporary)
    normalized["PYTHONUTF8"] = "1"
    normalized["PYTHONIOENCODING"] = "utf-8"
    normalized["PYTHONDONTWRITEBYTECODE"] = "1"
    return normalized


def run_isolated_command(
    *,
    argv: list[str],
    cwd: Path,
    overrides: dict[str, str] | None = None,
    python: Path | str | None = None,
    git: Path | str | None = None,
    temp_dir: Path | str | None = None,
) -> dict[str, Any]:
    if not argv:
        raise UatIsolationError("Isolated command requires an executable")
    executable = canonical_path(argv[0], label="isolated command executable")
    result = subprocess.run(
        [str(executable), *[str(item) for item in argv[1:]]],
        cwd=canonical_path(cwd, label="isolated command working directory"),
        env=isolated_child_environment(
            overrides,
            python=python,
            git=git,
            temp_dir=temp_dir,
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _json_write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UatIsolationError(f"Cannot read {label}: {path}") from error
    if not isinstance(value, dict):
        raise UatIsolationError(f"{label} must be a JSON object: {path}")
    return value


def _owned_record(path: Path, *, nonce: str, pid: int | None = None) -> bool:
    if not path.is_file():
        return False
    try:
        payload = _read_json_object(path, label="UAT ownership record")
    except UatIsolationError:
        return False
    return payload.get("nonce") == nonce and (
        pid is None or int(payload.get("pid") or 0) == pid
    )


def _owned_process_record(
    path: Path,
    *,
    nonce: str,
    pid: int,
    creation_token: str,
) -> bool:
    if not _owned_record(path, nonce=nonce, pid=pid):
        return False
    try:
        payload = _read_json_object(path, label="UAT ownership record")
    except UatIsolationError:
        return False
    return payload.get("process_creation_token") == creation_token


def cleanup_owned_launch(
    *,
    lease_path: Path,
    pid_path: Path,
    attestation_path: Path,
    nonce: str,
    terminate: bool,
) -> dict[str, Any]:
    pid = 0
    creation_token = ""
    if lease_path.is_file():
        try:
            lease_record = _read_json_object(lease_path, label="UAT launch lease")
            if lease_record.get("nonce") != nonce:
                return {"pid": 0, "removed": []}
            pid = int(lease_record.get("pid") or 0)
            creation_token = str(lease_record.get("process_creation_token") or "")
        except (UatIsolationError, ValueError, TypeError):
            pid = 0
            creation_token = ""
    child_identity = pid_path.with_name(f".{pid_path.name}.{nonce}.child.json")
    serving_pid = 0
    serving_token = ""
    if attestation_path.is_file() and _owned_record(attestation_path, nonce=nonce):
        attestation = _read_json_object(attestation_path, label="UAT attestation")
        serving_pid = int(attestation.get("pid") or 0)
        serving_token = str(attestation.get("process_creation_token") or "")
    if child_identity.is_file():
        if not _owned_record(child_identity, nonce=nonce):
            raise UatIsolationError(
                "UAT child identity belongs to another launch; refusing cleanup"
            )
        child_record = _read_json_object(child_identity, label="UAT child identity")
        child_pid = int(child_record.get("pid") or 0)
        child_token = str(child_record.get("process_creation_token") or "")
        if serving_pid and (child_pid != serving_pid or child_token != serving_token):
            raise UatIsolationError("UAT child identity disagrees with attestation")
        serving_pid = child_pid
        serving_token = child_token

    serving_stopped = serving_pid <= 0 or serving_pid == pid
    if serving_pid > 0 and serving_pid != pid:
        current_serving_token = process_creation_token(serving_pid)
        if current_serving_token is None:
            serving_stopped = True
        elif not serving_token or current_serving_token != serving_token:
            raise UatIsolationError(
                "UAT serving PID was reused or lacks a trustworthy process identity"
            )
        elif terminate and os.name == "nt":
            _terminate_owned_windows_process(serving_pid, serving_token)
            serving_stopped = True
        elif terminate:
            try:
                os.kill(serving_pid, 15)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 10
            while (
                process_creation_token(serving_pid) == serving_token
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if process_creation_token(serving_pid) is not None:
                raise UatIsolationError(
                    f"Owned UAT serving process PID {serving_pid} did not stop"
                )
            serving_stopped = True
        else:
            serving_stopped = False

    wrapper_stopped = pid <= 0
    if terminate and pid > 0:
        if not creation_token or not _owned_process_record(
            lease_path,
            nonce=nonce,
            pid=pid,
            creation_token=creation_token,
        ):
            raise UatIsolationError("UAT lease lacks a trustworthy process identity")
        current_token = process_creation_token(pid)
        if current_token is None:
            wrapper_stopped = True
        elif current_token != creation_token:
            raise UatIsolationError(
                "UAT PID was reused by another process; refusing to terminate or clean ownership"
            )
        elif os.name == "nt":
            _terminate_owned_windows_process(pid, creation_token)
            wrapper_stopped = True
        else:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 10
            while process_creation_token(pid) == creation_token and time.monotonic() < deadline:
                time.sleep(0.05)
            if process_creation_token(pid) is not None:
                raise UatIsolationError(f"Owned UAT process PID {pid} did not stop")
            wrapper_stopped = True
    elif pid > 0:
        wrapper_stopped = process_creation_token(pid) is None

    if not wrapper_stopped or not serving_stopped:
        raise UatIsolationError("Refusing to remove ownership for a running UAT process")
    removed: list[str] = []
    for path in (attestation_path, pid_path, lease_path):
        expected_record_pid = (
            serving_pid if path == attestation_path and serving_pid else pid
        )
        if _owned_record(
            path,
            nonce=nonce,
            pid=expected_record_pid if path != lease_path else None,
        ):
            path.unlink(missing_ok=True)
            removed.append(str(path))
    if _owned_record(child_identity, nonce=nonce, pid=serving_pid or None):
        child_identity.unlink(missing_ok=True)
        removed.append(str(child_identity))
    return {"pid": pid, "removed": removed}


def port_owner_pid(port: int) -> int | None:
    if os.name != "nt":
        return None
    script = (
        "$item=Get-NetTCPConnection -LocalAddress 127.0.0.1 "
        f"-LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty OwningProcess; "
        "if($null -ne $item){[Console]::Out.Write($item)}"
    )
    windows_root = _trusted_windows_directory()
    result = subprocess.run(
        [
            str(windows_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        env=isolated_child_environment(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    value = result.stdout.strip()
    return int(value) if result.returncode == 0 and value.isdigit() else None


def assert_launch_ownership(
    *,
    pid_path: Path,
    attestation_path: Path,
    expected_pid: int,
    expected_nonce: str,
    port: int,
    listener_resolver: Callable[[int], int | None] = port_owner_pid,
) -> dict[str, Any]:
    """Bind the PID lease, app attestation and actual loopback listener."""

    if not _owned_record(pid_path, nonce=expected_nonce, pid=expected_pid):
        raise UatIsolationError("UAT PID ownership does not match this launch nonce")
    pid_record = _read_json_object(pid_path, label="UAT PID ownership")
    creation_token = str(pid_record.get("process_creation_token") or "")
    if not creation_token or process_creation_token(expected_pid) != creation_token:
        raise UatIsolationError("UAT process creation identity does not match ownership")
    payload = verify_uat_attestation(
        attestation_path,
        expected_pid,
        expected_nonce,
    )
    owner = listener_resolver(port)
    serving_pid = int(payload.get("pid") or 0)
    if owner != serving_pid:
        raise UatIsolationError(
            f"UAT listener owner mismatch: port={port}, expected={serving_pid}, actual={owner}"
        )
    return payload


def spawn_uat_process(
    *,
    python: Path,
    project_root: Path,
    port: int,
    stdout_path: Path,
    stderr_path: Path,
    nonce: str,
    lease_path: Path,
    pid_path: Path,
    attestation_path: Path,
    overrides: dict[str, str] | None = None,
    argv: list[str] | None = None,
    git: Path | str | None = None,
    temp_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Spawn Uvicorn without PowerShell Start-Process's Path/PATH crash path."""

    executable = canonical_path(python, label="UAT Python")
    app_root = canonical_path(project_root, label="UAT project root")
    if not executable.is_file():
        raise UatIsolationError(f"UAT Python does not exist: {executable}")
    for label, target in (("stdout", stdout_path), ("stderr", stderr_path)):
        target = _absolute_lexical(target)
        assert_no_reparse_components(target.parent, label=f"UAT {label} log directory")
        if target.exists():
            raise UatIsolationError(f"Refusing to overwrite an existing UAT log: {target}")
    if not nonce or len(nonce) < 16:
        raise UatIsolationError("UAT launch nonce is missing or too short")
    for target in (lease_path, pid_path, attestation_path):
        target = _absolute_lexical(target)
        assert_no_reparse_components(target.parent, label="UAT ownership directory")
    lease = _absolute_lexical(lease_path)
    try:
        _json_write_exclusive(
            lease,
            {
                "nonce": nonce,
                "pid": 0,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    except FileExistsError as error:
        raise UatIsolationError(f"Another UAT launch owns the startup lease: {lease}") from error
    merged_overrides = dict(overrides or {})
    merged_overrides[LAUNCH_NONCE_ENV] = nonce
    environment = isolated_child_environment(
        merged_overrides,
        python=executable,
        git=git,
        temp_dir=temp_dir,
    )
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    )
    try:
        stdout_handle = stdout_path.open("xb")
        try:
            stderr_handle = stderr_path.open("xb")
        except Exception:
            stdout_handle.close()
            stdout_path.unlink(missing_ok=True)
            raise
    except Exception:
        if _owned_record(lease, nonce=nonce):
            lease.unlink(missing_ok=True)
        raise
    try:
        command = argv or uat_server_command(executable, app_root, port)
        process = subprocess.Popen(
            command,
            cwd=app_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creation_flags,
            close_fds=True,
        )
    except Exception:
        stdout_handle.close()
        stderr_handle.close()
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        if _owned_record(lease, nonce=nonce):
            lease.unlink(missing_ok=True)
        raise
    stdout_handle.close()
    stderr_handle.close()
    try:
        process_token = _spawned_process_creation_token(process)
    except Exception:
        _terminate_spawned_process(process)
        if _owned_record(lease, nonce=nonce):
            lease.unlink(missing_ok=True)
        raise
    ownership = {
        "nonce": nonce,
        "pid": process.pid,
        "process_creation_token": process_token,
        "port": port,
        "isolation_id": merged_overrides.get(ISOLATION_ID_ENV, ""),
        "attestation_path": str(_absolute_lexical(attestation_path)),
        "lease_path": str(lease),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    temporary_lease = lease.with_name(f".{lease.name}.{nonce}.tmp")
    try:
        _json_write_exclusive(temporary_lease, ownership)
        os.replace(temporary_lease, lease)
        _json_write_exclusive(_absolute_lexical(pid_path), ownership)
    except Exception:
        temporary_lease.unlink(missing_ok=True)
        _terminate_spawned_process(process)
        for owned_path in (
            _absolute_lexical(attestation_path),
            _absolute_lexical(pid_path),
            lease,
        ):
            if _owned_record(owned_path, nonce=nonce):
                owned_path.unlink(missing_ok=True)
        raise
    return {
        "pid": process.pid,
        "nonce": nonce,
        "lease": str(lease),
        "stdout": str(_absolute_lexical(stdout_path)),
        "stderr": str(_absolute_lexical(stderr_path)),
    }


def uat_server_command(python: Path, project_root: Path, port: int) -> list[str]:
    """Build the isolated Uvicorn command without trusting PYTHONPATH."""

    executable = canonical_path(python, label="UAT Python")
    app_root = canonical_path(project_root, label="UAT project root")
    return [
        str(executable),
        "-I",
        "-X",
        "utf8",
        str(Path(__file__).resolve()),
        "serve",
        "--project-root",
        str(app_root),
        "--port",
        str(port),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tianming ERP UAT isolation gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--database", type=Path, required=True)
    prepare.add_argument("--port", type=int, required=True)
    prepare.add_argument("--layout-source", type=Path, required=True)
    prepare.add_argument("--source-database", type=Path)
    prepare.add_argument("--twin-database-source", type=Path)
    prepare.add_argument("--protected-path", type=Path, action="append", default=[])
    prepare.add_argument("--forbidden-root", type=Path, action="append", default=[])
    prepare.add_argument("--launch-nonce")

    subparsers.add_parser("validate")
    verify = subparsers.add_parser("verify-attestation")
    verify.add_argument("--path", type=Path, required=True)
    verify.add_argument("--expected-pid", type=int, required=True)
    verify.add_argument("--expected-nonce", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--cwd", type=Path, required=True)
    run.add_argument("--python", type=Path, required=True)
    run.add_argument("--git", type=Path, required=True)
    run.add_argument("--temp-dir", type=Path, required=True)
    run.add_argument("--environment-base64", required=True)
    run.add_argument("--command-base64", required=True)

    spawn = subparsers.add_parser("spawn")
    spawn.add_argument("--python", type=Path, required=True)
    spawn.add_argument("--git", type=Path, required=True)
    spawn.add_argument("--project-root", type=Path, required=True)
    spawn.add_argument("--port", type=int, required=True)
    spawn.add_argument("--stdout", type=Path, required=True)
    spawn.add_argument("--stderr", type=Path, required=True)
    spawn.add_argument("--nonce", required=True)
    spawn.add_argument("--lease", type=Path, required=True)
    spawn.add_argument("--pid", type=Path, required=True)
    spawn.add_argument("--attestation", type=Path, required=True)
    spawn.add_argument("--temp-dir", type=Path, required=True)
    spawn.add_argument("--environment-base64", required=True)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--project-root", type=Path, required=True)
    serve.add_argument("--port", type=int, required=True)

    cleanup = subparsers.add_parser("cleanup-owned")
    cleanup.add_argument("--lease", type=Path, required=True)
    cleanup.add_argument("--pid", type=Path, required=True)
    cleanup.add_argument("--attestation", type=Path, required=True)
    cleanup.add_argument("--nonce", required=True)
    cleanup.add_argument("--terminate", action="store_true")

    ownership = subparsers.add_parser("assert-ownership")
    ownership.add_argument("--pid", type=Path, required=True)
    ownership.add_argument("--attestation", type=Path, required=True)
    ownership.add_argument("--expected-pid", type=int, required=True)
    ownership.add_argument("--expected-nonce", required=True)
    ownership.add_argument("--port", type=int, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "prepare":
            result = prepare_uat_layout(
                root=args.root,
                database=args.database,
                port=args.port,
                layout_source=args.layout_source,
                source_database=args.source_database,
                twin_database_source=args.twin_database_source,
                protected_paths=args.protected_path,
                forbidden_roots=args.forbidden_root,
                launch_nonce=args.launch_nonce,
            )
        elif args.command == "validate":
            result = validate_uat_environment()
        elif args.command == "verify-attestation":
            result = verify_uat_attestation(
                args.path,
                args.expected_pid,
                args.expected_nonce,
            )
        elif args.command == "run":
            environment = _decode_base64_json_object(
                args.environment_base64,
                label="isolated environment",
            )
            command = _decode_base64_json_array(
                args.command_base64,
                label="isolated command",
            )
            result = run_isolated_command(
                argv=command,
                cwd=args.cwd,
                overrides=environment,
                python=args.python,
                git=args.git,
                temp_dir=args.temp_dir,
            )
        elif args.command == "spawn":
            environment = _decode_base64_json_object(
                args.environment_base64,
                label="isolated environment",
            )
            result = spawn_uat_process(
                python=args.python,
                git=args.git,
                project_root=args.project_root,
                port=args.port,
                stdout_path=args.stdout,
                stderr_path=args.stderr,
                nonce=args.nonce,
                lease_path=args.lease,
                pid_path=args.pid,
                attestation_path=args.attestation,
                overrides=environment,
                temp_dir=args.temp_dir,
            )
        elif args.command == "serve":
            app_root = canonical_path(args.project_root, label="UAT project root")
            sys.path.insert(0, str(app_root))
            from uvicorn import Config, Server

            config = Config(
                "app.main:app",
                host="127.0.0.1",
                port=args.port,
                workers=1,
            )
            server = Server(config)
            pid_path = _required_environment_path("ERP_UAT_PID_PATH")
            nonce = os.environ.get(LAUNCH_NONCE_ENV, "")
            token = process_creation_token(os.getpid())
            if not token or len(nonce) < 16:
                raise UatIsolationError("Cannot identify the UAT serving process")
            child_identity_path = pid_path.with_name(
                f".{pid_path.name}.{nonce}.child.json"
            )
            _json_write_exclusive(
                child_identity_path,
                {
                    "nonce": nonce,
                    "pid": os.getpid(),
                    "process_creation_token": token,
                },
            )
            import asyncio

            asyncio.run(server.serve())
            return 0 if server.started else 1
        elif args.command == "cleanup-owned":
            result = cleanup_owned_launch(
                lease_path=args.lease,
                pid_path=args.pid,
                attestation_path=args.attestation,
                nonce=args.nonce,
                terminate=args.terminate,
            )
        else:
            result = assert_launch_ownership(
                pid_path=args.pid,
                attestation_path=args.attestation,
                expected_pid=args.expected_pid,
                expected_nonce=args.expected_nonce,
                port=args.port,
            )
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P0-5B phase 2: signed active-runtime switching with one recovery attempt.

The controller checkout is never switched.  Prepare creates only new evidence,
SQLite copies and an immutable activation runtime below the fixed rollback
runtime root.  Apply is split into explicit authorization, pointer activation,
verification and recovery commands so the Windows wrapper can stop/start the
single verified ERP process without weakening the Python evidence gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

if __package__:
    from scripts.admin import release_erp, rollback_runtime
    from scripts.admin.release_state_common import (
        ReleaseStateError,
        assert_formal_release_allowed as common_assert_formal_release_allowed,
        runtime_config_fingerprint,
        sha256_file,
        sign_evidence,
        sqlite_logical_fingerprint_via_copy,
        verify_evidence,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import release_erp  # type: ignore[no-redef]
    import rollback_runtime  # type: ignore[no-redef]
    from release_state_common import (  # type: ignore[no-redef]
        ReleaseStateError,
        assert_formal_release_allowed as common_assert_formal_release_allowed,
        runtime_config_fingerprint,
        sha256_file,
        sign_evidence,
        sqlite_logical_fingerprint_via_copy,
        verify_evidence,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAN_PURPOSE = "rollback-switch-plan-v1"
POINTER_PURPOSE = "active-runtime-pointer-v1"
TICKET_PURPOSE = "rollback-switch-ticket-v1"
CONSUMPTION_PURPOSE = "rollback-switch-consumption-v1"
ACTIVATION_CLAIM_PURPOSE = "rollback-switch-activation-claim-v1"
REVOCATION_PURPOSE = "rollback-switch-revocation-v1"
VALIDATION_PURPOSE = "rollback-switch-loopback-validation-v1"
AUTO_RESTORE_PERMIT_PURPOSE = "rollback-switch-auto-restore-permit-v1"
EXPOSURE_INTENT_PURPOSE = "rollback-switch-production-exposure-intent-v1"
EXPOSURE_PURPOSE = "rollback-switch-production-exposure-v1"
EVENT_PURPOSE = "rollback-switch-event-v1"
RESULT_PURPOSE = "rollback-switch-result-v1"
CONTROLLER_MANIFEST_ALGORITHM = "rollback-switch-controller-v1"
IMMUTABLE_MANIFEST_ALGORITHM = "activation-runtime-files-v1"
DEFAULT_PLAN_TTL_MINUTES = 30
ALLOWED_MUTABLE_RUNTIME_PREFIXES = (
    "data/pdf_training_samples/",
    "static/uploads/",
    "logs/",
)
CONTROLLER_FILES = (
    "scripts/admin/release_erp.py",
    "scripts/admin/release_erp.ps1",
    "scripts/admin/release_state_common.py",
    "scripts/admin/rollback_runtime.py",
    "scripts/admin/rollback_execute.py",
    "scripts/admin/rollback_execute.ps1",
    "scripts/windows/erp_process_identity.ps1",
    "scripts/windows/start_erp.ps1",
)


class RollbackExecutionError(RuntimeError):
    """A phase-2 rollback gate rejected an unsafe operation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: object, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError as error:
        raise RollbackExecutionError(f"{label}时间格式错误") from error
    if parsed.tzinfo is None:
        raise RollbackExecutionError(f"{label}必须包含时区")
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise RollbackExecutionError(f"{label}不存在：{resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RollbackExecutionError(f"{label}无法读取：{error}") from error
    if not isinstance(payload, dict):
        raise RollbackExecutionError(f"{label}格式错误")
    return payload


def _verify_signed(
    path: Path,
    *,
    label: str,
    purpose: str,
    project_root: Path,
) -> dict[str, Any]:
    payload = _read_json(path, label=label)
    try:
        verify_evidence(payload, project_root=project_root, purpose=purpose)
    except ReleaseStateError as error:
        raise RollbackExecutionError(str(error)) from error
    return payload


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _unsigned_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key != "evidence_hmac"
    }


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_signed_atomic(
    path: Path,
    payload: dict[str, Any],
    *,
    project_root: Path,
    purpose: str,
    replace: bool,
) -> dict[str, Any]:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not replace:
        raise RollbackExecutionError(f"证据文件已存在，拒绝覆盖：{target}")
    signed = sign_evidence(payload, project_root=project_root, purpose=purpose)
    encoded = (
        json.dumps(signed, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, target)
        else:
            try:
                # A hard-link publication is atomic and refuses an existing
                # destination on both Windows and POSIX.  Unlike
                # exists()+replace it cannot overwrite a one-time ticket,
                # consumption record or active pointer in a concurrent run.
                os.link(temporary, target)
            except FileExistsError as error:
                raise RollbackExecutionError(
                    f"证据文件已存在，拒绝覆盖：{target}"
                ) from error
            except OSError as error:
                raise RollbackExecutionError(
                    f"证据文件无法安全原子发布：{target}：{error}"
                ) from error
            try:
                temporary.unlink()
            except OSError:
                # The destination is already atomically published.  A stale
                # hidden temporary file is harmless and must not make callers
                # misclassify the committed pointer as "not switched".
                pass
        _fsync_directory(target.parent)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return signed


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise RollbackExecutionError(
            f"证据文件已存在，拒绝覆盖：{path.resolve()}"
        ) from error
    _fsync_directory(path.parent)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _is_link_or_reparse(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError as error:
        raise RollbackExecutionError(f"无法读取路径属性：{path}") from error
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _assert_plain_path(
    path: Path,
    *,
    label: str,
    must_exist: bool = True,
) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path)))
    if must_exist and not lexical.exists() and not lexical.is_symlink():
        raise RollbackExecutionError(f"{label}不存在：{lexical}")
    current = lexical
    while True:
        try:
            current.lstat()
        except FileNotFoundError:
            pass
        else:
            if _is_link_or_reparse(current):
                raise RollbackExecutionError(
                    f"{label}不得使用链接或 reparse point"
                )
        if current.parent == current:
            break
        current = current.parent
    return lexical.resolve(strict=must_exist)


def _require_output_root(output_root: Path, project_root: Path) -> Path:
    requested = Path(os.path.abspath(os.fspath(output_root)))
    _assert_plain_path(
        requested,
        label="阶段2输出目录路径",
        must_exist=False,
    )
    root = requested.resolve()
    allowed_requested = (
        project_root.resolve().parent / "tm-rollback-runtimes"
    )
    _assert_plain_path(
        allowed_requested,
        label="固定回退目录路径",
        must_exist=False,
    )
    allowed = allowed_requested.resolve()
    if root == allowed or not _within(root, allowed):
        raise RollbackExecutionError(f"阶段2输出目录必须位于固定目录内：{allowed}")
    if _within(root, project_root):
        raise RollbackExecutionError("阶段2输出目录不得位于控制 checkout 内")
    if root == Path(root.anchor):
        raise RollbackExecutionError("阶段2输出目录不得为驱动器根目录")
    if allowed_requested.exists():
        _assert_plain_path(allowed_requested, label="固定回退目录")
    if root.exists():
        if _is_link_or_reparse(root):
            raise RollbackExecutionError("阶段2输出目录不得使用链接或 reparse point")
        if any(root.iterdir()):
            raise RollbackExecutionError(
                f"阶段2输出目录必须不存在或为空：{root}"
            )
    requested.mkdir(parents=True, exist_ok=True)
    return _assert_plain_path(requested, label="阶段2输出目录")


def _git(project_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise RollbackExecutionError(
            f"Git 检查失败：{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def _checkout_state(project_root: Path) -> dict[str, str]:
    return {
        "branch": _git(project_root, "branch", "--show-current"),
        "head": _git(project_root, "rev-parse", "HEAD"),
        "tracked_status": _git(
            project_root,
            "status",
            "--porcelain",
            "--untracked-files=no",
        ),
    }


def _assert_formal_checkout(
    project_root: Path,
    *,
    expected_sha: str,
    expected_state: dict[str, Any] | None = None,
) -> dict[str, str]:
    state = _checkout_state(project_root)
    if state["branch"] != "factory-current-baseline":
        raise RollbackExecutionError(
            "阶段2只允许在干净的 factory-current-baseline 正式 checkout 执行"
        )
    if state["head"] != expected_sha:
        raise RollbackExecutionError("正式 checkout HEAD 与签名发布版本不一致")
    if state["tracked_status"]:
        raise RollbackExecutionError("正式 checkout 存在已跟踪文件修改，禁止切换")
    if expected_state is not None and state != expected_state:
        raise RollbackExecutionError("Prepare 后正式 checkout 状态发生变化")
    return state


def _controller_manifest(project_root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for relative in CONTROLLER_FILES:
        path = _assert_plain_path(
            project_root / relative,
            label=f"阶段2控制文件 {relative}",
        )
        if not path.is_file():
            raise RollbackExecutionError(f"阶段2控制文件不存在：{relative}")
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    digest = hashlib.sha256(_canonical_bytes({"files": files})).hexdigest()
    return {
        "algorithm": CONTROLLER_MANIFEST_ALGORITHM,
        "sha256": digest,
        "file_count": len(files),
        "files": files,
    }


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_mutable_runtime_path(relative: str) -> bool:
    normalized = relative.rstrip("/") + ("/" if relative else "")
    return any(
        normalized.startswith(prefix)
        for prefix in ALLOWED_MUTABLE_RUNTIME_PREFIXES
    )


def _immutable_runtime_manifest(runtime_dir: Path) -> dict[str, Any]:
    root = _assert_plain_path(runtime_dir, label="活动运行目录")
    if not root.is_dir():
        raise RollbackExecutionError("活动运行目录不存在或使用了链接")
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = _relative_posix(path, root)
        if _is_link_or_reparse(path):
            raise RollbackExecutionError(
                f"活动运行目录包含链接或 reparse point：{relative}"
            )
        if _is_mutable_runtime_path(relative):
            continue
        if path.is_file():
            files.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    digest = hashlib.sha256(_canonical_bytes({"files": files})).hexdigest()
    return {
        "algorithm": IMMUTABLE_MANIFEST_ALGORITHM,
        "sha256": digest,
        "file_count": len(files),
        "files": files,
        "mutable_prefixes": list(ALLOWED_MUTABLE_RUNTIME_PREFIXES),
    }


def _copy_mutable_snapshot(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    source = _assert_plain_path(source, label="共享业务文件目录")
    if not source.is_dir():
        raise RollbackExecutionError(f"共享业务文件路径不是目录：{source}")
    for entry in sorted(source.rglob("*")):
        if _is_link_or_reparse(entry):
            raise RollbackExecutionError(
                f"共享业务文件包含链接或 reparse point：{entry}"
            )
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        symlinks=True,
    )
    for entry in sorted(destination.rglob("*")):
        if _is_link_or_reparse(entry):
            raise RollbackExecutionError(
                f"共享业务文件副本包含链接或 reparse point：{entry}"
            )


def _prepare_activation_runtime(
    source_runtime: Path,
    destination: Path,
    project_root: Path,
) -> dict[str, Any]:
    source = _assert_plain_path(source_runtime, label="阶段1旧代码目录")
    source_before = rollback_runtime._file_manifest(source)
    if destination.exists():
        raise RollbackExecutionError(
            f"活动运行候选目录已存在，拒绝覆盖：{destination}"
        )
    for entry in sorted(source.rglob("*")):
        if _is_link_or_reparse(entry):
            raise RollbackExecutionError(
                f"阶段1旧代码目录包含链接或 reparse point：{entry}"
            )
    shutil.copytree(source, destination, symlinks=True)
    _copy_mutable_snapshot(
        project_root / "static" / "uploads",
        destination / "static" / "uploads",
    )
    _copy_mutable_snapshot(
        project_root / "data" / "pdf_training_samples",
        destination / "data" / "pdf_training_samples",
    )
    if rollback_runtime._file_manifest(source) != source_before:
        raise RollbackExecutionError("复制活动目录期间阶段1旧代码证据发生变化")
    return _immutable_runtime_manifest(destination)


def _runtime_service_config(project_root: Path) -> dict[str, Any]:
    script = (
        "import json,sys;"
        f"sys.path.insert(0,{str(project_root.resolve())!r});"
        "from app.core.config import load_settings;"
        "s=load_settings();"
        "print(json.dumps({"
        "'bind_host':s.bind_host,'port':s.port,'workers':s.workers,"
        "'environment':s.environment,"
        "'production_transport':s.production_transport,"
        "'health_url':s.health_url,'browser_url':s.browser_url,"
        "'database_path':str(s.database_path)"
        "},ensure_ascii=False))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", script],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise RollbackExecutionError(
            "无法读取正式运行配置："
            + (result.stderr or result.stdout).strip()
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RollbackExecutionError("正式运行配置输出不是有效 JSON") from error
    if (
        payload.get("environment") != "production"
        or int(payload.get("workers") or 0) != 1
    ):
        raise RollbackExecutionError("阶段2只支持 production 单 worker 运行契约")
    port = int(payload.get("port") or 0)
    if not (1 <= port <= 65535):
        raise RollbackExecutionError("正式运行端口无效")
    payload["local_health_url"] = f"http://127.0.0.1:{port}/api/health"
    return payload


def _token_digest(*, purpose: str, plan_id: str, token: str) -> str:
    if not token:
        return ""
    payload = f"{purpose}\0{plan_id}\0{token}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _new_token(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(32)}"


def _file_identity(path: Path, *, label: str) -> dict[str, int]:
    resolved = _assert_plain_path(path, label=label)
    stat_result = resolved.stat()
    identity = {
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "link_count": int(stat_result.st_nlink),
    }
    if identity["inode"] <= 0:
        raise RollbackExecutionError(f"{label}缺少稳定文件身份")
    if identity["link_count"] != 1:
        raise RollbackExecutionError(f"{label}不得是硬链接数据库")
    return identity


def _database_snapshot(path: Path, *, label: str) -> dict[str, Any]:
    identity_before = _file_identity(path, label=label)
    snapshot = release_erp.inspect_database(path)
    release_erp.assert_healthy(snapshot, label=label)
    release_erp.assert_business_smoke(snapshot, label=label)
    snapshot["logical_fingerprint"] = sqlite_logical_fingerprint_via_copy(path)
    identity_after = _file_identity(path, label=label)
    if identity_after != identity_before:
        raise RollbackExecutionError(f"{label}在核验期间被替换")
    snapshot["file_identity"] = identity_after
    return snapshot


def _assert_snapshot_matches(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    label: str,
    include_logical: bool = True,
    include_path: bool = True,
) -> None:
    keys = ["revision", "integrity_check", "foreign_key_violations"]
    if include_path:
        keys[0:0] = ["path", "file_identity"]
    for key in keys:
        if actual.get(key) != expected.get(key):
            raise RollbackExecutionError(f"{label} {key} 已变化")
    if include_logical and actual.get("logical_fingerprint") != expected.get(
        "logical_fingerprint"
    ):
        raise RollbackExecutionError(f"{label}逻辑内容已变化")


def _safe_environment_overrides(
    project_root: Path,
    database_path: Path,
) -> dict[str, str]:
    private_root = (project_root / "data" / "private_uploads").resolve()
    return {
        "ERP_DATABASE_PATH": str(database_path.resolve()),
        "ERP_SECRET_KEY_FILE": str(
            (project_root / "data" / "session_secret.key").resolve()
        ),
        "ERP_FILE_STORAGE_DIR": str(private_root),
        "ERP_UPLOAD_TEMP_DIR": str((private_root / "_temporary").resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _prepare_validation_environment(
    output_root: Path,
    database_path: Path,
) -> dict[str, Any]:
    """Create disposable mutable paths used only by loopback warmup."""

    validation_root = output_root / "validation"
    private_root = validation_root / "private_uploads"
    temporary_root = private_root / "_temporary"
    backup_root = validation_root / "backups"
    secret_path = validation_root / "session_secret.key"
    for directory in (temporary_root, backup_root):
        directory.mkdir(parents=True, exist_ok=True)
        _assert_plain_path(directory, label="loopback 隔离可变目录")
    try:
        with secret_path.open("xb") as handle:
            handle.write(secrets.token_hex(32).encode("ascii") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise RollbackExecutionError(
            "loopback 隔离会话密钥已存在，拒绝覆盖"
        ) from error
    _fsync_directory(secret_path.parent)
    return {
        "root": str(validation_root.resolve()),
        "session_secret_sha256": sha256_file(secret_path),
        "environment_overrides": {
            "ERP_DATABASE_PATH": str(database_path.resolve()),
            "ERP_SECRET_KEY_FILE": str(secret_path.resolve()),
            "ERP_FILE_STORAGE_DIR": str(private_root.resolve()),
            "ERP_UPLOAD_TEMP_DIR": str(temporary_root.resolve()),
            "ERP_BACKUP_DIR": str(backup_root.resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    }


def _verify_validation_environment(
    plan: dict[str, Any],
) -> dict[str, str]:
    target = plan.get("target_runtime") or {}
    runtime_dir = Path(str(target.get("runtime_dir") or "")).resolve()
    output_root = runtime_dir.parent.parent
    candidate_database = Path(
        str((plan.get("candidate_database") or {}).get("path") or "")
    ).resolve()
    validation = plan.get("validation_environment")
    if not isinstance(validation, dict):
        raise RollbackExecutionError("阶段2计划缺少 loopback 隔离环境")
    overrides = validation.get("environment_overrides")
    if not isinstance(overrides, dict):
        raise RollbackExecutionError("loopback 隔离环境覆盖格式错误")
    expected_root = (output_root / "validation").resolve()
    expected = {
        "ERP_DATABASE_PATH": str(candidate_database),
        "ERP_SECRET_KEY_FILE": str(
            (expected_root / "session_secret.key").resolve()
        ),
        "ERP_FILE_STORAGE_DIR": str(
            (expected_root / "private_uploads").resolve()
        ),
        "ERP_UPLOAD_TEMP_DIR": str(
            (expected_root / "private_uploads" / "_temporary").resolve()
        ),
        "ERP_BACKUP_DIR": str((expected_root / "backups").resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    if (
        Path(str(validation.get("root") or "")).resolve() != expected_root
        or overrides != expected
    ):
        raise RollbackExecutionError("loopback 隔离环境路径与本轮输出目录不一致")
    for name in (
        "ERP_SECRET_KEY_FILE",
        "ERP_FILE_STORAGE_DIR",
        "ERP_UPLOAD_TEMP_DIR",
        "ERP_BACKUP_DIR",
    ):
        path = _assert_plain_path(
            Path(expected[name]),
            label=f"loopback 隔离路径 {name}",
        )
        if name == "ERP_SECRET_KEY_FILE":
            if not path.is_file():
                raise RollbackExecutionError("loopback 隔离会话密钥不是文件")
        elif not path.is_dir():
            raise RollbackExecutionError(
                f"loopback 隔离路径 {name} 不是目录"
            )
    secret_path = Path(expected["ERP_SECRET_KEY_FILE"])
    if sha256_file(secret_path) != validation.get("session_secret_sha256"):
        raise RollbackExecutionError("loopback 隔离会话密钥已变化")
    return expected


def _pointer_payload(
    *,
    pointer_version: int,
    runtime_id: str,
    runtime_kind: str,
    runtime_dir: Path,
    code_sha: str,
    code_revision: str,
    database_path: Path,
    database_snapshot: dict[str, Any],
    python_path: Path,
    python_environment: dict[str, Any],
    service: dict[str, Any],
    environment_overrides: dict[str, str],
    immutable_manifest: dict[str, Any] | None,
    checkout_state: dict[str, str] | None,
    controller_manifest: dict[str, Any],
    previous_pointer_sha256: str | None,
    activation_plan_id: str | None,
    activation_claim_path: Path | None,
    revocation_path: Path | None,
    validation_event_path: Path | None,
    auto_restore_permit_path: Path | None,
    auto_restore_permit_consumed_path: Path | None,
    production_exposure_intent_path: Path | None,
    production_exposure_path: Path | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pointer_version": pointer_version,
        "updated_at": _utc_now(),
        "runtime_id": runtime_id,
        "runtime_kind": runtime_kind,
        "runtime_dir": str(runtime_dir.resolve()),
        "code_sha": code_sha,
        "code_revision": code_revision,
        "database_path": str(database_path.resolve()),
        "database_activation_snapshot": database_snapshot,
        "python_path": str(python_path.resolve()),
        "python_environment": python_environment,
        "service": service,
        "environment_overrides": environment_overrides,
        "immutable_manifest": immutable_manifest,
        "checkout_state": checkout_state,
        "controller_manifest": controller_manifest,
        "previous_pointer_sha256": previous_pointer_sha256,
        "activation_plan_id": activation_plan_id,
        "activation_claim_path": (
            str(activation_claim_path.resolve())
            if activation_claim_path is not None
            else None
        ),
        "revocation_path": (
            str(revocation_path.resolve())
            if revocation_path is not None
            else None
        ),
        "validation_event_path": (
            str(validation_event_path.resolve())
            if validation_event_path is not None
            else None
        ),
        "auto_restore_permit_path": (
            str(auto_restore_permit_path.resolve())
            if auto_restore_permit_path is not None
            else None
        ),
        "auto_restore_permit_consumed_path": (
            str(auto_restore_permit_consumed_path.resolve())
            if auto_restore_permit_consumed_path is not None
            else None
        ),
        "production_exposure_path": (
            str(production_exposure_path.resolve())
            if production_exposure_path is not None
            else None
        ),
        "production_exposure_intent_path": (
            str(production_exposure_intent_path.resolve())
            if production_exposure_intent_path is not None
            else None
        ),
    }


def _assert_allowed_pointer_path(pointer_path: Path, project_root: Path) -> Path:
    lexical = Path(os.path.abspath(os.fspath(pointer_path)))
    allowed_lexical = Path(
        os.path.abspath(
            os.fspath(project_root / "data" / "release_state")
        )
    )
    _assert_plain_path(
        allowed_lexical,
        label="活动运行指针目录",
        must_exist=False,
    )
    if (
        lexical.parent != allowed_lexical
        or lexical.name != "active_runtime.json"
    ):
        raise RollbackExecutionError(
            f"活动运行指针路径必须为：{allowed_lexical / 'active_runtime.json'}"
        )
    if _path_entry_exists(lexical):
        _assert_plain_path(lexical, label="活动运行指针")
    return lexical.resolve(strict=False)


def _activation_state_paths(
    plan_id: str,
    project_root: Path,
) -> tuple[Path, Path]:
    if not plan_id or len(plan_id) > 64 or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in plan_id
    ):
        raise RollbackExecutionError("活动运行指针的计划编号不安全")
    root = (
        project_root
        / "data"
        / "release_state"
        / "rollback_activations"
    ).resolve()
    return (
        root / f"{plan_id}.claim.json",
        root / f"{plan_id}.revoked.json",
    )


def _assert_no_outstanding_activations(project_root: Path) -> None:
    """Fail closed when a durable activation could still own production."""
    root = (
        project_root
        / "data"
        / "release_state"
        / "rollback_activations"
    )
    if not _path_entry_exists(root):
        return
    resolved_root = _assert_plain_path(root, label="回退激活状态目录")
    if not resolved_root.is_dir():
        raise RollbackExecutionError("回退激活状态路径不是目录")
    entries: dict[str, dict[str, Path]] = {}
    suffixes = {
        ".claim.json": "claim",
        ".revoked.json": "revocation",
        ".exposed.json": "exposure",
    }
    for entry in sorted(resolved_root.iterdir()):
        _assert_plain_path(entry, label="回退激活状态文件")
        if not entry.is_file():
            raise RollbackExecutionError(
                f"回退激活状态目录包含非文件条目：{entry.name}"
            )
        matched = False
        for suffix, kind in suffixes.items():
            if entry.name.endswith(suffix):
                plan_id = entry.name[: -len(suffix)]
                _activation_state_paths(plan_id, project_root)
                bucket = entries.setdefault(plan_id, {})
                if kind in bucket:
                    raise RollbackExecutionError("回退激活状态存在重复证据")
                bucket[kind] = entry.resolve()
                matched = True
                break
        if not matched:
            raise RollbackExecutionError(
                f"回退激活状态目录包含未知条目：{entry.name}"
            )
    for plan_id, state in entries.items():
        claim_path = state.get("claim")
        if claim_path is None:
            raise RollbackExecutionError(
                f"回退激活状态 {plan_id} 缺少声明，禁止隐式启动或普通发布"
            )
        claim = _verify_signed(
            claim_path,
            label="持久激活声明",
            purpose=ACTIVATION_CLAIM_PURPOSE,
            project_root=project_root,
        )
        if (
            claim.get("plan_id") != plan_id
            or not claim.get("runtime_id")
            or len(str(claim.get("target_pointer_sha256") or "")) != 64
        ):
            raise RollbackExecutionError("持久激活声明内容不一致")
        revocation_path = state.get("revocation")
        exposure_path = state.get("exposure")
        if revocation_path is None:
            raise RollbackExecutionError(
                f"回退激活 {plan_id} 尚未撤销；活动指针缺失时禁止"
                "回到正式 checkout，也禁止普通发布"
            )
        revocation = _verify_signed(
            revocation_path,
            label="持久激活撤销记录",
            purpose=REVOCATION_PURPOSE,
            project_root=project_root,
        )
        if (
            revocation.get("plan_id") != plan_id
            or revocation.get("runtime_id") != claim.get("runtime_id")
            or revocation.get("target_pointer_sha256")
            != claim.get("target_pointer_sha256")
            or revocation.get("activation_claim_sha256")
            != sha256_file(claim_path)
        ):
            raise RollbackExecutionError("持久激活撤销记录与声明不一致")
        if exposure_path is not None:
            raise RollbackExecutionError(
                "同一回退激活同时存在撤销和正式开放证据，状态冲突"
            )


def assert_formal_release_allowed(
    *,
    pointer_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Shared gate for ordinary releases and implicit formal startup."""
    try:
        return common_assert_formal_release_allowed(
            project_root=project_root,
            pointer_path=pointer_path,
        )
    except ReleaseStateError as error:
        raise RollbackExecutionError(str(error)) from error


def _path_entry_exists(path: Path) -> bool:
    """Return True for files, directories, symlinks and broken symlinks."""
    return os.path.lexists(os.fspath(path))


def _exposure_paths_from_pointer(
    pointer: dict[str, Any],
    *,
    project_root: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    plan_id = str(pointer.get("activation_plan_id") or "")
    runtime_dir = Path(str(pointer.get("runtime_dir") or "")).resolve()
    run_root = runtime_dir.parent.parent
    validation_event_path = Path(
        str(pointer.get("validation_event_path") or "")
    ).resolve()
    auto_restore_permit_path = Path(
        str(pointer.get("auto_restore_permit_path") or "")
    ).resolve()
    auto_restore_permit_consumed_path = Path(
        str(pointer.get("auto_restore_permit_consumed_path") or "")
    ).resolve()
    exposure_intent_path = Path(
        str(pointer.get("production_exposure_intent_path") or "")
    ).resolve()
    exposure_path = Path(
        str(pointer.get("production_exposure_path") or "")
    ).resolve()
    claim_path, _revocation_path = _activation_state_paths(
        plan_id,
        project_root,
    )
    expected_exposure = claim_path.with_name(f"{plan_id}.exposed.json")
    if (
        validation_event_path
        != (run_root / "loopback_validation_event.json").resolve()
        or auto_restore_permit_path
        != (run_root / "auto_restore_permit.json").resolve()
        or auto_restore_permit_consumed_path
        != (run_root / "auto_restore_permit.consumed.json").resolve()
        or exposure_intent_path
        != (run_root / "production_exposure_intent.json").resolve()
        or exposure_path != expected_exposure
    ):
        raise RollbackExecutionError("活动运行指针的网络开放证据路径不安全")
    return (
        validation_event_path,
        auto_restore_permit_path,
        auto_restore_permit_consumed_path,
        exposure_intent_path,
        exposure_path,
    )


def _exposure_marker_presence(
    *,
    intent_path: Path,
    exposure_path: Path,
) -> dict[str, bool]:
    return {
        "intent_present": _path_entry_exists(intent_path),
        "exposure_present": _path_entry_exists(exposure_path),
    }


def _verify_auto_restore_permit(
    path: Path,
    *,
    plan_id: str,
    runtime_id: str,
    permit_path: Path,
    consumed_path: Path,
    project_root: Path,
    label: str,
) -> dict[str, Any]:
    permit = _verify_signed(
        path,
        label=label,
        purpose=AUTO_RESTORE_PERMIT_PURPOSE,
        project_root=project_root,
    )
    if (
        permit.get("status") != "pre_exposure_auto_restore_permitted"
        or permit.get("plan_id") != plan_id
        or permit.get("runtime_id") != runtime_id
        or Path(str(permit.get("permit_path") or "")).resolve()
        != permit_path
        or Path(str(permit.get("consumed_path") or "")).resolve()
        != consumed_path
    ):
        raise RollbackExecutionError(f"{label}与阶段2计划不一致")
    return permit


def _assert_auto_restore_allowed(
    plan: dict[str, Any],
    *,
    project_root: Path,
) -> dict[str, Any]:
    permit_path = Path(
        str(plan.get("auto_restore_permit_path") or "")
    ).resolve()
    consumed_path = Path(
        str(plan.get("auto_restore_permit_consumed_path") or "")
    ).resolve()
    intent_path = Path(
        str(plan.get("production_exposure_intent_path") or "")
    ).resolve()
    exposure_path = Path(
        str(plan.get("production_exposure_path") or "")
    ).resolve()
    presence = _exposure_marker_presence(
        intent_path=intent_path,
        exposure_path=exposure_path,
    )
    if presence["intent_present"] or presence["exposure_present"]:
        raise RollbackExecutionError(
            "正式网络开放意向或承诺已经落盘；即使证据不完整，"
            "也禁止自动恢复当前版本或丢弃候选数据库"
        )
    if _path_entry_exists(consumed_path):
        raise RollbackExecutionError(
            "预开放自动恢复许可已经原子消费；即使开放证据丢失，"
            "也禁止自动恢复当前版本或丢弃候选数据库"
        )
    if not _path_entry_exists(permit_path):
        raise RollbackExecutionError(
            "预开放自动恢复许可缺失或已消费，禁止自动恢复"
        )
    return _verify_auto_restore_permit(
        permit_path,
        plan_id=str(plan.get("plan_id") or ""),
        runtime_id=str(
            (plan.get("target_runtime") or {}).get("runtime_id") or ""
        ),
        permit_path=permit_path,
        consumed_path=consumed_path,
        project_root=project_root,
        label="预开放自动恢复许可",
    )


def _consume_auto_restore_permit(
    plan: dict[str, Any],
    *,
    project_root: Path,
) -> dict[str, Any]:
    permit = _assert_auto_restore_allowed(
        plan,
        project_root=project_root,
    )
    permit_path = Path(str(plan["auto_restore_permit_path"])).resolve()
    consumed_path = Path(
        str(plan["auto_restore_permit_consumed_path"])
    ).resolve()
    consumed_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(permit_path, consumed_path)
    except FileExistsError as error:
        raise RollbackExecutionError(
            "预开放自动恢复许可已经消费，禁止重放"
        ) from error
    except OSError as error:
        raise RollbackExecutionError(
            f"无法原子消费预开放自动恢复许可：{error}"
        ) from error
    _fsync_directory(consumed_path.parent)
    try:
        permit_path.unlink()
        _fsync_directory(permit_path.parent)
    except OSError as error:
        raise RollbackExecutionError(
            "预开放自动恢复许可已产生消费见证但原许可未能移除；"
            "必须保留目标现场并人工处理"
        ) from error
    consumed = _verify_auto_restore_permit(
        consumed_path,
        plan_id=str(plan.get("plan_id") or ""),
        runtime_id=str(
            (plan.get("target_runtime") or {}).get("runtime_id") or ""
        ),
        permit_path=permit_path,
        consumed_path=consumed_path,
        project_root=project_root,
        label="已消费的预开放自动恢复许可",
    )
    if _unsigned_payload(consumed) != _unsigned_payload(permit):
        raise RollbackExecutionError("预开放自动恢复许可消费前后内容不一致")
    return consumed


def _verify_exposure_markers(
    pointer: dict[str, Any],
    *,
    claim: dict[str, Any],
    project_root: Path,
    allow_pre_exposure: bool,
) -> dict[str, Any]:
    (
        validation_event_path,
        auto_restore_permit_path,
        auto_restore_permit_consumed_path,
        exposure_intent_path,
        exposure_path,
    ) = _exposure_paths_from_pointer(pointer, project_root=project_root)
    presence = _exposure_marker_presence(
        intent_path=exposure_intent_path,
        exposure_path=exposure_path,
    )
    permit_present = _path_entry_exists(auto_restore_permit_path)
    permit_consumed = _path_entry_exists(
        auto_restore_permit_consumed_path
    )
    plan_id = str(pointer.get("activation_plan_id") or "")
    runtime_id = str(pointer.get("runtime_id") or "")
    if not presence["intent_present"] and not presence["exposure_present"]:
        if not allow_pre_exposure:
            raise RollbackExecutionError(
                "目标运行目录尚未完成 loopback 验证并承诺正式开放"
            )
        if not permit_present or permit_consumed:
            raise RollbackExecutionError(
                "预开放自动恢复许可缺失、损坏或已经消费"
            )
        _verify_auto_restore_permit(
            auto_restore_permit_path,
            plan_id=plan_id,
            runtime_id=runtime_id,
            permit_path=auto_restore_permit_path,
            consumed_path=auto_restore_permit_consumed_path,
            project_root=project_root,
            label="预开放自动恢复许可",
        )
        return {
            "state": "pre_exposure",
            "validation_event_path": str(validation_event_path),
            "auto_restore_permit_present": True,
            "auto_restore_permit_consumed": False,
            "production_exposure_intent_path": str(exposure_intent_path),
            "production_exposure_path": str(exposure_path),
            **presence,
        }
    if permit_present or not permit_consumed:
        raise RollbackExecutionError(
            "正式网络开放状态缺少已消费的自动恢复许可见证"
        )
    _verify_auto_restore_permit(
        auto_restore_permit_consumed_path,
        plan_id=plan_id,
        runtime_id=runtime_id,
        permit_path=auto_restore_permit_path,
        consumed_path=auto_restore_permit_consumed_path,
        project_root=project_root,
        label="已消费的预开放自动恢复许可",
    )
    if not (
        presence["intent_present"] and presence["exposure_present"]
    ):
        raise RollbackExecutionError(
            "正式网络开放状态不完整；已进入不可自动恢复边界，"
            "必须保留目标指针和候选数据库并人工处理"
        )
    intent = _verify_signed(
        exposure_intent_path,
        label="正式网络开放意向",
        purpose=EXPOSURE_INTENT_PURPOSE,
        project_root=project_root,
    )
    exposure = _verify_signed(
        exposure_path,
        label="正式网络开放承诺",
        purpose=EXPOSURE_PURPOSE,
        project_root=project_root,
    )
    validation = _verify_signed(
        validation_event_path,
        label="loopback 验证事件",
        purpose=VALIDATION_PURPOSE,
        project_root=project_root,
    )
    pointer_digest = hashlib.sha256(
        _canonical_bytes(_unsigned_payload(pointer))
    ).hexdigest()
    ticket_id = str(claim.get("ticket_id") or "")
    if (
        intent.get("status") != "production_exposure_intent_recorded"
        or intent.get("plan_id") != plan_id
        or intent.get("ticket_id") != ticket_id
        or intent.get("runtime_id") != runtime_id
        or intent.get("target_pointer_sha256") != pointer_digest
        or Path(
            str(intent.get("auto_restore_permit_consumed_path") or "")
        ).resolve()
        != auto_restore_permit_consumed_path
        or intent.get("auto_restore_permit_consumed_sha256")
        != sha256_file(auto_restore_permit_consumed_path)
        or Path(str(intent.get("validation_event_path") or "")).resolve()
        != validation_event_path
        or intent.get("validation_event_sha256")
        != sha256_file(validation_event_path)
        or exposure.get("status") != "production_exposure_committed"
        or exposure.get("plan_id") != plan_id
        or exposure.get("ticket_id") != ticket_id
        or exposure.get("runtime_id") != runtime_id
        or exposure.get("target_pointer_sha256") != pointer_digest
        or Path(
            str(exposure.get("auto_restore_permit_consumed_path") or "")
        ).resolve()
        != auto_restore_permit_consumed_path
        or exposure.get("auto_restore_permit_consumed_sha256")
        != sha256_file(auto_restore_permit_consumed_path)
        or Path(str(exposure.get("validation_event_path") or "")).resolve()
        != validation_event_path
        or exposure.get("validation_event_sha256")
        != sha256_file(validation_event_path)
        or Path(
            str(exposure.get("production_exposure_intent_path") or "")
        ).resolve()
        != exposure_intent_path
        or exposure.get("production_exposure_intent_sha256")
        != sha256_file(exposure_intent_path)
        or validation.get("status") != "loopback_validation_completed"
        or validation.get("plan_id") != plan_id
        or validation.get("ticket_id") != ticket_id
        or validation.get("runtime_id") != runtime_id
        or validation.get("network_exposure") != "loopback_only"
    ):
        raise RollbackExecutionError(
            "正式网络开放意向或承诺与活动指针不一致"
        )
    return {
        "state": "production_exposure_committed",
        "validation_event_path": str(validation_event_path),
        "auto_restore_permit_present": False,
        "auto_restore_permit_consumed": True,
        "production_exposure_intent_path": str(exposure_intent_path),
        "production_exposure_path": str(exposure_path),
        "intent": intent,
        "exposure": exposure,
        **presence,
    }


def _verify_archive_activation_state(
    pointer: dict[str, Any],
    *,
    project_root: Path,
    allow_pre_exposure: bool,
) -> dict[str, Any]:
    plan_id = str(pointer.get("activation_plan_id") or "")
    expected_claim, expected_revocation = _activation_state_paths(
        plan_id,
        project_root,
    )
    claim_path = Path(
        str(pointer.get("activation_claim_path") or "")
    ).resolve()
    revocation_path = Path(
        str(pointer.get("revocation_path") or "")
    ).resolve()
    if claim_path != expected_claim or revocation_path != expected_revocation:
        raise RollbackExecutionError("活动运行指针的持久状态路径不安全")
    if _path_entry_exists(revocation_path):
        revocation = _verify_signed(
            revocation_path,
            label="活动运行指针撤销记录",
            purpose=REVOCATION_PURPOSE,
            project_root=project_root,
        )
        if (
            revocation.get("plan_id") != plan_id
            or revocation.get("runtime_id") != pointer.get("runtime_id")
        ):
            raise RollbackExecutionError("活动运行指针撤销记录不一致")
        raise RollbackExecutionError("该活动运行指针已经撤销，禁止再次启动")
    claim = _verify_signed(
        claim_path,
        label="活动运行指针激活声明",
        purpose=ACTIVATION_CLAIM_PURPOSE,
        project_root=project_root,
    )
    pointer_digest = hashlib.sha256(
        _canonical_bytes(_unsigned_payload(pointer))
    ).hexdigest()
    if (
        claim.get("plan_id") != plan_id
        or claim.get("runtime_id") != pointer.get("runtime_id")
        or claim.get("target_pointer_sha256") != pointer_digest
    ):
        raise RollbackExecutionError("活动运行指针与持久激活声明不一致")
    _verify_exposure_markers(
        pointer,
        claim=claim,
        project_root=project_root,
        allow_pre_exposure=allow_pre_exposure,
    )
    return claim


def _verify_pointer_payload(
    pointer: dict[str, Any],
    *,
    project_root: Path,
    require_database_baseline: bool,
    require_activation_state: bool = True,
    allow_pre_exposure: bool = False,
) -> dict[str, Any]:
    if int(pointer.get("schema_version") or 0) != 1:
        raise RollbackExecutionError("活动运行指针版本不受支持")
    runtime_dir = _assert_plain_path(
        Path(str(pointer.get("runtime_dir") or "")),
        label="活动运行目录",
    )
    database = _assert_plain_path(
        Path(str(pointer.get("database_path") or "")),
        label="活动数据库",
    )
    python_path = _assert_plain_path(
        Path(str(pointer.get("python_path") or "")),
        label="活动 Python",
    )
    runtime_kind = str(pointer.get("runtime_kind") or "")
    code_sha = str(pointer.get("code_sha") or "")
    code_revision = str(pointer.get("code_revision") or "")
    if len(code_sha) != 40 or not code_revision:
        raise RollbackExecutionError("活动运行指针的代码身份不完整")
    if not python_path.is_file():
        raise RollbackExecutionError("活动运行指针的 Python 不存在")
    if pointer.get("python_environment") != rollback_runtime._python_environment(
        project_root,
        python_path,
    ):
        raise RollbackExecutionError("活动运行指针绑定的 Python 环境已变化")
    if pointer.get("controller_manifest") != _controller_manifest(project_root):
        raise RollbackExecutionError("活动运行指针绑定的控制器文件已变化")
    if runtime_kind == "formal_checkout":
        expected_state = pointer.get("checkout_state")
        if not isinstance(expected_state, dict):
            raise RollbackExecutionError("正式活动指针缺少 checkout 证据")
        _assert_formal_checkout(
            project_root,
            expected_sha=str(pointer.get("code_sha") or ""),
            expected_state=expected_state,
        )
        if runtime_dir != project_root.resolve():
            raise RollbackExecutionError("正式活动指针运行目录不一致")
        if database != (
            project_root / "data" / "carton_erp.sqlite3"
        ).resolve():
            raise RollbackExecutionError("正式活动指针数据库路径不一致")
    elif runtime_kind == "archive_activation":
        expected_state = pointer.get("checkout_state")
        if not isinstance(expected_state, dict):
            raise RollbackExecutionError("活动运行指针缺少正式控制 checkout 证据")
        _assert_formal_checkout(
            project_root,
            expected_sha=str(expected_state.get("head") or ""),
            expected_state=expected_state,
        )
        previous_pointer_sha256 = str(
            pointer.get("previous_pointer_sha256") or ""
        )
        if len(previous_pointer_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in previous_pointer_sha256.lower()
        ):
            raise RollbackExecutionError("活动运行指针缺少切换前指针哈希")
        plan_id = str(pointer.get("activation_plan_id") or "")
        expected_claim, expected_revocation = _activation_state_paths(
            plan_id,
            project_root,
        )
        if (
            Path(str(pointer.get("activation_claim_path") or "")).resolve()
            != expected_claim
            or Path(str(pointer.get("revocation_path") or "")).resolve()
            != expected_revocation
        ):
            raise RollbackExecutionError("活动运行指针的持久状态路径不安全")
        if require_activation_state:
            _verify_archive_activation_state(
                pointer,
                project_root=project_root,
                allow_pre_exposure=allow_pre_exposure,
            )
        allowed = (project_root.resolve().parent / "tm-rollback-runtimes").resolve()
        if not _within(runtime_dir, allowed) or runtime_dir == allowed:
            raise RollbackExecutionError("活动运行目录逃出固定回退目录")
        run_root = runtime_dir.parent.parent
        if not _within(database, run_root):
            raise RollbackExecutionError("活动数据库逃出本次回退证据目录")
        expected_manifest = pointer.get("immutable_manifest")
        if not isinstance(expected_manifest, dict) or (
            _immutable_runtime_manifest(runtime_dir) != expected_manifest
        ):
            raise RollbackExecutionError("活动运行目录代码文件已变化")
    else:
        raise RollbackExecutionError("活动运行指针类型不受支持")
    identity_before = _file_identity(database, label="活动数据库")
    snapshot = release_erp.inspect_database(database)
    release_erp.assert_healthy(snapshot, label="活动数据库")
    release_erp.assert_business_smoke(snapshot, label="活动数据库")
    if require_database_baseline:
        snapshot["logical_fingerprint"] = sqlite_logical_fingerprint_via_copy(
            database
        )
    identity_after = _file_identity(database, label="活动数据库")
    if identity_after != identity_before:
        raise RollbackExecutionError("活动数据库在核验期间被替换")
    snapshot["file_identity"] = identity_after
    expected_snapshot = pointer.get("database_activation_snapshot")
    if not isinstance(expected_snapshot, dict):
        raise RollbackExecutionError("活动运行指针缺少数据库基线")
    _assert_snapshot_matches(
        snapshot,
        expected_snapshot,
        label="活动数据库",
        include_logical=require_database_baseline,
    )
    if snapshot["revision"] != pointer.get("code_revision"):
        raise RollbackExecutionError("活动代码与数据库 Alembic revision 不一致")
    service = pointer.get("service") or {}
    if (
        int(service.get("workers") or 0) != 1
        or str(service.get("environment") or "") != "production"
        or not (1 <= int(service.get("port") or 0) <= 65535)
    ):
        raise RollbackExecutionError("活动运行指针不符合 production 单 worker 契约")
    overrides = pointer.get("environment_overrides") or {}
    expected_overrides = _safe_environment_overrides(project_root, database)
    if overrides != expected_overrides:
        raise RollbackExecutionError("活动运行指针的数据路径环境覆盖不一致")
    return {
        "application_root": str(runtime_dir),
        "runtime_dir": str(runtime_dir),
        "database_path": str(database),
        "python_path": str(python_path),
        "bind_host": str(service.get("bind_host") or ""),
        "port": int(service.get("port") or 0),
        "workers": int(service.get("workers") or 0),
        "environment": str(service.get("environment") or ""),
        "production_transport": str(
            service.get("production_transport") or ""
        ),
        "health_url": str(service.get("local_health_url") or ""),
        "external_health_url": str(service.get("health_url") or ""),
        "browser_url": str(service.get("browser_url") or ""),
        "code_sha": code_sha,
        "code_revision": code_revision,
        "runtime_kind": runtime_kind,
        "environment_overrides": pointer.get("environment_overrides") or {},
        "pointer_version": int(pointer.get("pointer_version") or 0),
        "runtime_id": str(pointer.get("runtime_id") or ""),
        "service": service,
    }


def resolve_validation_runtime(
    *,
    ticket_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Resolve the signed target for loopback warmup before pointer activation."""
    project_root = project_root.resolve()
    ticket, plan, _ = _load_ticket(
        ticket_path,
        project_root,
        allow_expired=True,
    )
    if _path_entry_exists(
        Path(str(plan["active_pointer_path"])).resolve()
    ):
        raise RollbackExecutionError("loopback 预热必须在活动指针切换前执行")
    if _path_entry_exists(
        Path(str(plan["production_exposure_intent_path"])).resolve()
    ) or _path_entry_exists(
        Path(str(plan["production_exposure_path"])).resolve()
    ):
        raise RollbackExecutionError("正式网络开放后不得再次执行 loopback 预热")
    if _path_entry_exists(
        Path(str(plan["validation_event_path"])).resolve()
    ):
        raise RollbackExecutionError("该票据已经完成 loopback 预热验证")
    _revalidate_plan(plan, project_root)
    validation_overrides = _verify_validation_environment(plan)
    target = plan["target_runtime"]
    resolved = _verify_pointer_payload(
        target,
        project_root=project_root,
        require_database_baseline=True,
        require_activation_state=False,
    )
    validation = plan.get("validation_service") or {}
    if (
        validation.get("bind_host") != "127.0.0.1"
        or int(validation.get("workers") or 0) != 1
    ):
        raise RollbackExecutionError("loopback 预热服务配置无效")
    return {
        "ok": True,
        "validation_mode": True,
        "ticket_id": ticket["ticket_id"],
        "application_root": resolved["application_root"],
        "runtime_dir": resolved["runtime_dir"],
        "database_path": resolved["database_path"],
        "python_path": resolved["python_path"],
        "bind_host": "127.0.0.1",
        "port": int(validation["port"]),
        "workers": 1,
        "environment": resolved["environment"],
        "production_transport": resolved["production_transport"],
        "health_url": str(validation["local_health_url"]),
        "external_health_url": str(validation["local_health_url"]),
        "browser_url": str(validation["local_health_url"]),
        "code_sha": resolved["code_sha"],
        "code_revision": resolved["code_revision"],
        "runtime_kind": resolved["runtime_kind"],
        "environment_overrides": validation_overrides,
        "runtime_id": resolved["runtime_id"],
        "service": validation,
    }


def resolve_active_runtime(
    *,
    pointer_path: Path,
    project_root: Path = PROJECT_ROOT,
    allow_pre_exposure: bool = False,
) -> dict[str, Any]:
    """Resolve and verify the signed pointer, or the implicit current runtime."""
    project_root = project_root.resolve()
    pointer_path = _assert_allowed_pointer_path(pointer_path, project_root)
    if _path_entry_exists(pointer_path):
        pointer = _verify_signed(
            pointer_path,
            label="活动运行指针",
            purpose=POINTER_PURPOSE,
            project_root=project_root,
        )
        result = _verify_pointer_payload(
            pointer,
            project_root=project_root,
            require_database_baseline=False,
            allow_pre_exposure=allow_pre_exposure,
        )
        return {
            "ok": True,
            "pointer_present": True,
            "pointer_path": str(pointer_path),
            **result,
        }
    _assert_no_outstanding_activations(project_root)
    state = _checkout_state(project_root)
    _assert_formal_checkout(
        project_root,
        expected_sha=state["head"],
        expected_state=state,
    )
    service = _runtime_service_config(project_root)
    database = Path(str(service["database_path"])).resolve()
    expected_database = (
        project_root / "data" / "carton_erp.sqlite3"
    ).resolve()
    if database != expected_database:
        raise RollbackExecutionError(
            "无活动指针时只允许使用正式 checkout 的固定正式数据库"
        )
    latest_pointer_path = (
        project_root
        / "data"
        / "release_state"
        / "latest_completed_release.json"
    )
    try:
        latest_pointer, completed_release, _ = (
            rollback_runtime._verify_release_evidence(
                latest_pointer_path,
                project_root,
            )
        )
    except rollback_runtime.CompatibilityError as error:
        raise RollbackExecutionError(
            f"无活动指针时缺少有效的最近正式发布证据：{error}"
        ) from error
    if (
        latest_pointer.get("code_sha") != state["head"]
        or completed_release.get("code_sha") != state["head"]
        or completed_release.get("completed_runtime_config")
        != runtime_config_fingerprint(project_root)
        or Path(
            str((completed_release.get("source") or {}).get("path") or "")
        ).resolve()
        != expected_database
    ):
        raise RollbackExecutionError(
            "无活动指针时正式 checkout、配置或数据库路径不匹配最近发布证据"
        )
    snapshot = release_erp.inspect_database(database)
    release_erp.assert_healthy(snapshot, label="隐式正式数据库")
    release_erp.assert_business_smoke(snapshot, label="隐式正式数据库")
    revision = release_erp.code_revision(project_root)
    if snapshot["revision"] != revision:
        raise RollbackExecutionError("隐式正式代码与数据库 revision 不一致")
    return {
        "ok": True,
        "pointer_present": False,
        "pointer_path": str(pointer_path),
        "application_root": str(project_root),
        "runtime_dir": str(project_root),
        "database_path": str(database),
        "python_path": str(Path(sys.executable).resolve()),
        "bind_host": service["bind_host"],
        "port": service["port"],
        "workers": service["workers"],
        "environment": service["environment"],
        "production_transport": service["production_transport"],
        "health_url": service["local_health_url"],
        "external_health_url": service["health_url"],
        "browser_url": service["browser_url"],
        "code_sha": state["head"],
        "code_revision": revision,
        "runtime_kind": "formal_checkout",
        "environment_overrides": _safe_environment_overrides(
            project_root,
            database,
        ),
        "pointer_version": 0,
        "runtime_id": f"implicit-{state['head'][:12]}",
        "service": service,
    }


def _load_compatibility_report(
    report_path: Path,
    *,
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rollback_runtime.verify_compatibility(
        report_path=report_path,
        project_root=project_root,
    )
    report = _verify_signed(
        report_path,
        label="阶段1兼容报告",
        purpose=rollback_runtime.COMPATIBILITY_REPORT_PURPOSE,
        project_root=project_root,
    )
    if (
        report.get("status") != "compatibility_ready"
        or report.get("execution_eligible") is not False
        or report.get("mode") not in {"full_rollback", "code_only"}
    ):
        raise RollbackExecutionError("阶段1兼容报告状态或模式不受支持")
    if not str(report.get("evidence_nonce") or ""):
        raise RollbackExecutionError("阶段1兼容报告缺少随机证据编号")
    if datetime.now(timezone.utc) >= _parse_utc(
        report.get("expires_at"),
        label="阶段1兼容报告",
    ):
        raise RollbackExecutionError("阶段1兼容报告已过期，必须重新隔离演练")
    release_plan_path = Path(str(report.get("plan_path") or "")).resolve()
    release_plan = _verify_signed(
        release_plan_path,
        label="已完成发布报告",
        purpose=rollback_runtime.PLAN_PURPOSE,
        project_root=project_root,
    )
    return report, release_plan


def prepare_switch(
    *,
    compatibility_report_path: Path,
    active_pointer_path: Path,
    output_root: Path,
    plan_path: Path,
    project_root: Path = PROJECT_ROOT,
    expires_minutes: int = DEFAULT_PLAN_TTL_MINUTES,
) -> dict[str, Any]:
    """Prepare signed switch evidence without stopping or switching ERP."""
    project_root = project_root.resolve()
    if not (5 <= expires_minutes <= 120):
        raise RollbackExecutionError("阶段2计划有效期必须为 5 到 120 分钟")
    output_root = _require_output_root(output_root, project_root)
    plan_path = plan_path.resolve()
    if not _within(plan_path, output_root):
        raise RollbackExecutionError("阶段2计划必须位于本次全新输出目录内")
    pointer_path = _assert_allowed_pointer_path(active_pointer_path, project_root)
    if _path_entry_exists(pointer_path):
        raise RollbackExecutionError(
            "当前已经存在活动运行指针；阶段2最小闭环不允许链式再次回退"
        )
    _assert_no_outstanding_activations(project_root)

    report, release_plan = _load_compatibility_report(
        compatibility_report_path.resolve(),
        project_root=project_root,
    )
    release_sha = str(report.get("release_code_sha") or "")
    previous_sha = str(report.get("previous_code_sha") or "")
    previous_revision = str(report.get("previous_code_revision") or "")
    checkout = _assert_formal_checkout(
        project_root,
        expected_sha=release_sha,
    )
    prepared_runtime_config = runtime_config_fingerprint(project_root)
    if prepared_runtime_config != release_plan.get("completed_runtime_config"):
        raise RollbackExecutionError(
            "当前运行配置或 Python 依赖与已完成发布报告不一致"
        )
    controller_manifest = _controller_manifest(project_root)
    service = _runtime_service_config(project_root)
    stage1_validation = report.get("runtime_environment") or {}
    validation_port = int(stage1_validation.get("port") or 0)
    if (
        stage1_validation.get("bind_host") != "127.0.0.1"
        or int(stage1_validation.get("workers") or 0) != 1
        or not (1024 <= validation_port <= 65535)
        or validation_port in {8000, int(service["port"])}
    ):
        raise RollbackExecutionError("阶段1隔离监听配置不能用于阶段2维护验证")
    validation_service = {
        "bind_host": "127.0.0.1",
        "port": validation_port,
        "workers": 1,
        "local_health_url": (
            f"http://127.0.0.1:{validation_port}/api/health"
        ),
        "network_exposure": "loopback_only_before_production_open",
    }
    formal_database = _assert_plain_path(
        Path(str(report.get("formal_database") or "")),
        label="阶段1正式数据库路径",
    )
    if Path(str(service.get("database_path") or "")).resolve() != formal_database:
        raise RollbackExecutionError("正式运行配置数据库与阶段1证据不一致")
    formal_before = _database_snapshot(formal_database, label="当前正式数据库")
    current_revision = release_erp.code_revision(project_root)
    if formal_before["revision"] != current_revision:
        raise RollbackExecutionError("当前正式代码与数据库 Alembic revision 不一致")
    expected_formal = report.get("formal_database_logical_fingerprint")
    if formal_before["logical_fingerprint"] != expected_formal:
        raise RollbackExecutionError("当前正式数据库已偏离阶段1兼容证据")

    plan_id = secrets.token_hex(16)
    database_root = output_root / "databases"
    site_backup_path = database_root / "carton_erp_rollback_site_backup.sqlite3"
    release_erp.create_sqlite_copy(
        formal_database,
        site_backup_path,
    )
    site_backup = _database_snapshot(
        site_backup_path,
        label="回退前现场备份",
    )
    _assert_snapshot_matches(
        site_backup,
        formal_before,
        label="回退前现场备份",
        include_path=False,
    )

    mode = str(report["mode"])
    if mode == "code_only":
        candidate_source = site_backup_path
        candidate_name = "carton_erp_code_only_candidate.sqlite3"
        data_loss_window = None
    else:
        release_backup = release_plan.get("backup") or {}
        candidate_source = _assert_plain_path(
            Path(str(release_backup.get("path") or "")),
            label="更新前备份",
        )
        expected_hash = str(release_backup.get("sha256") or "")
        if (
            not candidate_source.is_file()
            or not expected_hash
            or sha256_file(candidate_source) != expected_hash
        ):
            raise RollbackExecutionError("更新前备份缺失或哈希不匹配")
        candidate_name = "carton_erp_full_rollback_candidate.sqlite3"
        backup_time = _parse_utc(
            release_plan.get("prepared_at"),
            label="签名发布计划 Prepare",
        )
        data_loss_window = {
            "from": backup_time.isoformat(timespec="seconds"),
            "to": _utc_now(),
            "message": (
                "完整回退候选不含此时间段内的新业务数据；起点采用"
                "签名发布计划时间，可能早于实际备份，按保守口径提示"
            ),
        }
    candidate_database = database_root / candidate_name
    release_erp.create_sqlite_copy(
        candidate_source,
        candidate_database,
    )
    candidate_snapshot = _database_snapshot(
        candidate_database,
        label="回退候选数据库",
    )
    if candidate_snapshot["revision"] != previous_revision:
        raise RollbackExecutionError(
            "回退候选数据库 revision 与上一版本代码不一致"
        )
    validation_environment = _prepare_validation_environment(
        output_root,
        candidate_database,
    )

    stage1_runtime = _assert_plain_path(
        Path(str(report.get("runtime_dir") or "")),
        label="阶段1旧代码目录",
    )
    activation_runtime = output_root / "activation" / "runtime"
    immutable_manifest = _prepare_activation_runtime(
        stage1_runtime,
        activation_runtime,
        project_root,
    )
    python_environment = (
        _read_json(
            Path(str(report.get("runtime_manifest_path") or "")).resolve(),
            label="阶段1运行清单",
        ).get("python_environment")
        or {}
    )
    python_path = Path(sys.executable).resolve()
    activation_state_root = (
        project_root
        / "data"
        / "release_state"
        / "rollback_activations"
    ).resolve()
    activation_claim_path = (
        activation_state_root / f"{plan_id}.claim.json"
    )
    revocation_path = activation_state_root / f"{plan_id}.revoked.json"
    validation_event_path = output_root / "loopback_validation_event.json"
    auto_restore_permit_path = output_root / "auto_restore_permit.json"
    auto_restore_permit_consumed_path = (
        output_root / "auto_restore_permit.consumed.json"
    )
    production_exposure_intent_path = (
        output_root / "production_exposure_intent.json"
    )
    production_exposure_path = (
        activation_state_root / f"{plan_id}.exposed.json"
    )
    current_pointer = _pointer_payload(
        pointer_version=1,
        runtime_id=f"current-{release_sha[:12]}-{plan_id[:8]}",
        runtime_kind="formal_checkout",
        runtime_dir=project_root,
        code_sha=release_sha,
        code_revision=current_revision,
        database_path=formal_database,
        database_snapshot=formal_before,
        python_path=python_path,
        python_environment=python_environment,
        service=service,
        environment_overrides=_safe_environment_overrides(
            project_root,
            formal_database,
        ),
        immutable_manifest=None,
        checkout_state=checkout,
        controller_manifest=controller_manifest,
        previous_pointer_sha256=None,
        activation_plan_id=None,
        activation_claim_path=None,
        revocation_path=None,
        validation_event_path=None,
        auto_restore_permit_path=None,
        auto_restore_permit_consumed_path=None,
        production_exposure_intent_path=None,
        production_exposure_path=None,
    )
    target_pointer = _pointer_payload(
        pointer_version=2,
        runtime_id=f"rollback-{previous_sha[:12]}-{plan_id[:8]}",
        runtime_kind="archive_activation",
        runtime_dir=activation_runtime,
        code_sha=previous_sha,
        code_revision=previous_revision,
        database_path=candidate_database,
        database_snapshot=candidate_snapshot,
        python_path=python_path,
        python_environment=python_environment,
        service=service,
        environment_overrides=_safe_environment_overrides(
            project_root,
            candidate_database,
        ),
        immutable_manifest=immutable_manifest,
        checkout_state=checkout,
        controller_manifest=controller_manifest,
        previous_pointer_sha256=hashlib.sha256(
            _canonical_bytes(current_pointer)
        ).hexdigest(),
        activation_plan_id=plan_id,
        activation_claim_path=activation_claim_path,
        revocation_path=revocation_path,
        validation_event_path=validation_event_path,
        auto_restore_permit_path=auto_restore_permit_path,
        auto_restore_permit_consumed_path=(
            auto_restore_permit_consumed_path
        ),
        production_exposure_intent_path=production_exposure_intent_path,
        production_exposure_path=production_exposure_path,
    )

    approval_token = _new_token("ROLLBACK")
    database_token = _new_token("DATABASE") if mode == "full_rollback" else None
    prepared_at = datetime.now(timezone.utc)
    plan = {
        "schema_version": 1,
        "status": "awaiting_switch_approval",
        "execution_eligible": True,
        "plan_id": plan_id,
        "prepared_at": prepared_at.isoformat(timespec="seconds"),
        "expires_at": (
            prepared_at + timedelta(minutes=expires_minutes)
        ).isoformat(timespec="seconds"),
        "mode": mode,
        "mode_label": str(report.get("mode_label") or mode),
        "requires_database_approval": mode == "full_rollback",
        "data_loss_window": data_loss_window,
        "compatibility_report_path": str(compatibility_report_path.resolve()),
        "compatibility_report_sha256": sha256_file(
            compatibility_report_path.resolve()
        ),
        "compatibility_evidence_nonce": report["evidence_nonce"],
        "release_plan_path": str(Path(str(report["plan_path"])).resolve()),
        "release_plan_sha256": sha256_file(
            Path(str(report["plan_path"])).resolve()
        ),
        "formal_checkout": checkout,
        "prepared_runtime_config": prepared_runtime_config,
        "controller_manifest": controller_manifest,
        "service": service,
        "validation_service": validation_service,
        "validation_environment": validation_environment,
        "formal_database": formal_before,
        "rollback_site_backup": site_backup,
        "candidate_database": candidate_snapshot,
        "candidate_source_path": str(candidate_source),
        "candidate_source_sha256": sha256_file(candidate_source),
        "active_pointer_path": str(pointer_path),
        "active_pointer_before": None,
        "current_runtime": current_pointer,
        "target_runtime": target_pointer,
        "approval_token_digest": _token_digest(
            purpose="switch",
            plan_id=plan_id,
            token=approval_token,
        ),
        "database_approval_token_digest": (
            _token_digest(
                purpose="database",
                plan_id=plan_id,
                token=database_token or "",
            )
            if database_token
            else None
        ),
        "ticket_path": str((output_root / "execution_ticket.json").resolve()),
        "activation_claim_path": str(activation_claim_path),
        "revocation_path": str(revocation_path),
        "consumption_path": str(
            (
                project_root
                / "data"
                / "release_state"
                / "rollback_consumptions"
                / f"{plan_id}.json"
            ).resolve()
        ),
        "activation_event_path": str(
            (output_root / "activation_event.json").resolve()
        ),
        "validation_event_path": str(
            validation_event_path.resolve()
        ),
        "auto_restore_permit_path": str(
            auto_restore_permit_path.resolve()
        ),
        "auto_restore_permit_consumed_path": str(
            auto_restore_permit_consumed_path.resolve()
        ),
        "production_exposure_path": str(
            production_exposure_path.resolve()
        ),
        "production_exposure_intent_path": str(
            production_exposure_intent_path.resolve()
        ),
        "restoration_event_path": str(
            (output_root / "restoration_event.json").resolve()
        ),
        "result_path": str((output_root / "switch_result.json").resolve()),
        "plan_path": str(plan_path),
    }
    formal_after = _database_snapshot(formal_database, label="Prepare 后正式数据库")
    _assert_snapshot_matches(
        formal_after,
        formal_before,
        label="Prepare 期间正式数据库",
    )
    _assert_formal_checkout(
        project_root,
        expected_sha=release_sha,
        expected_state=checkout,
    )
    auto_restore_permit = {
        "schema_version": 1,
        "status": "pre_exposure_auto_restore_permitted",
        "created_at": _utc_now(),
        "plan_id": plan_id,
        "runtime_id": target_pointer["runtime_id"],
        "permit_path": str(auto_restore_permit_path.resolve()),
        "consumed_path": str(
            auto_restore_permit_consumed_path.resolve()
        ),
        "scope": (
            "仅在正式网络开放意向落盘前允许一次自动恢复当前运行目录"
        ),
    }
    _write_signed_atomic(
        auto_restore_permit_path,
        auto_restore_permit,
        project_root=project_root,
        purpose=AUTO_RESTORE_PERMIT_PURPOSE,
        replace=False,
    )
    _write_signed_atomic(
        plan_path,
        plan,
        project_root=project_root,
        purpose=PLAN_PURPOSE,
        replace=False,
    )
    return {
        "ok": True,
        "status": "awaiting_switch_approval",
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "mode": mode,
        "mode_label": plan["mode_label"],
        "approval_token": approval_token,
        "database_approval_token": database_token,
        "requires_database_approval": plan["requires_database_approval"],
        "data_loss_window": data_loss_window,
        "rollback_site_backup": site_backup,
        "candidate_database": candidate_snapshot,
        "target_runtime_dir": str(activation_runtime),
        "formal_database_unchanged": True,
    }


def _load_plan(
    plan_path: Path,
    project_root: Path,
    *,
    allow_expired: bool = False,
) -> dict[str, Any]:
    plan = _verify_signed(
        plan_path,
        label="阶段2回退计划",
        purpose=PLAN_PURPOSE,
        project_root=project_root,
    )
    if (
        int(plan.get("schema_version") or 0) != 1
        or plan.get("status") != "awaiting_switch_approval"
        or plan.get("execution_eligible") is not True
    ):
        raise RollbackExecutionError("阶段2回退计划状态或版本不可执行")
    if not allow_expired and datetime.now(timezone.utc) >= _parse_utc(
        plan.get("expires_at"),
        label="阶段2回退计划",
    ):
        raise RollbackExecutionError("阶段2回退计划已过期，必须重新 Prepare")
    if Path(str(plan.get("plan_path") or "")).resolve() != plan_path.resolve():
        raise RollbackExecutionError("阶段2回退计划路径绑定不一致")
    return plan


def _revalidate_plan(plan: dict[str, Any], project_root: Path) -> None:
    expected_sha = str((plan.get("formal_checkout") or {}).get("head") or "")
    _assert_formal_checkout(
        project_root,
        expected_sha=expected_sha,
        expected_state=plan.get("formal_checkout"),
    )
    if runtime_config_fingerprint(project_root) != plan.get(
        "prepared_runtime_config"
    ):
        raise RollbackExecutionError("Prepare 后运行配置或 Python 依赖已变化")
    if _controller_manifest(project_root) != plan.get("controller_manifest"):
        raise RollbackExecutionError("Prepare 后阶段2控制文件已变化")
    report_path = Path(str(plan.get("compatibility_report_path") or "")).resolve()
    if (
        not report_path.is_file()
        or sha256_file(report_path) != plan.get("compatibility_report_sha256")
    ):
        raise RollbackExecutionError("Prepare 后阶段1兼容报告已变化")
    report, _ = _load_compatibility_report(
        report_path,
        project_root=project_root,
    )
    if report.get("evidence_nonce") != plan.get("compatibility_evidence_nonce"):
        raise RollbackExecutionError("阶段1兼容证据编号与阶段2计划不一致")
    formal = _database_snapshot(
        Path(str((plan.get("formal_database") or {}).get("path") or "")),
        label="Apply 前正式数据库",
    )
    _assert_snapshot_matches(
        formal,
        plan["formal_database"],
        label="Prepare 后正式数据库",
    )
    for label, key in (
        ("回退前现场备份", "rollback_site_backup"),
        ("回退候选数据库", "candidate_database"),
    ):
        expected = plan.get(key)
        if not isinstance(expected, dict):
            raise RollbackExecutionError(f"阶段2计划缺少{label}")
        actual = _database_snapshot(
            Path(str(expected.get("path") or "")),
            label=label,
        )
        _assert_snapshot_matches(actual, expected, label=label)
    target = plan.get("target_runtime")
    if not isinstance(target, dict):
        raise RollbackExecutionError("阶段2计划缺少目标活动运行指针")
    current = plan.get("current_runtime")
    if not isinstance(current, dict) or target.get(
        "previous_pointer_sha256"
    ) != hashlib.sha256(_canonical_bytes(current)).hexdigest():
        raise RollbackExecutionError("目标活动指针未绑定切换前运行指针")
    expected_claim, expected_revocation = _activation_state_paths(
        str(plan.get("plan_id") or ""),
        project_root,
    )
    if (
        Path(str(plan.get("activation_claim_path") or "")).resolve()
        != expected_claim
        or Path(str(plan.get("revocation_path") or "")).resolve()
        != expected_revocation
        or Path(str(target.get("activation_claim_path") or "")).resolve()
        != expected_claim
        or Path(str(target.get("revocation_path") or "")).resolve()
        != expected_revocation
        or target.get("activation_plan_id") != plan.get("plan_id")
    ):
        raise RollbackExecutionError("阶段2持久激活状态路径与计划不一致")
    target_runtime_dir = Path(str(target.get("runtime_dir") or "")).resolve()
    target_run_root = target_runtime_dir.parent.parent
    expected_validation = (
        target_run_root / "loopback_validation_event.json"
    ).resolve()
    expected_auto_restore_permit = (
        target_run_root / "auto_restore_permit.json"
    ).resolve()
    expected_auto_restore_permit_consumed = (
        target_run_root / "auto_restore_permit.consumed.json"
    ).resolve()
    expected_exposure_intent = (
        target_run_root / "production_exposure_intent.json"
    ).resolve()
    expected_exposure = (
        expected_claim.parent / f"{plan['plan_id']}.exposed.json"
    ).resolve()
    if (
        Path(str(plan.get("validation_event_path") or "")).resolve()
        != expected_validation
        or Path(str(plan.get("auto_restore_permit_path") or "")).resolve()
        != expected_auto_restore_permit
        or Path(
            str(plan.get("auto_restore_permit_consumed_path") or "")
        ).resolve()
        != expected_auto_restore_permit_consumed
        or Path(str(plan.get("production_exposure_path") or "")).resolve()
        != expected_exposure
        or Path(
            str(plan.get("production_exposure_intent_path") or "")
        ).resolve()
        != expected_exposure_intent
        or Path(str(target.get("validation_event_path") or "")).resolve()
        != expected_validation
        or Path(
            str(target.get("auto_restore_permit_path") or "")
        ).resolve()
        != expected_auto_restore_permit
        or Path(
            str(target.get("auto_restore_permit_consumed_path") or "")
        ).resolve()
        != expected_auto_restore_permit_consumed
        or Path(str(target.get("production_exposure_path") or "")).resolve()
        != expected_exposure
        or Path(
            str(target.get("production_exposure_intent_path") or "")
        ).resolve()
        != expected_exposure_intent
    ):
        raise RollbackExecutionError("阶段2 loopback 验证或正式开放证据路径不一致")
    _verify_validation_environment(plan)
    _verify_pointer_payload(
        target,
        project_root=project_root,
        require_database_baseline=True,
        require_activation_state=False,
    )
    _verify_auto_restore_permit(
        expected_auto_restore_permit,
        plan_id=str(plan.get("plan_id") or ""),
        runtime_id=str(target.get("runtime_id") or ""),
        permit_path=expected_auto_restore_permit,
        consumed_path=expected_auto_restore_permit_consumed,
        project_root=project_root,
        label="预开放自动恢复许可",
    )
    if _path_entry_exists(expected_auto_restore_permit_consumed):
        raise RollbackExecutionError(
            "预开放自动恢复许可已经消费，禁止重放阶段2计划"
        )
    for state_path, label in (
        (expected_claim, "激活声明"),
        (expected_revocation, "撤销记录"),
        (expected_exposure_intent, "正式开放意向"),
        (expected_exposure, "正式开放承诺"),
    ):
        if _path_entry_exists(state_path.resolve()):
            raise RollbackExecutionError(f"该阶段2计划已经产生{label}，禁止重放")
    pointer_path = Path(str(plan.get("active_pointer_path") or "")).resolve()
    _assert_allowed_pointer_path(pointer_path, project_root)
    if _path_entry_exists(pointer_path):
        raise RollbackExecutionError("Prepare 后活动运行指针被创建或修改")


def authorize_switch(
    *,
    plan_path: Path,
    approval_token: str,
    database_approval_token: str | None = None,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Consume the plan-bound token once, before the service is stopped."""
    project_root = project_root.resolve()
    plan_path = plan_path.resolve()
    plan = _load_plan(plan_path, project_root)
    plan_id = str(plan.get("plan_id") or "")
    actual = _token_digest(
        purpose="switch",
        plan_id=plan_id,
        token=approval_token,
    )
    if not secrets.compare_digest(
        actual,
        str(plan.get("approval_token_digest") or ""),
    ):
        raise RollbackExecutionError("一次授权口令不匹配，ERP 尚未停服")
    if plan.get("requires_database_approval"):
        database_actual = _token_digest(
            purpose="database",
            plan_id=plan_id,
            token=database_approval_token or "",
        )
        if not secrets.compare_digest(
            database_actual,
            str(plan.get("database_approval_token_digest") or ""),
        ):
            raise RollbackExecutionError(
                "完整回退必须提供第二个数据库授权口令，ERP 尚未停服"
            )
    elif database_approval_token:
        raise RollbackExecutionError("仅代码回退不接受数据库恢复授权口令")
    _revalidate_plan(plan, project_root)
    ticket_path = Path(str(plan.get("ticket_path") or "")).resolve()
    consumption_path = Path(
        str(plan.get("consumption_path") or "")
    ).resolve()
    expected_consumption_parent = (
        project_root / "data" / "release_state" / "rollback_consumptions"
    ).resolve()
    if (
        consumption_path.parent != expected_consumption_parent
        or consumption_path.name != f"{plan_id}.json"
    ):
        raise RollbackExecutionError("一次授权消费记录路径不安全")
    if _path_entry_exists(consumption_path) or _path_entry_exists(ticket_path):
        raise RollbackExecutionError("一次授权口令已经消费，禁止重放")
    consumption = {
        "schema_version": 1,
        "status": "authorization_consumed",
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "plan_sha256": sha256_file(plan_path),
        "consumed_at": _utc_now(),
        "mode": plan["mode"],
        "switch_token_digest": plan["approval_token_digest"],
        "database_token_digest": plan.get("database_approval_token_digest"),
    }
    _write_signed_atomic(
        consumption_path,
        consumption,
        project_root=project_root,
        purpose=CONSUMPTION_PURPOSE,
        replace=False,
    )
    ticket = {
        "schema_version": 1,
        "status": "authorized_pending_service_stop",
        "ticket_id": secrets.token_hex(24),
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "plan_sha256": sha256_file(plan_path),
        "authorized_at": _utc_now(),
        "expires_at": plan["expires_at"],
        "mode": plan["mode"],
        "consumption_path": str(consumption_path),
        "consumption_sha256": sha256_file(consumption_path),
        "current_runtime": plan["current_runtime"],
        "target_runtime": plan["target_runtime"],
        "service": plan["service"],
        "validation_service": plan["validation_service"],
    }
    _write_signed_atomic(
        ticket_path,
        ticket,
        project_root=project_root,
        purpose=TICKET_PURPOSE,
        replace=False,
    )
    return {
        "ok": True,
        "status": ticket["status"],
        "ticket_path": str(ticket_path),
        "plan_path": str(plan_path),
        "mode": plan["mode"],
        "current_runtime": plan["current_runtime"],
        "target_runtime": plan["target_runtime"],
        "service": plan["service"],
        "validation_service": plan["validation_service"],
    }


def _load_ticket(
    ticket_path: Path,
    project_root: Path,
    *,
    allow_expired: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    ticket_path = ticket_path.resolve()
    ticket = _verify_signed(
        ticket_path,
        label="阶段2执行票据",
        purpose=TICKET_PURPOSE,
        project_root=project_root,
    )
    if (
        int(ticket.get("schema_version") or 0) != 1
        or ticket.get("status") != "authorized_pending_service_stop"
    ):
        raise RollbackExecutionError("阶段2执行票据状态或版本不受支持")
    if not allow_expired and datetime.now(timezone.utc) >= _parse_utc(
        ticket.get("expires_at"),
        label="阶段2执行票据",
    ):
        raise RollbackExecutionError("阶段2执行票据已过期")
    consumption_path = Path(
        str(ticket.get("consumption_path") or "")
    ).resolve()
    consumption = _verify_signed(
        consumption_path,
        label="一次授权消费记录",
        purpose=CONSUMPTION_PURPOSE,
        project_root=project_root,
    )
    if (
        consumption.get("plan_id") != ticket.get("plan_id")
        or not consumption_path.is_file()
        or sha256_file(consumption_path) != ticket.get("consumption_sha256")
    ):
        raise RollbackExecutionError("一次授权消费记录与执行票据不一致")
    plan_path = Path(str(ticket.get("plan_path") or "")).resolve()
    if (
        not plan_path.is_file()
        or sha256_file(plan_path) != ticket.get("plan_sha256")
    ):
        raise RollbackExecutionError("阶段2执行票据绑定的计划已变化")
    plan = _load_plan(
        plan_path,
        project_root,
        allow_expired=allow_expired,
    )
    if ticket.get("plan_id") != plan.get("plan_id"):
        raise RollbackExecutionError("阶段2执行票据与计划编号不一致")
    return ticket, plan, plan_path


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _assert_port_free(port: int) -> None:
    if not _port_is_free(port):
        raise RollbackExecutionError(
            f"ERP 端口 {port} 仍有监听，禁止切换活动运行指针"
        )


def _windows_process_exists(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(process_id, 0)
        except (OSError, ProcessLookupError):
            return False
        return True
    script = (
        "$processId=[int]$args[0];"
        "$item=Get-CimInstance Win32_Process "
        "-Filter ('ProcessId = ' + $processId) -ErrorAction SilentlyContinue;"
        "if($null -eq $item){'false'}else{'true'}"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(process_id),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    if result.returncode != 0:
        raise RollbackExecutionError("无法复核停服进程是否仍存在")
    return result.stdout.strip().lower() == "true"


def _windows_listener_process_ids(port: int) -> list[int]:
    if os.name != "nt":
        return [] if _port_is_free(port) else [-1]
    script = (
        "$portNumber=[int]$args[0];"
        "@(Get-NetTCPConnection -State Listen -LocalPort $portNumber "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty "
        "OwningProcess -Unique | Sort-Object) | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(port),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    if result.returncode != 0:
        raise RollbackExecutionError("无法复核正式端口监听进程")
    raw = result.stdout.strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RollbackExecutionError("正式端口监听进程输出格式错误") from error
    values = payload if isinstance(payload, list) else [payload]
    try:
        return sorted({int(value) for value in values if int(value) > 0})
    except (TypeError, ValueError) as error:
        raise RollbackExecutionError("正式端口监听进程编号无效") from error


def _write_revocation(
    *,
    ticket: dict[str, Any],
    plan: dict[str, Any],
    pointer: dict[str, Any],
    reason: str,
    project_root: Path,
) -> dict[str, Any]:
    revocation_path = Path(str(plan.get("revocation_path") or "")).resolve()
    expected_claim, expected_revocation = _activation_state_paths(
        str(plan.get("plan_id") or ""),
        project_root,
    )
    claim_path = Path(
        str(plan.get("activation_claim_path") or "")
    ).resolve()
    if (
        claim_path != expected_claim
        or revocation_path != expected_revocation
        or revocation_path
        != Path(str(pointer.get("revocation_path") or "")).resolve()
    ):
        raise RollbackExecutionError("活动运行撤销路径与计划不一致")
    revocation = {
        "schema_version": 1,
        "status": "activation_revoked",
        "revoked_at": _utc_now(),
        "plan_id": plan["plan_id"],
        "ticket_id": ticket["ticket_id"],
        "runtime_id": pointer["runtime_id"],
        "activation_claim_path": str(claim_path),
        "activation_claim_sha256": sha256_file(claim_path),
        "target_pointer_sha256": hashlib.sha256(
            _canonical_bytes(_unsigned_payload(pointer))
        ).hexdigest(),
        "reason": reason[:1000],
    }
    return _write_signed_atomic(
        revocation_path,
        revocation,
        project_root=project_root,
        purpose=REVOCATION_PURPOSE,
        replace=False,
    )


def _verify_revocation_record(
    *,
    ticket: dict[str, Any],
    plan: dict[str, Any],
    project_root: Path,
) -> dict[str, Any]:
    revocation_path = Path(str(plan.get("revocation_path") or "")).resolve()
    revocation = _verify_signed(
        revocation_path,
        label="活动运行指针撤销记录",
        purpose=REVOCATION_PURPOSE,
        project_root=project_root,
    )
    if (
        revocation.get("plan_id") != plan.get("plan_id")
        or revocation.get("ticket_id") != ticket.get("ticket_id")
        or revocation.get("runtime_id")
        != (plan.get("target_runtime") or {}).get("runtime_id")
    ):
        raise RollbackExecutionError("活动运行指针撤销记录与票据不一致")
    return revocation


def revoke_switch(
    *,
    ticket_path: Path,
    reason: str,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Persistently revoke the target pointer without killing any process."""
    project_root = project_root.resolve()
    ticket, plan, _ = _load_ticket(
        ticket_path,
        project_root,
        allow_expired=True,
    )
    event = _verify_signed(
        Path(str(plan.get("activation_event_path") or "")).resolve(),
        label="活动指针切换事件",
        purpose=EVENT_PURPOSE,
        project_root=project_root,
    )
    if event.get("ticket_id") != ticket.get("ticket_id"):
        raise RollbackExecutionError("活动指针切换事件与票据不一致")
    _assert_auto_restore_allowed(plan, project_root=project_root)
    pointer_path = Path(str(plan.get("active_pointer_path") or "")).resolve()
    pointer = _verify_signed(
        pointer_path,
        label="待人工处理的活动运行指针",
        purpose=POINTER_PURPOSE,
        project_root=project_root,
    )
    if pointer.get("runtime_id") != plan["target_runtime"]["runtime_id"]:
        raise RollbackExecutionError("待撤销活动运行指针不是本计划目标")
    _verify_archive_activation_state(
        pointer,
        project_root=project_root,
        allow_pre_exposure=True,
    )
    revocation_path = Path(str(plan.get("revocation_path") or "")).resolve()
    if not _path_entry_exists(revocation_path):
        _write_revocation(
            ticket=ticket,
            plan=plan,
            pointer=pointer,
            reason=reason,
            project_root=project_root,
        )
    _verify_revocation_record(
        ticket=ticket,
        plan=plan,
        project_root=project_root,
    )
    return {
        "ok": False,
        "status": "target_runtime_revoked_manual_recovery_required",
        "ticket_path": str(ticket_path.resolve()),
        "active_pointer_path": str(pointer_path),
        "active_pointer_present": _path_entry_exists(pointer_path),
        "revocation_path": str(revocation_path),
    }


def activate_switch(
    *,
    ticket_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Atomically activate the target pointer after the wrapper stopped ERP."""
    project_root = project_root.resolve()
    ticket, plan, _ = _load_ticket(ticket_path, project_root)
    event_path = Path(str(plan.get("activation_event_path") or "")).resolve()
    restoration_path = Path(
        str(plan.get("restoration_event_path") or "")
    ).resolve()
    result_path = Path(str(plan.get("result_path") or "")).resolve()
    claim_path = Path(str(plan.get("activation_claim_path") or "")).resolve()
    revocation_path = Path(str(plan.get("revocation_path") or "")).resolve()
    if any(
        _path_entry_exists(path)
        for path in (
            event_path,
            restoration_path,
            result_path,
            claim_path,
            revocation_path,
        )
    ):
        raise RollbackExecutionError("该执行票据已经执行或撤销，禁止再次切换")
    _verify_validation_event(
        ticket=ticket,
        plan=plan,
        project_root=project_root,
    )
    _revalidate_plan(plan, project_root)
    _assert_port_free(
        int((plan.get("validation_service") or {}).get("port") or 0)
    )
    _assert_port_free(int((plan.get("service") or {}).get("port") or 0))
    pointer_path = Path(str(plan["active_pointer_path"])).resolve()
    target = dict(plan["target_runtime"])
    target["updated_at"] = _utc_now()
    claim = {
        "schema_version": 1,
        "status": "activation_claimed",
        "claimed_at": _utc_now(),
        "plan_id": plan["plan_id"],
        "ticket_id": ticket["ticket_id"],
        "runtime_id": target["runtime_id"],
        "plan_path": ticket["plan_path"],
        "plan_sha256": ticket["plan_sha256"],
        "target_pointer_sha256": hashlib.sha256(
            _canonical_bytes(target)
        ).hexdigest(),
    }
    _write_signed_atomic(
        claim_path,
        claim,
        project_root=project_root,
        purpose=ACTIVATION_CLAIM_PURPOSE,
        replace=False,
    )
    try:
        written = _write_signed_atomic(
            pointer_path,
            target,
            project_root=project_root,
            purpose=POINTER_PURPOSE,
            replace=False,
        )
        _verify_pointer_payload(
            written,
            project_root=project_root,
            require_database_baseline=True,
            allow_pre_exposure=True,
        )
        event = {
            "schema_version": 1,
            "event": "target_pointer_activated",
            "event_at": _utc_now(),
            "ticket_id": ticket["ticket_id"],
            "plan_id": plan["plan_id"],
            "active_pointer_path": str(pointer_path),
            "active_pointer_sha256": sha256_file(pointer_path),
            "target_runtime_id": target["runtime_id"],
        }
        _write_signed_atomic(
            event_path,
            event,
            project_root=project_root,
            purpose=EVENT_PURPOSE,
            replace=False,
        )
    except Exception:
        if _path_entry_exists(pointer_path):
            published = _verify_signed(
                pointer_path,
                label="激活失败现场活动指针",
                purpose=POINTER_PURPOSE,
                project_root=project_root,
            )
            if published.get("runtime_id") == target.get("runtime_id"):
                if not _path_entry_exists(revocation_path):
                    _write_revocation(
                        ticket=ticket,
                        plan=plan,
                        pointer=published,
                        reason="目标活动指针发布或核验失败",
                        project_root=project_root,
                    )
                archived = event_path.parent / "failed_target_pointer.json"
                try:
                    _write_json_exclusive(archived, published)
                finally:
                    pointer_path.unlink(missing_ok=True)
                    _fsync_directory(pointer_path.parent)
        elif _path_entry_exists(claim_path) and not _path_entry_exists(
            revocation_path
        ):
            _write_revocation(
                ticket=ticket,
                plan=plan,
                pointer=target,
                reason="目标活动指针尚未发布即失败",
                project_root=project_root,
            )
        raise
    return {
        "ok": True,
        "status": "target_pointer_activated",
        "ticket_path": str(ticket_path.resolve()),
        "active_pointer_path": str(pointer_path),
        "target_runtime": plan["target_runtime"],
    }


def _read_health(url: str, timeout_seconds: int = 4) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or body != {"ok": True}:
                raise RollbackExecutionError(
                    f"ERP 健康响应异常：HTTP {response.status} {body}"
                )
            return {"status_code": response.status, "body": body, "url": url}
    except RollbackExecutionError:
        raise
    except (
        urllib.error.URLError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise RollbackExecutionError(f"ERP 健康检查失败：{error}") from error


def _verify_validation_event(
    *,
    ticket: dict[str, Any],
    plan: dict[str, Any],
    project_root: Path,
) -> dict[str, Any]:
    validation_path = Path(str(plan["validation_event_path"])).resolve()
    validation = _verify_signed(
        validation_path,
        label="loopback 验证事件",
        purpose=VALIDATION_PURPOSE,
        project_root=project_root,
    )
    target = plan.get("target_runtime") or {}
    if (
        validation.get("status") != "loopback_validation_completed"
        or validation.get("ticket_id") != ticket.get("ticket_id")
        or validation.get("plan_id") != plan.get("plan_id")
        or validation.get("runtime_id") != target.get("runtime_id")
        or validation.get("target_runtime_template_sha256")
        != hashlib.sha256(_canonical_bytes(target)).hexdigest()
        or validation.get("validation_service")
        != plan.get("validation_service")
        or validation.get("network_exposure") != "loopback_only"
        or validation.get("health", {}).get("body") != {"ok": True}
    ):
        raise RollbackExecutionError("loopback 验证事件与票据或目标运行目录不一致")
    return validation


def verify_loopback_switch(
    *,
    ticket_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Verify the target on a loopback-only maintenance port before exposure."""
    project_root = project_root.resolve()
    ticket, plan, _ = _load_ticket(
        ticket_path,
        project_root,
        allow_expired=True,
    )
    intent_path = Path(
        str(plan["production_exposure_intent_path"])
    ).resolve()
    exposure_path = Path(str(plan["production_exposure_path"])).resolve()
    if _path_entry_exists(intent_path) or _path_entry_exists(exposure_path):
        raise RollbackExecutionError("正式网络已经开放，不能重复 loopback 验证")
    validation_path = Path(str(plan["validation_event_path"])).resolve()
    if _path_entry_exists(validation_path):
        raise RollbackExecutionError("loopback 验证已经完成，禁止重放")
    if _path_entry_exists(
        Path(str(plan["active_pointer_path"])).resolve()
    ):
        raise RollbackExecutionError("loopback 验证必须在活动指针切换前完成")
    _revalidate_plan(plan, project_root)
    target = plan["target_runtime"]
    resolved = _verify_pointer_payload(
        target,
        project_root=project_root,
        require_database_baseline=True,
        require_activation_state=False,
    )
    validation_service = plan.get("validation_service") or {}
    validation_url = str(
        validation_service.get("local_health_url") or ""
    )
    health = _read_health(validation_url)
    database_after = _database_snapshot(
        Path(str(resolved["database_path"])),
        label="loopback 验证后候选数据库",
    )
    _assert_snapshot_matches(
        database_after,
        plan["candidate_database"],
        label="loopback 验证期间候选数据库",
    )
    _revalidate_plan(plan, project_root)
    event = {
        "schema_version": 1,
        "status": "loopback_validation_completed",
        "validated_at": _utc_now(),
        "plan_id": plan["plan_id"],
        "ticket_id": ticket["ticket_id"],
        "runtime_id": target["runtime_id"],
        "target_runtime_template_sha256": hashlib.sha256(
            _canonical_bytes(target)
        ).hexdigest(),
        "validation_service": validation_service,
        "current_production_service_remained_running": True,
        "health": health,
        "candidate_database": database_after,
        "network_exposure": "loopback_only",
    }
    _write_signed_atomic(
        validation_path,
        event,
        project_root=project_root,
        purpose=VALIDATION_PURPOSE,
        replace=False,
    )
    return {
        "ok": True,
        "status": event["status"],
        "validation_event_path": str(validation_path),
        "health": health,
        "candidate_database_unchanged": True,
    }


def commit_production_exposure(
    *,
    ticket_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Commit the no-auto-restore boundary before binding the LAN port."""
    project_root = project_root.resolve()
    ticket, plan, _ = _load_ticket(
        ticket_path,
        project_root,
        allow_expired=True,
    )
    validation_path = Path(str(plan["validation_event_path"])).resolve()
    validation = _verify_validation_event(
        ticket=ticket,
        plan=plan,
        project_root=project_root,
    )
    intent_path = Path(
        str(plan["production_exposure_intent_path"])
    ).resolve()
    exposure_path = Path(str(plan["production_exposure_path"])).resolve()
    if _path_entry_exists(intent_path) or _path_entry_exists(exposure_path):
        raise RollbackExecutionError("正式网络开放承诺已经存在，禁止重放")
    validation_service = plan.get("validation_service") or {}
    _assert_port_free(int(validation_service.get("port") or 0))
    _assert_port_free(int((plan.get("service") or {}).get("port") or 0))
    pointer_path = Path(str(plan["active_pointer_path"])).resolve()
    pointer = _verify_signed(
        pointer_path,
        label="待正式开放的活动运行指针",
        purpose=POINTER_PURPOSE,
        project_root=project_root,
    )
    if (
        validation.get("runtime_id") != pointer.get("runtime_id")
        or validation.get("target_runtime_template_sha256")
        != hashlib.sha256(
            _canonical_bytes(plan["target_runtime"])
        ).hexdigest()
    ):
        raise RollbackExecutionError("loopback 验证事件与当前活动指针不一致")
    resolved = _verify_pointer_payload(
        pointer,
        project_root=project_root,
        require_database_baseline=True,
        allow_pre_exposure=True,
    )
    if resolved["runtime_id"] != plan["target_runtime"]["runtime_id"]:
        raise RollbackExecutionError("待正式开放活动运行指针不是本计划目标")
    pointer_digest = hashlib.sha256(
        _canonical_bytes(_unsigned_payload(pointer))
    ).hexdigest()
    consumed_permit = _consume_auto_restore_permit(
        plan,
        project_root=project_root,
    )
    consumed_permit_path = Path(
        str(plan["auto_restore_permit_consumed_path"])
    ).resolve()
    intent = {
        "schema_version": 1,
        "status": "production_exposure_intent_recorded",
        "recorded_at": _utc_now(),
        "plan_id": plan["plan_id"],
        "ticket_id": ticket["ticket_id"],
        "runtime_id": pointer["runtime_id"],
        "target_pointer_sha256": pointer_digest,
        "auto_restore_permit_consumed_path": str(consumed_permit_path),
        "auto_restore_permit_consumed_sha256": sha256_file(
            consumed_permit_path
        ),
        "validation_event_path": str(validation_path),
        "validation_event_sha256": sha256_file(validation_path),
        "candidate_database": plan["candidate_database"],
        "commitment": (
            "本意向一旦落盘即进入不可自动恢复边界；即使后续承诺文件"
            "未写完，也必须保留目标指针和候选数据库并人工处理"
        ),
    }
    _write_signed_atomic(
        intent_path,
        intent,
        project_root=project_root,
        purpose=EXPOSURE_INTENT_PURPOSE,
        replace=False,
    )
    event = {
        "schema_version": 1,
        "status": "production_exposure_committed",
        "committed_at": _utc_now(),
        "plan_id": plan["plan_id"],
        "ticket_id": ticket["ticket_id"],
        "runtime_id": pointer["runtime_id"],
        "target_pointer_sha256": pointer_digest,
        "auto_restore_permit_consumed_path": str(consumed_permit_path),
        "auto_restore_permit_consumed_sha256": sha256_file(
            consumed_permit_path
        ),
        "validation_event_path": str(validation_path),
        "validation_event_sha256": sha256_file(validation_path),
        "production_exposure_intent_path": str(intent_path),
        "production_exposure_intent_sha256": sha256_file(intent_path),
        "candidate_database": plan["candidate_database"],
        "commitment": (
            "从此时起禁止自动恢复当前版本或丢弃候选数据库；"
            "正式监听失败转人工处理"
        ),
    }
    _write_signed_atomic(
        exposure_path,
        event,
        project_root=project_root,
        purpose=EXPOSURE_PURPOSE,
        replace=False,
    )
    return {
        "ok": True,
        "status": event["status"],
        "production_exposure_intent_path": str(intent_path),
        "production_exposure_path": str(exposure_path),
        "auto_restore_allowed": False,
        "auto_restore_permit_consumed": (
            consumed_permit.get("status")
            == "pre_exposure_auto_restore_permitted"
        ),
    }


def verify_active_switch(
    *,
    ticket_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    ticket, plan, _ = _load_ticket(
        ticket_path,
        project_root,
        allow_expired=True,
    )
    event_path = Path(str(plan["activation_event_path"])).resolve()
    event = _verify_signed(
        event_path,
        label="活动指针切换事件",
        purpose=EVENT_PURPOSE,
        project_root=project_root,
    )
    if event.get("ticket_id") != ticket.get("ticket_id"):
        raise RollbackExecutionError("活动指针切换事件与票据不一致")
    pointer_path = Path(str(plan["active_pointer_path"])).resolve()
    pointer = _verify_signed(
        pointer_path,
        label="目标活动运行指针",
        purpose=POINTER_PURPOSE,
        project_root=project_root,
    )
    if pointer.get("runtime_id") != plan["target_runtime"]["runtime_id"]:
        raise RollbackExecutionError("当前活动运行指针不是本计划目标")
    resolved = _verify_pointer_payload(
        pointer,
        project_root=project_root,
        require_database_baseline=False,
    )
    health = _read_health(str(resolved["health_url"]))
    return {
        "ok": True,
        "status": "target_runtime_verified",
        "ticket_path": str(ticket_path.resolve()),
        "active_runtime": resolved,
        "health": health,
    }


def restore_switch(
    *,
    ticket_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Restore the pre-switch implicit current runtime; never start a process."""
    project_root = project_root.resolve()
    ticket, plan, _ = _load_ticket(
        ticket_path,
        project_root,
        allow_expired=True,
    )
    event_path = Path(str(plan["activation_event_path"])).resolve()
    activation_event = _verify_signed(
        event_path,
        label="活动指针切换事件",
        purpose=EVENT_PURPOSE,
        project_root=project_root,
    )
    if activation_event.get("ticket_id") != ticket.get("ticket_id"):
        raise RollbackExecutionError("活动指针切换事件与票据不一致")
    _assert_auto_restore_allowed(plan, project_root=project_root)
    restoration_path = Path(str(plan["restoration_event_path"])).resolve()
    if _path_entry_exists(restoration_path):
        raise RollbackExecutionError("该执行票据已经执行过一次自动恢复")
    _assert_port_free(int((plan.get("service") or {}).get("port") or 0))
    pointer_path = Path(str(plan["active_pointer_path"])).resolve()
    pointer = _verify_signed(
        pointer_path,
        label="失败目标活动指针",
        purpose=POINTER_PURPOSE,
        project_root=project_root,
    )
    if pointer.get("runtime_id") != plan["target_runtime"]["runtime_id"]:
        raise RollbackExecutionError("失败现场活动指针不是本计划目标")
    _verify_archive_activation_state(
        pointer,
        project_root=project_root,
        allow_pre_exposure=True,
    )
    _write_revocation(
        ticket=ticket,
        plan=plan,
        pointer=pointer,
        reason="目标运行目录启动或健康核验失败，自动恢复当前版本",
        project_root=project_root,
    )
    archived = restoration_path.parent / "failed_target_pointer.json"
    _write_json_exclusive(archived, pointer)
    pointer_path.unlink()
    _fsync_directory(pointer_path.parent)
    current = plan["current_runtime"]
    current_database = _database_snapshot(
        Path(str(current["database_path"])),
        label="自动恢复当前数据库",
    )
    _assert_snapshot_matches(
        current_database,
        current["database_activation_snapshot"],
        label="自动恢复当前数据库",
    )
    restoration = {
        "schema_version": 1,
        "event": "current_pointer_restored",
        "event_at": _utc_now(),
        "ticket_id": ticket["ticket_id"],
        "plan_id": plan["plan_id"],
        "failed_target_pointer_archive": str(archived.resolve()),
        "failed_target_pointer_sha256": sha256_file(archived),
        "active_pointer_removed": True,
        "current_runtime_id": current["runtime_id"],
    }
    _write_signed_atomic(
        restoration_path,
        restoration,
        project_root=project_root,
        purpose=EVENT_PURPOSE,
        replace=False,
    )
    return {
        "ok": True,
        "status": "current_pointer_restored",
        "ticket_path": str(ticket_path.resolve()),
        "current_runtime": current,
        "active_pointer_present": False,
    }


def _verify_current_recovery(
    plan: dict[str, Any],
    *,
    ticket: dict[str, Any],
    project_root: Path,
) -> dict[str, Any]:
    pointer_path = Path(str(plan["active_pointer_path"])).resolve()
    if _path_entry_exists(pointer_path):
        raise RollbackExecutionError("自动恢复后活动指针仍然存在")
    _verify_revocation_record(
        ticket=ticket,
        plan=plan,
        project_root=project_root,
    )
    current = plan["current_runtime"]
    _assert_formal_checkout(
        project_root,
        expected_sha=str(current["code_sha"]),
        expected_state=current["checkout_state"],
    )
    database = _database_snapshot(
        Path(str(current["database_path"])),
        label="恢复后的当前数据库",
    )
    _assert_snapshot_matches(
        database,
        current["database_activation_snapshot"],
        label="恢复后的当前数据库",
    )
    health = _read_health(
        str((current.get("service") or {}).get("local_health_url") or "")
    )
    return {
        "runtime_dir": current["runtime_dir"],
        "database_path": current["database_path"],
        "code_sha": current["code_sha"],
        "code_revision": current["code_revision"],
        "health": health,
    }


def _verify_manual_target_retained(
    plan: dict[str, Any],
    *,
    ticket: dict[str, Any],
    project_root: Path,
) -> dict[str, Any]:
    """Capture the fail-closed post-exposure state without changing it."""
    pointer_path = Path(str(plan["active_pointer_path"])).resolve()
    if not _path_entry_exists(pointer_path):
        raise RollbackExecutionError(
            "正式网络开放边界后活动指针缺失，无法证明目标现场已保留"
        )
    pointer = _verify_signed(
        pointer_path,
        label="待人工恢复的目标活动指针",
        purpose=POINTER_PURPOSE,
        project_root=project_root,
    )
    if pointer.get("runtime_id") != (
        plan.get("target_runtime") or {}
    ).get("runtime_id"):
        raise RollbackExecutionError("待人工恢复活动指针不是本计划目标")
    claim_path, _revocation_path = _activation_state_paths(
        str(plan.get("plan_id") or ""),
        project_root,
    )
    claim = _verify_signed(
        claim_path,
        label="待人工恢复的激活声明",
        purpose=ACTIVATION_CLAIM_PURPOSE,
        project_root=project_root,
    )
    if (
        claim.get("ticket_id") != ticket.get("ticket_id")
        or claim.get("runtime_id") != pointer.get("runtime_id")
        or claim.get("target_pointer_sha256")
        != hashlib.sha256(
            _canonical_bytes(_unsigned_payload(pointer))
        ).hexdigest()
    ):
        raise RollbackExecutionError("待人工恢复激活声明与目标指针不一致")
    (
        _validation_path,
        auto_restore_permit_path,
        auto_restore_permit_consumed_path,
        exposure_intent_path,
        exposure_path,
    ) = _exposure_paths_from_pointer(pointer, project_root=project_root)
    presence = _exposure_marker_presence(
        intent_path=exposure_intent_path,
        exposure_path=exposure_path,
    )
    permit_present = _path_entry_exists(auto_restore_permit_path)
    permit_consumed = _path_entry_exists(
        auto_restore_permit_consumed_path
    )
    if (
        permit_present
        and not permit_consumed
        and not presence["intent_present"]
        and not presence["exposure_present"]
    ):
        raise RollbackExecutionError(
            "尚未进入正式网络开放边界，不应标记为保留目标人工恢复"
        )
    exposure_state = "irreversible_evidence_incomplete"
    exposure_error: str | None = None
    try:
        exposure = _verify_exposure_markers(
            pointer,
            claim=claim,
            project_root=project_root,
            allow_pre_exposure=False,
        )
        exposure_state = str(exposure["state"])
    except Exception as error:
        exposure_error = str(error)[:1000]
    pointer_verification_error: str | None = None
    try:
        _verify_pointer_payload(
            pointer,
            project_root=project_root,
            require_database_baseline=False,
            require_activation_state=False,
        )
    except Exception as error:
        pointer_verification_error = str(error)[:1000]
    database_path = Path(str(pointer.get("database_path") or "")).resolve()
    database: dict[str, Any] = {"path": str(database_path)}
    database_verification_error: str | None = None
    try:
        identity_before = _file_identity(
            database_path,
            label="人工恢复保留的候选数据库",
        )
        database = release_erp.inspect_database(database_path)
        identity_after = _file_identity(
            database_path,
            label="人工恢复保留的候选数据库",
        )
        if identity_after != identity_before:
            raise RollbackExecutionError(
                "人工恢复候选数据库在核验期间被替换"
            )
        database["file_identity"] = identity_after
        database["healthy"] = (
            database.get("integrity_check") == "ok"
            and int(database.get("foreign_key_violations") or 0) == 0
        )
    except Exception as error:
        database_verification_error = str(error)[:1000]
        database["healthy"] = False
    port = int((plan.get("service") or {}).get("port") or 0)
    production_port_free = _port_is_free(port)
    return {
        "manual_recovery_required": True,
        "target_pointer_retained": True,
        "target_pointer_path": str(pointer_path),
        "target_pointer_sha256": sha256_file(pointer_path),
        "target_runtime_id": pointer["runtime_id"],
        "target_pointer_verification_error": pointer_verification_error,
        "candidate_database_retained": _path_entry_exists(database_path),
        "candidate_database": database,
        "candidate_database_verification_error": (
            database_verification_error
        ),
        "production_port_free": production_port_free,
        "listener_may_still_be_running": not production_port_free,
        "exposure_state": exposure_state,
        "exposure_verification_error": exposure_error,
        "auto_restore_permit_present": permit_present,
        "auto_restore_permit_consumed": permit_consumed,
        **presence,
    }


def finalize_switch(
    *,
    ticket_path: Path,
    status: str,
    error: str | None = None,
    stopped_process_id: int | None = None,
    observed_listener_process_id: int | None = None,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    ticket, plan, _ = _load_ticket(
        ticket_path,
        project_root,
        allow_expired=True,
    )
    result_path = Path(str(plan["result_path"])).resolve()
    if _path_entry_exists(result_path):
        raise RollbackExecutionError("该执行票据已经生成最终结果，禁止重放")
    if status == "switch_completed":
        verification = verify_active_switch(
            ticket_path=ticket_path,
            project_root=project_root,
        )
    elif status == "rolled_back_to_current_runtime":
        restoration_path = Path(str(plan["restoration_event_path"])).resolve()
        restoration = _verify_signed(
            restoration_path,
            label="自动恢复事件",
            purpose=EVENT_PURPOSE,
            project_root=project_root,
        )
        if restoration.get("ticket_id") != ticket.get("ticket_id"):
            raise RollbackExecutionError("自动恢复事件与票据不一致")
        verification = _verify_current_recovery(
            plan,
            ticket=ticket,
            project_root=project_root,
        )
    elif status == "stopped_before_activation_manual_recovery_required":
        if not stopped_process_id or stopped_process_id <= 0:
            raise RollbackExecutionError("停服前人工恢复结果缺少原 ERP PID")
        forbidden_paths = (
            Path(str(plan["active_pointer_path"])).resolve(),
            Path(str(plan["activation_event_path"])).resolve(),
            Path(str(plan["activation_claim_path"])).resolve(),
            Path(str(plan["revocation_path"])).resolve(),
            Path(str(plan["restoration_event_path"])).resolve(),
            Path(str(plan["production_exposure_intent_path"])).resolve(),
            Path(str(plan["production_exposure_path"])).resolve(),
            Path(
                str(plan["auto_restore_permit_consumed_path"])
            ).resolve(),
        )
        if any(_path_entry_exists(path) for path in forbidden_paths):
            raise RollbackExecutionError(
                "停服前人工恢复结果检测到活动指针或切换证据，状态冲突"
            )
        _verify_validation_event(
            ticket=ticket,
            plan=plan,
            project_root=project_root,
        )
        _assert_auto_restore_allowed(plan, project_root=project_root)
        if _windows_process_exists(stopped_process_id):
            raise RollbackExecutionError("原 ERP PID 仍然存在，不能固化为已停现场")
        port = int((plan.get("service") or {}).get("port") or 0)
        listeners = _windows_listener_process_ids(port)
        if listeners and (
            not observed_listener_process_id
            or observed_listener_process_id not in listeners
        ):
            raise RollbackExecutionError(
                "正式端口存在未知监听，但提供的监听 PID 无法复核"
            )
        if (
            observed_listener_process_id
            and observed_listener_process_id == stopped_process_id
        ):
            raise RollbackExecutionError("新监听 PID 不得等于已经退出的原 ERP PID")
        verification = {
            "service_stopped_before_activation": True,
            "stopped_process_id": stopped_process_id,
            "production_port": port,
            "production_port_free": not listeners,
            "observed_listener_process_ids": listeners,
            "observed_listener_process_id": observed_listener_process_id,
            "activation_started": False,
            "active_pointer_present": False,
            "auto_restore_permit_unconsumed": True,
            "manual_recovery_required": True,
        }
    elif status == "stopped_manual_recovery_required":
        restoration_path = Path(str(plan["restoration_event_path"])).resolve()
        restoration = _verify_signed(
            restoration_path,
            label="自动恢复事件",
            purpose=EVENT_PURPOSE,
            project_root=project_root,
        )
        if restoration.get("ticket_id") != ticket.get("ticket_id"):
            raise RollbackExecutionError("自动恢复事件与票据不一致")
        _verify_revocation_record(
            ticket=ticket,
            plan=plan,
            project_root=project_root,
        )
        if _path_entry_exists(
            Path(str(plan["active_pointer_path"])).resolve()
        ):
            raise RollbackExecutionError("双重失败后活动运行指针仍然存在")
        _assert_port_free(int((plan.get("service") or {}).get("port") or 0))
        verification = {
            "service_stopped": True,
            "manual_recovery_required": True,
        }
    elif status == "manual_recovery_required":
        revocation = _verify_revocation_record(
            ticket=ticket,
            plan=plan,
            project_root=project_root,
        )
        pointer_path = Path(str(plan["active_pointer_path"])).resolve()
        if _path_entry_exists(pointer_path):
            pointer = _verify_signed(
                pointer_path,
                label="待人工处理的已撤销活动运行指针",
                purpose=POINTER_PURPOSE,
                project_root=project_root,
            )
            if pointer.get("runtime_id") != revocation.get("runtime_id"):
                raise RollbackExecutionError("待人工处理活动指针与撤销记录不一致")
        port = int((plan.get("service") or {}).get("port") or 0)
        service_stopped = _port_is_free(port)
        verification = {
            "service_stopped": service_stopped,
            "listener_may_still_be_running": not service_stopped,
            "active_pointer_present": _path_entry_exists(pointer_path),
            "active_pointer_revoked": True,
            "manual_recovery_required": True,
        }
    elif status == "manual_target_retained":
        verification = _verify_manual_target_retained(
            plan,
            ticket=ticket,
            project_root=project_root,
        )
    else:
        raise RollbackExecutionError("最终结果状态不受支持")
    result = {
        "schema_version": 1,
        "status": status,
        "completed_at": _utc_now(),
        "ticket_id": ticket["ticket_id"],
        "plan_id": plan["plan_id"],
        "plan_path": ticket["plan_path"],
        "mode": plan["mode"],
        "error": (error or "")[:2000] or None,
        "verification": verification,
        "formal_checkout_unchanged": _checkout_state(project_root)
        == plan["formal_checkout"],
        "formal_database_preserved": plan["formal_database"],
        "rollback_site_backup": plan["rollback_site_backup"],
        "candidate_database": plan["candidate_database"],
    }
    signed = _write_signed_atomic(
        result_path,
        result,
        project_root=project_root,
        purpose=RESULT_PURPOSE,
        replace=False,
    )
    return {
        "ok": status in {"switch_completed", "rolled_back_to_current_runtime"},
        "status": status,
        "result_path": str(result_path),
        "result_sha256": sha256_file(result_path),
        "verification": verification,
        "evidence_hmac": signed.get("evidence_hmac"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="天明 ERP P0-5B 阶段2活动运行目录切换工具"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare",
        help="生成现场备份、候选副本、签名计划和一次性口令，不停服",
    )
    prepare.add_argument("--compatibility-report", type=Path, required=True)
    prepare.add_argument("--active-pointer", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument(
        "--expires-minutes",
        type=int,
        default=DEFAULT_PLAN_TTL_MINUTES,
    )

    authorize = commands.add_parser(
        "authorize",
        help="验签并单次消费负责人授权，错误口令不会停服",
    )
    authorize.add_argument("--plan", type=Path, required=True)
    authorize.add_argument("--approval-token", required=True)
    authorize.add_argument("--database-approval-token")

    for name, help_text in (
        ("activate", "停服后原子切换目标活动运行指针"),
        ("verify-loopback", "正式开放前核对 loopback 预热与数据库不变"),
        ("commit-exposure", "预热和停服通过后承诺正式网络开放"),
        ("verify-active", "正式开放后核对目标运行目录、数据库和健康状态"),
        ("restore", "目标启动失败后恢复切换前当前运行指针"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--ticket", type=Path, required=True)

    revoke = commands.add_parser(
        "revoke",
        help="无法安全停止未知监听进程时撤销目标指针并保留人工现场",
    )
    revoke.add_argument("--ticket", type=Path, required=True)
    revoke.add_argument("--reason", required=True)

    finalize = commands.add_parser("finalize", help="签发最终切换或恢复结果")
    finalize.add_argument("--ticket", type=Path, required=True)
    finalize.add_argument(
        "--status",
        choices=(
            "switch_completed",
            "rolled_back_to_current_runtime",
            "stopped_before_activation_manual_recovery_required",
            "stopped_manual_recovery_required",
            "manual_recovery_required",
            "manual_target_retained",
        ),
        required=True,
    )
    finalize.add_argument("--error")
    finalize.add_argument("--stopped-process-id", type=int)
    finalize.add_argument("--observed-listener-process-id", type=int)

    resolve = commands.add_parser(
        "resolve-active",
        help="供正式启动脚本验签并解析活动运行指针",
    )
    resolve.add_argument("--pointer", type=Path, required=True)
    resolve_validation = commands.add_parser(
        "resolve-validation",
        help="供 wrapper 在切换活动指针前解析 loopback 预热目标",
    )
    resolve_validation.add_argument("--ticket", type=Path, required=True)
    release_gate = commands.add_parser(
        "assert-formal-release-allowed",
        help="供普通发布在 Prepare/Apply 前统一检查活动指针和持久声明",
    )
    release_gate.add_argument("--pointer", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "prepare":
            result = prepare_switch(
                compatibility_report_path=args.compatibility_report,
                active_pointer_path=args.active_pointer,
                output_root=args.output_root,
                plan_path=args.plan,
                project_root=PROJECT_ROOT,
                expires_minutes=args.expires_minutes,
            )
        elif args.command == "authorize":
            result = authorize_switch(
                plan_path=args.plan,
                approval_token=args.approval_token,
                database_approval_token=args.database_approval_token,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "activate":
            result = activate_switch(
                ticket_path=args.ticket,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "verify-active":
            result = verify_active_switch(
                ticket_path=args.ticket,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "verify-loopback":
            result = verify_loopback_switch(
                ticket_path=args.ticket,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "commit-exposure":
            result = commit_production_exposure(
                ticket_path=args.ticket,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "restore":
            result = restore_switch(
                ticket_path=args.ticket,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "revoke":
            result = revoke_switch(
                ticket_path=args.ticket,
                reason=args.reason,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "finalize":
            result = finalize_switch(
                ticket_path=args.ticket,
                status=args.status,
                error=args.error,
                stopped_process_id=args.stopped_process_id,
                observed_listener_process_id=(
                    args.observed_listener_process_id
                ),
                project_root=PROJECT_ROOT,
            )
        elif args.command == "resolve-active":
            result = resolve_active_runtime(
                pointer_path=args.pointer,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "resolve-validation":
            result = resolve_validation_runtime(
                ticket_path=args.ticket,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "assert-formal-release-allowed":
            result = assert_formal_release_allowed(
                pointer_path=args.pointer,
                project_root=PROJECT_ROOT,
            )
        else:
            raise RollbackExecutionError("不支持的阶段2命令")
    except Exception as error:
        print(
            json.dumps(
                {"ok": False, "status": "blocked", "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


_RELEASE_ID = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_METADATA_BYTES = 64 * 1024 * 1024


def _guidance() -> str:
    return (
        "网页直接恢复已关闭。请在服务器桌面打开“天明ERP助手”，"
        "选择经过验证的完整备份（.tmbackup），并恢复到全新安装目录。"
    )


def _base(*, configured: bool, status: str) -> dict[str, Any]:
    return {
        "configured": configured,
        "status": status,
        "direct_web_restore_enabled": False,
        "restore_method": "desktop_assistant",
        "guidance": _guidance(),
        "latest_backup": None,
        "release": None,
        "backup_error_present": False,
    }


def _read_object(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0 or size > _MAX_METADATA_BYTES:
        raise ValueError("托管状态文件大小异常")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("托管状态文件格式无效")
    return value


def _path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def managed_recovery_status() -> dict[str, Any]:
    """Return non-secret, read-only evidence from the desktop assistant.

    The web process cannot stop and replace itself safely. This projection is
    deliberately status-only; restore execution remains in the assistant.
    """

    control_value = os.getenv("TM_ERP_CONTROL", "").strip()
    if not control_value:
        return _base(configured=False, status="not_configured")

    control = Path(control_value)
    if not control.is_absolute() or control.name.casefold() != "control":
        return _base(configured=False, status="invalid_control_path")
    root = control.parent
    state_path = root / "state.json"
    try:
        state = _read_object(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return _base(configured=False, status="state_unavailable")

    result = _base(configured=True, status="no_verified_backup")
    result["backup_error_present"] = bool(state.get("backup_error"))

    release_id = str(state.get("current") or "").strip().lower()
    if _RELEASE_ID.fullmatch(release_id):
        manifest_path = root / "releases" / release_id / "manifest.json"
        try:
            manifest = _read_object(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = {}
        if manifest.get("type") == "tianming.release.v1":
            result["release"] = {
                "version": str(manifest.get("version") or ""),
                "revision": str(manifest.get("revision") or ""),
            }

    backup_value = str(state.get("last_backup") or "").strip()
    if not backup_value:
        return result
    backup = Path(backup_value)
    if not backup.is_absolute() or backup.suffix.casefold() != ".tmbackup":
        result["status"] = "invalid_backup_reference"
        return result

    available = False
    size: int | None = None
    try:
        stat = backup.stat()
        available = backup.is_file()
        size = stat.st_size if available else None
    except OSError:
        pass

    receipt_verified = False
    receipt_path = control / "backup-receipts" / f"{backup.stem}.json"
    try:
        receipt = _read_object(receipt_path)
        receipt_size = int(receipt.get("size", -1))
        receipt_hash = str(receipt.get("sha256") or "").lower()
        receipt_verified = (
            available
            and receipt.get("verified") is True
            and receipt.get("storage") == "nas"
            and _path_key(str(receipt.get("path") or "")) == _path_key(backup)
            and receipt_size == size
            and _SHA256.fullmatch(receipt_hash) is not None
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        receipt_verified = False

    result["latest_backup"] = {
        "filename": backup.name,
        "created_at": state.get("last_backup_at"),
        "size": size,
        "available": available,
        "receipt_verified": receipt_verified,
    }
    if receipt_verified:
        result["status"] = "verified"
    elif not available:
        result["status"] = "backup_unavailable"
    else:
        result["status"] = "receipt_unverified"
    return result

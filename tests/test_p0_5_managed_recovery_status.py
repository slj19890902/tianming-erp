from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.services.managed_recovery_status import managed_recovery_status


def _write_managed_fixture(tmp_path: Path, *, receipt_size_delta: int = 0) -> Path:
    root = tmp_path / "managed"
    control = root / "control"
    receipts = control / "backup-receipts"
    release_id = "b" * 64
    release = root / "releases" / release_id
    backup = tmp_path / "nas" / "verified.tmbackup"
    receipts.mkdir(parents=True)
    release.mkdir(parents=True)
    backup.parent.mkdir()
    backup.write_bytes(b"fixture complete backup")
    (root / "state.json").write_text(
        json.dumps(
            {
                "current": release_id,
                "last_backup": str(backup),
                "last_backup_at": "2026-09-29T16:09:06+08:00",
                "backup_error": None,
            }
        ),
        encoding="utf-8",
    )
    (release / "manifest.json").write_text(
        json.dumps(
            {
                "type": "tianming.release.v1",
                "version": "fixture",
                "revision": "fixture_head",
            }
        ),
        encoding="utf-8",
    )
    (receipts / "verified.json").write_text(
        json.dumps(
            {
                "path": str(backup),
                "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "size": backup.stat().st_size + receipt_size_delta,
                "verified": True,
                "storage": "nas",
            }
        ),
        encoding="utf-8",
    )
    return control


def test_status_fails_closed_without_assistant_control(monkeypatch) -> None:
    monkeypatch.delenv("TM_ERP_CONTROL", raising=False)

    result = managed_recovery_status()

    assert result["configured"] is False
    assert result["status"] == "not_configured"
    assert result["direct_web_restore_enabled"] is False
    assert "天明ERP助手" in result["guidance"]


def test_status_does_not_call_a_mismatched_receipt_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control = _write_managed_fixture(tmp_path, receipt_size_delta=1)
    monkeypatch.setenv("TM_ERP_CONTROL", str(control))

    result = managed_recovery_status()

    assert result["configured"] is True
    assert result["status"] == "receipt_unverified"
    assert result["latest_backup"]["available"] is True
    assert result["latest_backup"]["receipt_verified"] is False


def test_status_marks_a_missing_nas_package_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control = _write_managed_fixture(tmp_path)
    backup = tmp_path / "nas" / "verified.tmbackup"
    backup.unlink()
    monkeypatch.setenv("TM_ERP_CONTROL", str(control))

    result = managed_recovery_status()

    assert result["status"] == "backup_unavailable"
    assert result["latest_backup"]["available"] is False
    assert result["latest_backup"]["receipt_verified"] is False

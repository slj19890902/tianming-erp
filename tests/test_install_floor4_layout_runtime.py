from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "admin" / "install_floor4_layout_runtime.py"
SPEC = importlib.util.spec_from_file_location("install_floor4_layout_runtime", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _revision(floor: dict) -> str:
    material = {key: value for key, value in floor.items() if key != "revision"}
    rendered = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()[:16]


def _floor(code: str, *, receipt_count: int = 1) -> dict:
    floor = {
        "floor_code": code,
        "name": f"{code}实测图",
        "features": [{"id": f"zone-{code}", "version": 1}],
        "racks": [],
        "placements": [],
        "pallets": [],
        "assets": [],
        "layout_edit_receipts": [
            {"operation_key": f"receipt-{code}-{index}", "action": "test"}
            for index in range(receipt_count)
        ],
    }
    floor["revision"] = _revision(floor)
    return floor


def _floor4() -> dict:
    floor = {
        "floor_code": "4F",
        "name": "四楼扫描规划图（待现场三点标定）",
        "operational_status": "planning_only",
        "features": [],
        "racks": [],
        "placements": [],
        "pallets": [],
        "assets": [],
        "erp_area_codes": [],
        "pallets_inventory_linked": False,
        "calibration": {
            "status": "requires_site_points",
            "source_points": [],
            "scale": 1.0,
            "mirror": False,
            "applied": False,
        },
    }
    floor["revision"] = _revision(floor)
    return floor


def _render(value: dict, *, crlf: bool = False) -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if crlf:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def _fixture_files(tmp_path: Path) -> dict[str, object]:
    floor1 = _floor("1F", receipt_count=2)
    floor3 = _floor("3F", receipt_count=2)
    runtime_document = {
        "schema_version": 1,
        "generated_at": "2026-09-01T00:00:00+00:00",
        "source": "test",
        "inventory_source": "erp",
        "floors": {"1F": floor1, "3F": floor3},
        "site_layout_receipt": {"status": "confirmed"},
        "p0_26_current_map_migration": {"status": "complete"},
        "p1_107_delayed_dispatch_relocation": {"status": "complete"},
    }
    candidate_document = copy.deepcopy(runtime_document)
    candidate_document["floors"]["4F"] = _floor4()

    draft_document = copy.deepcopy(runtime_document)
    draft_document["generated_at"] = "2026-09-01T01:00:00+00:00"
    draft_document["floors"]["3F"]["features"].append(
        {"id": "new-zone", "version": 1}
    )
    draft_document["floors"]["3F"]["layout_edit_receipts"].append(
        {"operation_key": "receipt-draft-new-zone", "action": "feature.create"}
    )
    draft_document["floors"]["3F"]["revision"] = _revision(
        draft_document["floors"]["3F"]
    )

    runtime_bytes = _render(runtime_document, crlf=True)
    candidate_bytes = _render(candidate_document, crlf=True)
    draft_document["draft_meta"] = {
        "status": "draft",
        "created_at": "2026-09-01T00:30:00+00:00",
        "updated_at": "2026-09-01T01:00:00+00:00",
        "base_published_sha256": _sha(runtime_bytes),
        "base_floor_revisions": {
            "1F": floor1["revision"],
            "3F": floor3["revision"],
        },
        "replacement_reason": "published_map_changed",
    }
    draft_bytes = _render(draft_document)

    runtime_path = tmp_path / "runtime" / "twin_layout_v1.json"
    draft_path = tmp_path / "draft" / "twin_layout_v1.draft.json"
    candidate_path = tmp_path / "candidate" / "twin_layout_v1.json"
    for path, content in (
        (runtime_path, runtime_bytes),
        (draft_path, draft_bytes),
        (candidate_path, candidate_bytes),
    ):
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    return {
        "runtime_path": runtime_path,
        "draft_path": draft_path,
        "candidate_path": candidate_path,
        "runtime_bytes": runtime_bytes,
        "draft_bytes": draft_bytes,
        "candidate_bytes": candidate_bytes,
        "runtime_document": runtime_document,
        "draft_document": draft_document,
        "candidate_document": candidate_document,
    }


def _plan(files: dict[str, object]):
    return installer.build_install_plan(
        runtime_path=files["runtime_path"],
        draft_path=files["draft_path"],
        candidate_path=files["candidate_path"],
        expected_runtime_sha256=_sha(files["runtime_bytes"]),
        expected_draft_sha256=_sha(files["draft_bytes"]),
        expected_candidate_sha256=_sha(files["candidate_bytes"]),
    )


def test_build_plan_only_adds_4f_and_preserves_dirty_draft(tmp_path: Path) -> None:
    files = _fixture_files(tmp_path)
    plan = _plan(files)

    rebased = json.loads(plan.draft_after.decode("utf-8"))
    assert plan.runtime_after == files["candidate_bytes"]
    assert plan.dirty_floors_before == plan.dirty_floors_after == ("3F",)
    assert rebased["floors"]["1F"] == files["draft_document"]["floors"]["1F"]
    assert rebased["floors"]["3F"] == files["draft_document"]["floors"]["3F"]
    assert rebased["floors"]["3F"]["layout_edit_receipts"] == (
        files["draft_document"]["floors"]["3F"]["layout_edit_receipts"]
    )
    assert rebased["draft_meta"]["base_published_sha256"] == _sha(
        files["candidate_bytes"]
    )
    assert rebased["draft_meta"]["base_floor_revisions"]["4F"] == (
        files["candidate_document"]["floors"]["4F"]["revision"]
    )


def test_apply_creates_exclusive_audit_bundle_and_installs_atomically(
    tmp_path: Path,
) -> None:
    files = _fixture_files(tmp_path)
    plan = _plan(files)
    backup_dir = tmp_path / "backups" / "release-floor4"

    result = installer.apply_install_plan(plan, backup_dir=backup_dir)

    assert result["status"] == "succeeded"
    assert files["runtime_path"].read_bytes() == files["candidate_bytes"]
    assert _sha(files["draft_path"].read_bytes()) == plan.draft_after_sha256
    assert (backup_dir / "runtime.before.json").read_bytes() == files["runtime_bytes"]
    assert (backup_dir / "draft.before.json").read_bytes() == files["draft_bytes"]
    assert (backup_dir / "candidate.source.json").read_bytes() == files["candidate_bytes"]
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["before"]["dirty_floors"] == ["3F"]
    assert manifest["planned_after"]["dirty_floors"] == ["3F"]
    assert (backup_dir / "manifest.sha256").is_file()
    assert (backup_dir / "result.succeeded.json").is_file()


def test_duplicate_json_key_is_rejected_without_writes(tmp_path: Path) -> None:
    files = _fixture_files(tmp_path)
    duplicate = (
        b'{"schema_version":1,"schema_version":1,"floors":{"1F":{},"3F":{}}}'
    )
    files["runtime_path"].write_bytes(duplicate)
    before_draft = files["draft_path"].read_bytes()

    with pytest.raises(installer.LayoutInstallError, match="duplicate key"):
        installer.build_install_plan(
            runtime_path=files["runtime_path"],
            draft_path=files["draft_path"],
            candidate_path=files["candidate_path"],
            expected_runtime_sha256=_sha(duplicate),
            expected_draft_sha256=_sha(files["draft_bytes"]),
            expected_candidate_sha256=_sha(files["candidate_bytes"]),
        )

    assert files["draft_path"].read_bytes() == before_draft


def test_candidate_change_to_protected_floor_is_rejected(tmp_path: Path) -> None:
    files = _fixture_files(tmp_path)
    candidate = copy.deepcopy(files["candidate_document"])
    candidate["floors"]["3F"]["name"] = "不允许的三楼改名"
    candidate_bytes = _render(candidate, crlf=True)
    files["candidate_path"].write_bytes(candidate_bytes)

    with pytest.raises(installer.LayoutInstallError, match="protected runtime floor 3F"):
        installer.build_install_plan(
            runtime_path=files["runtime_path"],
            draft_path=files["draft_path"],
            candidate_path=files["candidate_path"],
            expected_runtime_sha256=_sha(files["runtime_bytes"]),
            expected_draft_sha256=_sha(files["draft_bytes"]),
            expected_candidate_sha256=_sha(candidate_bytes),
        )


def test_second_replace_failure_restores_both_files_and_writes_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _fixture_files(tmp_path)
    plan = _plan(files)
    backup_dir = tmp_path / "backups" / "failed-floor4"
    original_replace = installer._atomic_replace_bytes
    calls = 0

    def fail_runtime_replace(target: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated runtime replace failure")
        original_replace(target, content)

    monkeypatch.setattr(installer, "_atomic_replace_bytes", fail_runtime_replace)

    with pytest.raises(installer.LayoutInstallError, match="both files were restored"):
        installer.apply_install_plan(plan, backup_dir=backup_dir)

    assert files["runtime_path"].read_bytes() == files["runtime_bytes"]
    assert files["draft_path"].read_bytes() == files["draft_bytes"]
    failure = json.loads(
        (backup_dir / "result.failed.json").read_text(encoding="utf-8")
    )
    assert failure["status"] == "rolled_back"
    assert failure["rollback"] == {"ok": True, "errors": []}


def test_source_change_after_plan_is_rejected_before_any_replace(
    tmp_path: Path,
) -> None:
    files = _fixture_files(tmp_path)
    plan = _plan(files)
    backup_dir = tmp_path / "backups" / "source-changed-floor4"
    changed_draft = files["draft_bytes"] + b"\n"
    files["draft_path"].write_bytes(changed_draft)

    with pytest.raises(installer.LayoutInstallError, match="no target file was replaced"):
        installer.apply_install_plan(plan, backup_dir=backup_dir)

    assert files["runtime_path"].read_bytes() == files["runtime_bytes"]
    assert files["draft_path"].read_bytes() == changed_draft
    failure = json.loads(
        (backup_dir / "result.failed.json").read_text(encoding="utf-8")
    )
    assert failure["status"] == "rejected_source_changed"
    assert failure["targets_modified"] is False


def test_existing_backup_directory_is_rejected_before_target_write(tmp_path: Path) -> None:
    files = _fixture_files(tmp_path)
    plan = _plan(files)
    backup_dir = tmp_path / "existing-backup"
    backup_dir.mkdir()

    with pytest.raises(installer.LayoutInstallError, match="must not already exist"):
        installer.apply_install_plan(plan, backup_dir=backup_dir)

    assert files["runtime_path"].read_bytes() == files["runtime_bytes"]
    assert files["draft_path"].read_bytes() == files["draft_bytes"]


def test_cli_requires_absolute_paths(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    files = _fixture_files(tmp_path)
    exit_code = installer.main(
        [
            "--runtime",
            "relative-runtime.json",
            "--draft",
            str(files["draft_path"]),
            "--candidate",
            str(files["candidate_path"]),
            "--backup-dir",
            str(tmp_path / "backup"),
            "--expected-runtime-sha256",
            _sha(files["runtime_bytes"]),
            "--expected-draft-sha256",
            _sha(files["draft_bytes"]),
            "--expected-candidate-sha256",
            _sha(files["candidate_bytes"]),
        ]
    )

    assert exit_code == 1
    assert "explicit absolute path" in capsys.readouterr().err

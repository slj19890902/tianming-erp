from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class LayoutInstallError(RuntimeError):
    """A controlled layout installation failed closed."""


@dataclass(frozen=True)
class DocumentSnapshot:
    path: Path
    content: bytes
    sha256: str
    document: dict[str, Any]


@dataclass(frozen=True)
class InstallPlan:
    runtime: DocumentSnapshot
    draft: DocumentSnapshot
    candidate: DocumentSnapshot
    runtime_after: bytes
    draft_after: bytes
    runtime_after_sha256: str
    draft_after_sha256: str
    dirty_floors_before: tuple[str, ...]
    dirty_floors_after: tuple[str, ...]
    protected_fingerprints: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return _sha256(_canonical_bytes(value))


def _stable_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LayoutInstallError(f"JSON contains a duplicate key: {key}")
        result[key] = value
    return result


def _parse_document(content: bytes, *, label: str) -> dict[str, Any]:
    if content.startswith(b"\xef\xbb\xbf"):
        raise LayoutInstallError(f"{label} must be UTF-8 without BOM")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LayoutInstallError(f"{label} is not valid UTF-8") from error
    try:
        document = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except LayoutInstallError:
        raise
    except json.JSONDecodeError as error:
        raise LayoutInstallError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise LayoutInstallError(f"{label} must contain a JSON object")
    if document.get("schema_version") != 1:
        raise LayoutInstallError(f"{label} schema_version must be 1")
    if not isinstance(document.get("floors"), dict):
        raise LayoutInstallError(f"{label} must contain a floors object")
    return document


def _absolute_path(raw: str, *, label: str, must_exist: bool) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise LayoutInstallError(f"{label} must be an explicit absolute path: {raw}")
    path = path.resolve(strict=False)
    if must_exist and not path.is_file():
        raise LayoutInstallError(f"{label} does not exist: {path}")
    if must_exist and path.is_symlink():
        raise LayoutInstallError(f"{label} must not be a symbolic link: {path}")
    return path


def _snapshot(path: Path, *, label: str) -> DocumentSnapshot:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise LayoutInstallError(f"Unable to read {label}: {path}") from error
    return DocumentSnapshot(
        path=path,
        content=content,
        sha256=_sha256(content),
        document=_parse_document(content, label=label),
    )


def _require_sha(actual: str, expected: str, *, label: str) -> None:
    normalized = str(expected or "").strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise LayoutInstallError(f"{label} expected SHA-256 is invalid")
    if actual != normalized:
        raise LayoutInstallError(
            f"{label} SHA-256 changed; expected={normalized} actual={actual}"
        )


def _floor_revision(floor: dict[str, Any]) -> str:
    material = {key: value for key, value in floor.items() if key != "revision"}
    return _canonical_sha256(material)[:16]


def _dirty_floors(document: dict[str, Any]) -> tuple[str, ...]:
    meta = document.get("draft_meta")
    if not isinstance(meta, dict):
        raise LayoutInstallError("Draft must contain draft_meta")
    revisions = meta.get("base_floor_revisions")
    if not isinstance(revisions, dict):
        raise LayoutInstallError("Draft must contain base_floor_revisions")
    return tuple(
        sorted(
            str(code)
            for code, floor in document["floors"].items()
            if not isinstance(floor, dict)
            or str(floor.get("revision") or "")
            != str(revisions.get(code) or "")
        )
    )


def _protected_fingerprints(
    runtime: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "runtime_top_without_floors": _canonical_sha256(
            {key: value for key, value in runtime.items() if key != "floors"}
        ),
        "draft_top_without_floors_and_meta": _canonical_sha256(
            {
                key: value
                for key, value in draft.items()
                if key not in {"floors", "draft_meta"}
            }
        ),
        "runtime_floors": {},
        "draft_floors": {},
    }
    for floor_code in ("1F", "3F"):
        runtime_floor = runtime["floors"][floor_code]
        draft_floor = draft["floors"][floor_code]
        result["runtime_floors"][floor_code] = {
            "floor": _canonical_sha256(runtime_floor),
            "receipts": _canonical_sha256(runtime_floor.get("layout_edit_receipts") or []),
        }
        result["draft_floors"][floor_code] = {
            "floor": _canonical_sha256(draft_floor),
            "receipts": _canonical_sha256(draft_floor.get("layout_edit_receipts") or []),
        }
    return result


def _validate_floor4(floor: Any) -> dict[str, Any]:
    if not isinstance(floor, dict):
        raise LayoutInstallError("Candidate 4F must be a JSON object")
    if floor.get("floor_code") != "4F":
        raise LayoutInstallError("Candidate 4F floor_code must be 4F")
    revision = str(floor.get("revision") or "")
    if not revision or revision != _floor_revision(floor):
        raise LayoutInstallError("Candidate 4F revision does not match its content")
    if floor.get("operational_status") != "planning_only":
        raise LayoutInstallError("Candidate 4F must remain planning_only")
    calibration = floor.get("calibration")
    if not isinstance(calibration, dict):
        raise LayoutInstallError("Candidate 4F must contain calibration metadata")
    if calibration.get("status") != "requires_site_points":
        raise LayoutInstallError("Candidate 4F must still require site calibration")
    if calibration.get("applied") is not False:
        raise LayoutInstallError("Candidate 4F calibration must not be applied")
    if calibration.get("source_points") not in ([], None):
        raise LayoutInstallError("Candidate 4F must not contain site calibration points")
    if calibration.get("scale") != 1.0 or calibration.get("mirror") is not False:
        raise LayoutInstallError("Candidate 4F must keep fixed scale and no mirroring")
    for collection in ("features", "placements", "racks", "pallets", "assets"):
        if floor.get(collection) not in ([], None):
            raise LayoutInstallError(f"Candidate 4F {collection} must be empty")
    if floor.get("erp_area_codes") not in ([], None):
        raise LayoutInstallError("Candidate 4F must not contain ERP area codes")
    if floor.get("pallets_inventory_linked") is not False:
        raise LayoutInstallError("Candidate 4F must not be linked to inventory")
    return floor


def build_install_plan(
    *,
    runtime_path: Path,
    draft_path: Path,
    candidate_path: Path,
    expected_runtime_sha256: str,
    expected_draft_sha256: str,
    expected_candidate_sha256: str,
) -> InstallPlan:
    paths = {runtime_path, draft_path, candidate_path}
    if len(paths) != 3:
        raise LayoutInstallError("Runtime, draft, and candidate paths must be distinct")

    runtime = _snapshot(runtime_path, label="runtime layout")
    draft = _snapshot(draft_path, label="layout draft")
    candidate = _snapshot(candidate_path, label="candidate layout")
    _require_sha(candidate.sha256, expected_candidate_sha256, label="Candidate layout")

    runtime_floors = runtime.document["floors"]
    candidate_floors = candidate.document["floors"]
    if set(runtime_floors) != {"1F", "3F"}:
        raise LayoutInstallError(
            f"Runtime floors must be exactly 1F and 3F before installation: {sorted(runtime_floors)}"
        )
    if set(candidate_floors) != {"1F", "3F", "4F"}:
        raise LayoutInstallError(
            "Candidate floors must be exactly 1F, 3F, and 4F"
        )

    runtime_without_floors = {
        key: value for key, value in runtime.document.items() if key != "floors"
    }
    candidate_without_floors = {
        key: value for key, value in candidate.document.items() if key != "floors"
    }
    if runtime_without_floors != candidate_without_floors:
        raise LayoutInstallError("Candidate changes top-level runtime metadata in addition to 4F")
    for floor_code in ("1F", "3F"):
        if runtime_floors[floor_code] != candidate_floors[floor_code]:
            raise LayoutInstallError(
                f"Candidate changes protected runtime floor {floor_code}"
            )
    floor4 = _validate_floor4(candidate_floors["4F"])

    expected_candidate = copy.deepcopy(runtime.document)
    expected_candidate["floors"]["4F"] = copy.deepcopy(floor4)
    if expected_candidate != candidate.document:
        raise LayoutInstallError("Candidate contains changes other than adding 4F")

    # Once the semantic comparison proves that the candidate only adds 4F, use
    # its exact tracked bytes as the runtime target.  This preserves the raw SHA
    # that draft staleness checks bind to, including checkout line endings.
    _require_sha(runtime.sha256, expected_runtime_sha256, label="Runtime layout")
    _require_sha(draft.sha256, expected_draft_sha256, label="Layout draft")

    draft_floors = draft.document["floors"]
    if set(draft_floors) != {"1F", "3F"}:
        raise LayoutInstallError(
            f"Draft floors must be exactly 1F and 3F before installation: {sorted(draft_floors)}"
        )
    meta = draft.document.get("draft_meta")
    if not isinstance(meta, dict) or meta.get("status") not in {"draft", "validated"}:
        raise LayoutInstallError("An active draft or validated draft is required")
    if str(meta.get("base_published_sha256") or "").lower() != runtime.sha256:
        raise LayoutInstallError("Draft is already stale against the current runtime")
    base_revisions = meta.get("base_floor_revisions")
    if not isinstance(base_revisions, dict):
        raise LayoutInstallError("Draft base_floor_revisions is invalid")
    for floor_code in ("1F", "3F"):
        runtime_revision = str(runtime_floors[floor_code].get("revision") or "")
        if str(base_revisions.get(floor_code) or "") != runtime_revision:
            raise LayoutInstallError(
                f"Draft base revision does not match runtime {floor_code}"
            )

    protected_before = _protected_fingerprints(runtime.document, draft.document)
    dirty_before = _dirty_floors(draft.document)
    draft_after_document = copy.deepcopy(draft.document)
    draft_after_document["floors"]["4F"] = copy.deepcopy(floor4)
    draft_after_meta = draft_after_document["draft_meta"]
    draft_after_meta["base_published_sha256"] = candidate.sha256
    draft_after_meta["base_floor_revisions"]["4F"] = floor4["revision"]
    dirty_after = _dirty_floors(draft_after_document)

    for floor_code in ("1F", "3F"):
        if draft_after_document["floors"][floor_code] != draft_floors[floor_code]:
            raise LayoutInstallError(f"Draft {floor_code} changed during rebase")
        if (
            draft_after_document["floors"][floor_code].get("layout_edit_receipts")
            != draft_floors[floor_code].get("layout_edit_receipts")
        ):
            raise LayoutInstallError(f"Draft {floor_code} receipts changed during rebase")
    if dirty_after != dirty_before:
        raise LayoutInstallError(
            f"Draft dirty floors changed during rebase: before={dirty_before} after={dirty_after}"
        )
    if "4F" in dirty_after:
        raise LayoutInstallError("New 4F must be clean in the rebased draft")

    draft_after = _stable_json_bytes(draft_after_document)
    protected_after = _protected_fingerprints(candidate.document, draft_after_document)
    if protected_after != protected_before:
        raise LayoutInstallError("Protected 1F/3F or draft receipt fingerprints changed")

    return InstallPlan(
        runtime=runtime,
        draft=draft,
        candidate=candidate,
        runtime_after=candidate.content,
        draft_after=draft_after,
        runtime_after_sha256=candidate.sha256,
        draft_after_sha256=_sha256(draft_after),
        dirty_floors_before=dirty_before,
        dirty_floors_after=dirty_after,
        protected_fingerprints=protected_before,
    )


def _write_exclusive(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise LayoutInstallError(f"Exclusive file creation failed: {path}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _stage_bytes(target: Path, content: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.floor4-",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256(temporary.read_bytes()) != _sha256(content):
            raise LayoutInstallError(f"Staged file SHA verification failed: {target}")
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_replace_bytes(target: Path, content: bytes) -> None:
    temporary = _stage_bytes(target, content)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest(plan: InstallPlan) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": "install_floor4_layout_runtime",
        "prepared_at": _utc_now(),
        "paths": {
            "runtime": str(plan.runtime.path),
            "draft": str(plan.draft.path),
            "candidate": str(plan.candidate.path),
        },
        "before": {
            "runtime": {"sha256": plan.runtime.sha256, "size": len(plan.runtime.content)},
            "draft": {"sha256": plan.draft.sha256, "size": len(plan.draft.content)},
            "dirty_floors": list(plan.dirty_floors_before),
        },
        "candidate": {
            "sha256": plan.candidate.sha256,
            "size": len(plan.candidate.content),
            "floor4_revision": plan.candidate.document["floors"]["4F"]["revision"],
            "floor4_canonical_sha256": _canonical_sha256(
                plan.candidate.document["floors"]["4F"]
            ),
        },
        "planned_after": {
            "runtime": {
                "sha256": plan.runtime_after_sha256,
                "size": len(plan.runtime_after),
            },
            "draft": {
                "sha256": plan.draft_after_sha256,
                "size": len(plan.draft_after),
            },
            "dirty_floors": list(plan.dirty_floors_after),
        },
        "protected_fingerprints": plan.protected_fingerprints,
    }


def _verify_after(plan: InstallPlan) -> dict[str, Any]:
    runtime = _snapshot(plan.runtime.path, label="installed runtime layout")
    draft = _snapshot(plan.draft.path, label="installed layout draft")
    if runtime.sha256 != plan.runtime_after_sha256:
        raise LayoutInstallError("Installed runtime SHA does not match the plan")
    if draft.sha256 != plan.draft_after_sha256:
        raise LayoutInstallError("Installed draft SHA does not match the plan")
    if runtime.document != plan.candidate.document:
        raise LayoutInstallError("Installed runtime is not the approved candidate")
    if _protected_fingerprints(runtime.document, draft.document) != plan.protected_fingerprints:
        raise LayoutInstallError("Installed protected fingerprints changed")
    meta = draft.document.get("draft_meta") or {}
    if str(meta.get("base_published_sha256") or "") != runtime.sha256:
        raise LayoutInstallError("Installed draft base SHA does not match runtime")
    floor4 = _validate_floor4(draft.document["floors"].get("4F"))
    if str((meta.get("base_floor_revisions") or {}).get("4F") or "") != str(
        floor4.get("revision") or ""
    ):
        raise LayoutInstallError("Installed draft 4F base revision is invalid")
    dirty = _dirty_floors(draft.document)
    if dirty != plan.dirty_floors_before or "4F" in dirty:
        raise LayoutInstallError("Installed draft dirty floor set changed")
    return {
        "runtime_sha256": runtime.sha256,
        "draft_sha256": draft.sha256,
        "dirty_floors": list(dirty),
    }


def _verify_sources_unchanged(plan: InstallPlan) -> None:
    """Close the plan-to-replace window before the first target mutation."""

    for expected, label in (
        (plan.runtime, "runtime layout"),
        (plan.draft, "layout draft"),
        (plan.candidate, "candidate layout"),
    ):
        current = _snapshot(expected.path, label=f"current {label}")
        if current.sha256 != expected.sha256:
            raise LayoutInstallError(
                f"{label} changed after validation; "
                f"planned={expected.sha256} current={current.sha256}"
            )


def _restore_originals(plan: InstallPlan) -> dict[str, Any]:
    errors: list[str] = []
    for target, content, expected_sha in (
        (plan.draft.path, plan.draft.content, plan.draft.sha256),
        (plan.runtime.path, plan.runtime.content, plan.runtime.sha256),
    ):
        try:
            _atomic_replace_bytes(target, content)
            actual_sha = _sha256(target.read_bytes())
            if actual_sha != expected_sha:
                raise LayoutInstallError(
                    f"Rollback SHA mismatch for {target}: {actual_sha}"
                )
        except Exception as error:  # Keep trying to restore both files.
            errors.append(f"{target}: {error}")
    return {"ok": not errors, "errors": errors}


def apply_install_plan(plan: InstallPlan, *, backup_dir: Path) -> dict[str, Any]:
    if backup_dir.exists():
        raise LayoutInstallError(f"Backup directory must not already exist: {backup_dir}")
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        backup_dir.mkdir(exist_ok=False)
    except OSError as error:
        raise LayoutInstallError(f"Unable to create exclusive backup directory: {backup_dir}") from error

    manifest = _manifest(plan)
    manifest_bytes = _stable_json_bytes(manifest)
    _write_exclusive(backup_dir / "runtime.before.json", plan.runtime.content)
    _write_exclusive(backup_dir / "draft.before.json", plan.draft.content)
    _write_exclusive(backup_dir / "candidate.source.json", plan.candidate.content)
    _write_exclusive(backup_dir / "manifest.json", manifest_bytes)
    _write_exclusive(
        backup_dir / "manifest.sha256",
        f"{_sha256(manifest_bytes)}  manifest.json\n".encode("ascii"),
    )

    for name, expected in (
        ("runtime.before.json", plan.runtime.sha256),
        ("draft.before.json", plan.draft.sha256),
        ("candidate.source.json", plan.candidate.sha256),
    ):
        if _sha256((backup_dir / name).read_bytes()) != expected:
            raise LayoutInstallError(f"Backup verification failed: {name}")

    try:
        _verify_sources_unchanged(plan)
    except Exception as error:
        rejection = {
            "schema_version": 1,
            "status": "rejected_source_changed",
            "failed_at": _utc_now(),
            "error": str(error),
            "targets_modified": False,
        }
        _write_exclusive(
            backup_dir / "result.failed.json", _stable_json_bytes(rejection)
        )
        raise LayoutInstallError(
            f"Layout sources changed after validation; no target file was replaced: {error}"
        ) from error

    try:
        # Cross-file atomic replacement does not exist on Windows.  Both files
        # are staged and replaced atomically one at a time while ERP is stopped;
        # any failure restores both original byte streams before returning.
        _atomic_replace_bytes(plan.draft.path, plan.draft_after)
        _atomic_replace_bytes(plan.runtime.path, plan.runtime_after)
        verified = _verify_after(plan)
    except Exception as error:
        rollback = _restore_originals(plan)
        failure = {
            "schema_version": 1,
            "status": "rolled_back" if rollback["ok"] else "rollback_failed",
            "failed_at": _utc_now(),
            "error": str(error),
            "rollback": rollback,
            "before": {
                "runtime_sha256": plan.runtime.sha256,
                "draft_sha256": plan.draft.sha256,
            },
        }
        _write_exclusive(backup_dir / "result.failed.json", _stable_json_bytes(failure))
        if not rollback["ok"]:
            raise LayoutInstallError(
                f"Layout install failed and automatic rollback was incomplete: {rollback['errors']}"
            ) from error
        raise LayoutInstallError(
            f"Layout install failed; both files were restored: {error}"
        ) from error

    result = {
        "schema_version": 1,
        "status": "succeeded",
        "completed_at": _utc_now(),
        "backup_dir": str(backup_dir),
        "before": {
            "runtime_sha256": plan.runtime.sha256,
            "draft_sha256": plan.draft.sha256,
            "dirty_floors": list(plan.dirty_floors_before),
        },
        "after": verified,
    }
    _write_exclusive(backup_dir / "result.succeeded.json", _stable_json_bytes(result))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely install the approved 4F planning layout into formal runtime and draft files."
    )
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--draft", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--expected-runtime-sha256", required=True)
    parser.add_argument("--expected-draft-sha256", required=True)
    parser.add_argument("--expected-candidate-sha256", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply after validation. Without this flag, only print the read-only plan.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runtime = _absolute_path(args.runtime, label="Runtime layout", must_exist=True)
        draft = _absolute_path(args.draft, label="Layout draft", must_exist=True)
        candidate = _absolute_path(args.candidate, label="Candidate layout", must_exist=True)
        backup_dir = _absolute_path(args.backup_dir, label="Backup directory", must_exist=False)
        plan = build_install_plan(
            runtime_path=runtime,
            draft_path=draft,
            candidate_path=candidate,
            expected_runtime_sha256=args.expected_runtime_sha256,
            expected_draft_sha256=args.expected_draft_sha256,
            expected_candidate_sha256=args.expected_candidate_sha256,
        )
        if args.apply:
            output = apply_install_plan(plan, backup_dir=backup_dir)
        else:
            output = {
                "schema_version": 1,
                "status": "validated_not_applied",
                "backup_dir": str(backup_dir),
                "before": {
                    "runtime_sha256": plan.runtime.sha256,
                    "draft_sha256": plan.draft.sha256,
                    "dirty_floors": list(plan.dirty_floors_before),
                },
                "planned_after": {
                    "runtime_sha256": plan.runtime_after_sha256,
                    "draft_sha256": plan.draft_after_sha256,
                    "dirty_floors": list(plan.dirty_floors_after),
                },
            }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except LayoutInstallError as error:
        print(
            json.dumps(
                {"status": "rejected", "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

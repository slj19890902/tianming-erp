from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__:
    from scripts.admin import release_erp
    from scripts.admin.release_state_common import (
        ReleaseStateError,
        code_revision_at,
        runtime_config_fingerprint,
        sha256_file,
        sqlite_logical_fingerprint_via_copy,
        verify_evidence,
    )
else:
    import release_erp  # type: ignore[no-redef]
    from release_state_common import (  # type: ignore[no-redef]
        ReleaseStateError,
        code_revision_at,
        runtime_config_fingerprint,
        sha256_file,
        sqlite_logical_fingerprint_via_copy,
        verify_evidence,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RollbackAssessmentError(RuntimeError):
    """The read-only rollback assessment cannot trust its evidence."""


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise RollbackAssessmentError(f"{label}不存在：{resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RollbackAssessmentError(f"{label}无法读取：{error}") from error
    if not isinstance(payload, dict):
        raise RollbackAssessmentError(f"{label}格式错误")
    return payload


def _git_output(project_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RollbackAssessmentError(f"Git 只读检查失败：{detail}")
    return result.stdout.strip()


def _changed_tables(
    completed: dict[str, Any],
    current: dict[str, Any],
) -> list[dict[str, Any]]:
    before_tables = completed.get("tables") or {}
    current_tables = current.get("tables") or {}
    names = sorted(set(before_tables) | set(current_tables))
    changed: list[dict[str, Any]] = []
    for name in names:
        before = before_tables.get(name)
        after = current_tables.get(name)
        if before != after:
            changed.append(
                {
                    "table": name,
                    "completed_rows": before.get("rows") if before else None,
                    "current_rows": after.get("rows") if after else None,
                }
            )
    return changed


def _blocked(
    *,
    reason: str,
    plan: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "read_only": True,
        "mode": "blocked",
        "mode_label": "当前不能安全回退",
        "reason": reason,
        "current_code_sha": None,
        "previous_code_sha": plan.get("previous_code_sha"),
        "release_code_sha": plan.get("code_sha"),
        "database_changed_since_release": None,
        "changed_tables": [],
    }
    if extra:
        result.update(extra)
    return result


def assess_rollback(
    *,
    pointer_path: Path,
    database: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Assess rollback eligibility without changing Git, service, or database."""

    pointer_path = pointer_path.resolve()
    pointer = _read_json(pointer_path, label="最近发布指针")
    try:
        verify_evidence(
            pointer,
            project_root=project_root,
            purpose="release-pointer-v2",
        )
    except ReleaseStateError as error:
        raise RollbackAssessmentError(str(error)) from error
    if int(pointer.get("schema_version") or 0) != 2:
        raise RollbackAssessmentError("最近发布指针版本不受支持")
    plan_path = Path(str(pointer.get("release_plan_path") or "")).resolve()
    expected_plan_hash = str(pointer.get("release_plan_sha256") or "")
    if not expected_plan_hash:
        raise RollbackAssessmentError("最近发布指针缺少发布报告哈希")
    if not plan_path.is_file():
        raise RollbackAssessmentError(f"发布报告不存在：{plan_path}")
    if sha256_file(plan_path) != expected_plan_hash:
        raise RollbackAssessmentError("发布报告与最近发布指针哈希不一致")

    plan = _read_json(plan_path, label="发布报告")
    try:
        verify_evidence(
            plan,
            project_root=project_root,
            purpose="release-plan-v3",
        )
    except ReleaseStateError as error:
        raise RollbackAssessmentError(str(error)) from error
    if int(plan.get("schema_version") or 0) != 3:
        return _blocked(reason="发布报告版本过旧，缺少可信回退证据", plan=plan)
    if plan.get("status") != "completed":
        return _blocked(
            reason=f"发布尚未完整完成：{plan.get('status')}",
            plan=plan,
        )

    current_branch = _git_output(project_root, "branch", "--show-current")
    current_code_sha = _git_output(project_root, "rev-parse", "HEAD")
    dirty = _git_output(project_root, "status", "--porcelain")
    common = {
        "current_code_sha": current_code_sha,
        "current_branch": current_branch,
        "previous_code_sha": plan.get("previous_code_sha"),
        "release_code_sha": plan.get("code_sha"),
        "release_completed_at": plan.get("service_started_at"),
        "backup_path": (plan.get("backup") or {}).get("path"),
        "plan_path": str(plan_path),
        "pointer_path": str(pointer_path),
    }
    if current_branch != "factory-current-baseline":
        return _blocked(
            reason=f"当前分支不是 factory-current-baseline：{current_branch or 'detached'}",
            plan=plan,
            extra=common,
        )
    if dirty:
        return _blocked(
            reason="正式工作区存在未提交修改，禁止自动判断可回退",
            plan=plan,
            extra=common,
        )
    if current_code_sha != plan.get("code_sha"):
        return _blocked(
            reason="当前代码不是最近一次已完成发布版本",
            plan=plan,
            extra=common,
        )

    resolved_database = database.resolve()
    planned_database = Path(str((plan.get("source") or {}).get("path") or "")).resolve()
    if resolved_database != planned_database:
        return _blocked(
            reason="当前数据库路径与发布报告不一致",
            plan=plan,
            extra=common,
        )

    backup = plan.get("backup") or {}
    backup_path = Path(str(backup.get("path") or "")).resolve()
    if not backup_path.is_file():
        return _blocked(
            reason=f"更新前备份不存在：{backup_path}",
            plan=plan,
            extra=common,
        )
    if sha256_file(backup_path) != backup.get("sha256"):
        return _blocked(
            reason="更新前备份 SHA-256 与发布报告不一致",
            plan=plan,
            extra=common,
        )
    try:
        backup_snapshot = release_erp.inspect_database(backup_path)
        release_erp.assert_healthy(backup_snapshot, label="更新前备份")
        release_erp.assert_business_smoke(backup_snapshot, label="更新前备份")
    except Exception as error:
        return _blocked(
            reason=f"更新前备份校验失败：{error}",
            plan=plan,
            extra=common,
        )
    if backup_snapshot["revision"] != plan.get("previous_code_revision"):
        return _blocked(
            reason="更新前备份 revision 与上一代码 head 不一致",
            plan=plan,
            extra=common,
        )

    try:
        previous_revision = code_revision_at(
            project_root,
            str(plan.get("previous_code_sha") or ""),
        )
    except ReleaseStateError as error:
        return _blocked(reason=str(error), plan=plan, extra=common)
    if previous_revision != plan.get("previous_code_revision"):
        return _blocked(
            reason="上一代码 Alembic head 与发布报告不一致",
            plan=plan,
            extra=common,
        )

    current_config = runtime_config_fingerprint(project_root)
    if current_config != plan.get("completed_runtime_config"):
        return _blocked(
            reason="运行配置、依赖或启动脚本自发布完成后已经变化",
            plan=plan,
            extra=common,
        )

    try:
        current_database = release_erp.inspect_database(resolved_database)
        release_erp.assert_healthy(current_database, label="当前数据库")
        release_erp.assert_business_smoke(current_database, label="当前数据库")
        current_logical = sqlite_logical_fingerprint_via_copy(resolved_database)
    except Exception as error:
        return _blocked(
            reason=f"当前数据库只读校验失败：{error}",
            plan=plan,
            extra=common,
        )

    completed_database = plan.get("completed_database") or {}
    completed_logical = completed_database.get("logical_fingerprint") or {}
    if not completed_logical.get("sha256"):
        return _blocked(
            reason="发布报告缺少发布完成时的全表逻辑指纹",
            plan=plan,
            extra=common,
        )
    changed_tables = _changed_tables(completed_logical, current_logical)
    database_changed = current_logical.get("sha256") != completed_logical.get("sha256")
    common.update(
        {
            "database_changed_since_release": database_changed,
            "changed_tables": changed_tables,
            "current_database_revision": current_database["revision"],
            "previous_code_revision": previous_revision,
            "release_database_revision": plan.get("expected_revision"),
            "backup_verified": True,
        }
    )

    if not database_changed:
        return {
            "ok": True,
            "read_only": True,
            "mode": "full_rollback_allowed",
            "mode_label": "具备完整回退申请条件",
            "reason": "发布完成后数据库内容和运行配置均未变化",
            **common,
        }
    if current_database["revision"] == previous_revision:
        return _blocked(
            reason=(
                "数据库已有变化；虽然 revision 与上一代码相同，"
                "但 P0-5A 尚未提供可验证、绑定当前数据指纹的兼容演练证据链，"
                "必须保留当前数据库；待 P0-5B 完成证据生成与验签后再评估仅代码回退"
            ),
            plan=plan,
            extra=common,
        )
    return _blocked(
        reason=(
            "数据库已有变化，且当前 revision 与上一代码 head 不一致；"
            "必须保留当前数据库并采用前向修复"
        ),
        plan=plan,
        extra=common,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="天明 ERP P0-5A 只读回退资格检查")
    parser.add_argument("--pointer", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = assess_rollback(
            pointer_path=args.pointer,
            database=args.database,
            project_root=args.project_root,
        )
    except Exception as error:
        print(
            json.dumps(
                {"ok": False, "read_only": True, "mode": "blocked", "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

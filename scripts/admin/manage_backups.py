from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.backup_retention import (
    apply_cleanup_plan,
    build_cleanup_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List or cleanup ERP backup files safely.")
    parser.add_argument("command", choices=("list", "cleanup"))
    parser.add_argument("--backup-dir", default=".\\data\\backups")
    parser.add_argument("--keep", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser


def run_command(
    *,
    command: str,
    backup_dir: Path,
    keep: int,
    apply: bool,
) -> dict:
    plan = build_cleanup_plan(backup_dir=backup_dir, keep=keep)
    result = apply_cleanup_plan(plan, apply=apply if command == "cleanup" else False)
    return {
        "command": command,
        "backup_dir": str(Path(backup_dir).resolve()),
        "keep": keep,
        "exists": Path(backup_dir).resolve().exists(),
        "total_sqlite_backups": plan.total_sqlite_backups,
        "regular_count": plan.regular_count,
        "protected_count": plan.protected_count,
        "ignored_count": plan.ignored_count,
        "keep_files": [path.name for path in plan.regular_keep],
        "delete_files": [path.name for path in plan.regular_delete],
        "protected_files": [path.name for path in plan.protected_files],
        "ignored_files": [path.name for path in plan.ignored_files],
        "estimated_reclaim_bytes": plan.estimated_reclaim_bytes,
        "estimated_reclaim_mb": round(plan.estimated_reclaim_bytes / 1024 / 1024, 2),
        "apply": apply if command == "cleanup" else False,
        "deleted_count": result.deleted_count,
        "deleted_bytes": result.deleted_bytes,
        "deleted_mb": round(result.deleted_bytes / 1024 / 1024, 2),
        "deleted_files": [path.name for path in result.deleted_files],
        "errors": list(result.errors),
    }


def main() -> int:
    args = build_parser().parse_args()
    payload = run_command(
        command=args.command,
        backup_dir=Path(args.backup_dir),
        keep=args.keep,
        apply=args.apply,
    )
    import json

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

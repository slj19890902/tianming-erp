from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.database import backup_to_nas


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 SQLite Backup API 创建 ERP 更新前备份。",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    return parser


def create_backup(database: Path, backup_dir: Path) -> dict:
    result = backup_to_nas(
        source_path=database,
        backup_dir=backup_dir,
        filename_suffix="_pre_update",
    )
    return {
        "path": str(result.path),
        "size": result.size,
        "sha256": result.sha256,
        "integrity_check": result.integrity_check,
        "cleanup_deleted_count": result.cleanup_deleted_count,
        "cleanup_deleted_bytes": result.cleanup_deleted_bytes,
        "cleanup_error": result.cleanup_error,
    }


def main() -> int:
    args = build_parser().parse_args()
    payload = create_backup(args.database, args.backup_dir)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

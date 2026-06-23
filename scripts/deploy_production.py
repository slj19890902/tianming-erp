from __future__ import annotations

import argparse
import os
import socket
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PROJECT_ROOT))

from app.core.config import PROJECT_ROOT, load_settings, normalize_path
from app.core.database import backup_to_nas


PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "carton_erp.sqlite3"


@dataclass(frozen=True, slots=True)
class PromotionResult:
    source: Path
    target: Path
    backup_path: Path | None
    integrity_check: str
    replaced: bool


def sqlite_integrity_check(path: Path) -> str:
    try:
        with closing(sqlite3.connect(path, timeout=30)) as connection:
            connection.execute("PRAGMA busy_timeout = 30000")
            result = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
    except sqlite3.DatabaseError as error:
        raise RuntimeError(f"数据库完整性检查失败: {path}") from error
    if result.lower() != "ok":
        raise RuntimeError(f"数据库完整性检查失败: {path}: {result}")
    return result


def _sqlite_backup(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source, timeout=30)) as source_connection:
        source_connection.execute("PRAGMA busy_timeout = 30000")
        with closing(sqlite3.connect(destination, timeout=30)) as target_connection:
            source_connection.backup(target_connection)
            target_connection.commit()


def promote_database(
    *,
    source: Path,
    target: Path = PRODUCTION_DATABASE,
    backup_dir: Path | None = None,
) -> PromotionResult:
    source = normalize_path(source)
    target = normalize_path(target)
    if not source.is_file():
        raise FileNotFoundError(f"预览数据库不存在: {source}")
    source_integrity = sqlite_integrity_check(source)

    target.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if target.is_file():
        backup_result = backup_to_nas(
            source_path=target,
            backup_dir=backup_dir,
            filename_suffix="_pre_production",
        )
        backup_path = backup_result.path

    if source == target:
        return PromotionResult(
            source=source,
            target=target,
            backup_path=backup_path,
            integrity_check=source_integrity,
            replaced=False,
        )

    temporary = target.with_suffix(".sqlite3.deploying")
    try:
        _sqlite_backup(source, temporary)
        temporary_integrity = sqlite_integrity_check(temporary)
        _sqlite_backup(temporary, target)
        target_integrity = sqlite_integrity_check(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink(missing_ok=True)

    return PromotionResult(
        source=source,
        target=target,
        backup_path=backup_path,
        integrity_check=target_integrity,
        replaced=True,
    )


def write_env_file(env_path: Path, values: dict[str, str]) -> None:
    existing = (
        env_path.read_text(encoding="utf-8").splitlines()
        if env_path.exists()
        else []
    )
    pending = dict(values)
    output: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in pending:
            output.append(f"{key}={pending.pop(key)}")
        else:
            output.append(line)
    for key, value in pending.items():
        output.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = env_path.with_suffix(env_path.suffix + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temporary, env_path)


def discover_preview_database(project_root: Path = PROJECT_ROOT) -> Path:
    configured = os.getenv("ERP_PREVIEW_DATABASE_PATH", "").strip()
    if configured:
        return normalize_path(configured)
    candidates = list(
        (project_root / "migration-workfiles").glob("*preview*.sqlite3")
    )
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    return normalize_path(project_root / "data" / "carton_erp.sqlite3")


def _lan_origin() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect(("10.255.255.255", 1))
            address = connection.getsockname()[0]
    except OSError:
        return None
    if address.startswith(("10.", "192.168.", "172.")):
        return f"http://{address}:8000"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全提升已验证数据库并写入生产环境配置。",
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--target", type=Path, default=PRODUCTION_DATABASE)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    current = load_settings()
    source = args.source or discover_preview_database()
    backup_dir = args.backup_dir or current.backup_dir
    result = promote_database(
        source=source,
        target=args.target,
        backup_dir=backup_dir,
    )

    origins = ["http://127.0.0.1:8000", "http://localhost:8000"]
    lan_origin = _lan_origin()
    if lan_origin:
        origins.append(lan_origin)
    write_env_file(
        normalize_path(args.env_file),
        {
            "ERP_ENVIRONMENT": "production",
            "ERP_DATABASE_PATH": str(result.target),
            "ERP_BACKUP_DIR": str(normalize_path(backup_dir)),
            "ERP_ALLOWED_ORIGINS": ",".join(origins),
            "ERP_BIND_HOST": "0.0.0.0",
            "ERP_PORT": "8000",
        },
    )

    print(f"source={result.source}")
    print(f"target={result.target}")
    print(f"replaced={str(result.replaced).lower()}")
    print(f"integrity_check={result.integrity_check}")
    print(f"backup={result.backup_path or 'none'}")
    print(f"env_file={normalize_path(args.env_file)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

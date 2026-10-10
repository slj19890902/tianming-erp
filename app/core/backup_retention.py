from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


PROTECTED_KEYWORDS_CASE_SENSITIVE = (
    "FINAL",
    "ARCHIVE",
    "MIGRATION",
)

PROTECTED_KEYWORDS_CASE_INSENSITIVE = (
    "tianhua_batch",
    "tianhua_sales_apply",
    "legacy_refresh",
)


@dataclass(frozen=True, slots=True)
class BackupFileInfo:
    path: Path
    category: str
    size: int
    mtime: float
    reason: str


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    backup_dir: Path
    keep: int
    total_sqlite_backups: int
    regular_count: int
    protected_count: int
    ignored_count: int
    regular_keep: tuple[Path, ...]
    regular_delete: tuple[Path, ...]
    protected_files: tuple[Path, ...]
    ignored_files: tuple[Path, ...]
    estimated_reclaim_bytes: int


@dataclass(frozen=True, slots=True)
class CleanupResult:
    apply: bool
    deleted_count: int
    deleted_bytes: int
    deleted_files: tuple[Path, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


def classify_backup_file(path: Path) -> BackupFileInfo:
    if not path.is_file() or path.suffix.lower() != ".sqlite3":
        return BackupFileInfo(path=path, category="ignored", size=0, mtime=0, reason="not_sqlite_backup")
    name_lower = path.name.lower()
    stat = path.stat()
    if (
        any(keyword in path.name for keyword in PROTECTED_KEYWORDS_CASE_SENSITIVE)
        or any(keyword in name_lower for keyword in PROTECTED_KEYWORDS_CASE_INSENSITIVE)
    ):
        return BackupFileInfo(
            path=path,
            category="protected",
            size=stat.st_size,
            mtime=stat.st_mtime,
            reason="protected_keyword",
        )
    return BackupFileInfo(
        path=path,
        category="regular",
        size=stat.st_size,
        mtime=stat.st_mtime,
        reason="regular_backup",
    )


def build_cleanup_plan(*, backup_dir: Path, keep: int = 5) -> CleanupPlan:
    raw_directory = Path(backup_dir).expanduser()
    try:
        directory = raw_directory.resolve(strict=False)
    except OSError:
        # Some mapped NAS filesystems (for example FUSE/rclone drives on
        # Windows) support normal file I/O but not GetFinalPathNameByHandle,
        # which pathlib.resolve() uses. Keep cleanup available by falling back
        # to an absolute path that does not require that filesystem feature.
        directory = Path(os.path.abspath(raw_directory))
    if keep < 0:
        raise ValueError("keep 不能小于 0")
    if not directory.exists():
        return CleanupPlan(
            backup_dir=directory,
            keep=keep,
            total_sqlite_backups=0,
            regular_count=0,
            protected_count=0,
            ignored_count=0,
            regular_keep=(),
            regular_delete=(),
            protected_files=(),
            ignored_files=(),
            estimated_reclaim_bytes=0,
        )

    classified = [classify_backup_file(path) for path in directory.iterdir()]
    sqlite_backups = [item for item in classified if item.category in {"regular", "protected"}]
    regular = sorted(
        (item for item in classified if item.category == "regular"),
        key=lambda item: item.mtime,
        reverse=True,
    )
    protected = sorted(
        (item for item in classified if item.category == "protected"),
        key=lambda item: item.mtime,
        reverse=True,
    )
    ignored = sorted(
        (item for item in classified if item.category == "ignored"),
        key=lambda item: item.path.name.lower(),
    )

    regular_keep = tuple(item.path for item in regular[:keep])
    regular_delete_items = tuple(regular[keep:])
    regular_delete = tuple(item.path for item in regular_delete_items)

    return CleanupPlan(
        backup_dir=directory,
        keep=keep,
        total_sqlite_backups=len(sqlite_backups),
        regular_count=len(regular),
        protected_count=len(protected),
        ignored_count=len(ignored),
        regular_keep=regular_keep,
        regular_delete=regular_delete,
        protected_files=tuple(item.path for item in protected),
        ignored_files=tuple(item.path for item in ignored),
        estimated_reclaim_bytes=sum(item.size for item in regular_delete_items),
    )


def apply_cleanup_plan(plan: CleanupPlan, *, apply: bool) -> CleanupResult:
    if not apply:
        return CleanupResult(apply=False, deleted_count=0, deleted_bytes=0)

    deleted_files: list[Path] = []
    deleted_bytes = 0
    errors: list[str] = []
    for path in plan.regular_delete:
        for attempt in range(5):
            try:
                size = path.stat().st_size if path.exists() else 0
                path.unlink(missing_ok=False)
                deleted_files.append(path)
                deleted_bytes += size
                break
            except FileNotFoundError:
                break
            except OSError as error:
                if attempt == 4:
                    errors.append(f"{path.name}: {error}")
                else:
                    time.sleep(0.1)
    return CleanupResult(
        apply=True,
        deleted_count=len(deleted_files),
        deleted_bytes=deleted_bytes,
        deleted_files=tuple(deleted_files),
        errors=tuple(errors),
    )


def auto_cleanup_regular_backups(*, backup_dir: Path, keep: int = 5) -> CleanupResult:
    plan = build_cleanup_plan(backup_dir=backup_dir, keep=keep)
    return apply_cleanup_plan(plan, apply=True)


def plan_to_dict(plan: CleanupPlan) -> dict:
    payload = asdict(plan)
    for key in ("backup_dir",):
        payload[key] = str(payload[key])
    for key in ("regular_keep", "regular_delete", "protected_files", "ignored_files"):
        payload[key] = [str(path) for path in getattr(plan, key)]
    return payload


def result_to_dict(result: CleanupResult) -> dict:
    payload = asdict(result)
    payload["deleted_files"] = [str(path) for path in result.deleted_files]
    return payload


def to_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)

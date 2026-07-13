from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload

from app.models.warehouse_inventory import InventoryLot
from app.services.inventory_cost_snapshot import (
    apply_cost_snapshot,
    estimate_inventory_lot_cost,
)


EXPECTED_REVISION = "ai36v7w8x9e26"
COPY_CONFIRMATION = "COPY_ONLY"
FORMAL_DATABASE_NAME = "carton_erp.sqlite3"
DEFAULT_LIVE_DATABASE = PROJECT_ROOT / "data" / FORMAL_DATABASE_NAME


class SafetyError(RuntimeError):
    """Raised when the selected database is not safe for this operation."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_read_only_connection(path: Path) -> sqlite3.Connection:
    """Open SQLite without write permission, including journal-mode changes."""
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
    )
    connection.execute("PRAGMA query_only = ON")
    return connection


def _read_only_engine(path: Path) -> Engine:
    """Give SQLAlchemy a real SQLite read-only connection for dry-runs."""
    return create_engine(
        "sqlite+pysqlite://",
        creator=lambda: _open_read_only_connection(path),
        future=True,
    )


def _apply_engine(path: Path) -> Engine:
    """Open an explicitly approved copy for writes without project-wide side effects."""
    engine = create_engine(
        f"sqlite+pysqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 5},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: sqlite3.Connection, _record: object) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")

    return engine


def _database_revision(path: Path) -> list[str]:
    connection = _open_read_only_connection(path)
    try:
        return [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
    finally:
        connection.close()


def _integrity(path: Path) -> dict:
    connection = _open_read_only_connection(path)
    try:
        return {
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }
    finally:
        connection.close()


def build_report(path: Path, *, apply: bool) -> dict:
    before = {
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
    }
    engine = _apply_engine(path) if apply else _read_only_engine(path)
    candidates: list[dict] = []
    skipped_existing = 0
    missing_inputs = 0
    captured_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(engine) as db:
        lots = list(
            db.scalars(
                select(InventoryLot)
                .options(
                    selectinload(InventoryLot.finished_detail),
                    selectinload(InventoryLot.semi_finished_detail),
                )
                .order_by(InventoryLot.id)
            ).all()
        )
        for lot in lots:
            if lot.estimated_unit_cost_snapshot is not None:
                skipped_existing += 1
                continue
            estimate = estimate_inventory_lot_cost(db, lot)
            if estimate is None:
                missing_inputs += 1
                continue
            candidates.append(
                {
                    "lot_id": lot.id,
                    "lot_number": lot.lot_number,
                    "inventory_type": lot.inventory_type,
                    "estimated_unit_cost": str(estimate.unit_cost),
                    "square_price": str(estimate.square_price),
                    "area_m2": str(estimate.area_m2),
                    "source": estimate.source,
                }
            )
            if apply:
                apply_cost_snapshot(lot, estimate, captured_at=captured_at)
        if apply:
            db.commit()
        else:
            db.rollback()
    engine.dispose()
    after = {
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
    }
    return {
        "database": str(path),
        "mode": "apply" if apply else "dry-run",
        "database_written": apply and before != after,
        "database_fingerprint_unchanged": before == after,
        "before": before,
        "after": after,
        "candidate_count": len(candidates),
        "skipped_existing_snapshot": skipped_existing,
        "missing_cost_inputs": missing_inputs,
        "candidates": candidates,
        "integrity": _integrity(path),
    }


def _known_live_databases() -> tuple[Path, ...]:
    configured = os.getenv("ERP_DATABASE_PATH")
    paths = [DEFAULT_LIVE_DATABASE]
    if configured:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            configured_path = PROJECT_ROOT / configured_path
        paths.append(configured_path)
    return tuple(path.resolve(strict=False) for path in paths)


def _contains_no_symlink(path: Path, root: Path) -> bool:
    """Reject a target path that traverses a link below its approved root."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    if current.is_symlink():
        return False
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _same_file_as_known_live(path: Path) -> bool:
    for live_database in _known_live_databases():
        try:
            if live_database.is_file() and path.samefile(live_database):
                return True
        except OSError:
            continue
    return False


def validate_database_safety(
    *,
    database: Path,
    apply: bool,
    confirm_copy: str | None,
    copy_root: Path | None,
) -> Path:
    """Validate an input database and return its resolved, safe filesystem path."""
    raw_path = database.expanduser()
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    raw_path = Path(os.path.abspath(raw_path))
    if raw_path.name.casefold() == FORMAL_DATABASE_NAME:
        raise SafetyError(f"refusing to target a database named {FORMAL_DATABASE_NAME}")
    if not raw_path.is_file():
        raise SafetyError(f"database does not exist: {raw_path}")
    if _same_file_as_known_live(raw_path):
        raise SafetyError("refusing to target a known live database")

    if not apply:
        return raw_path.resolve()
    if confirm_copy != COPY_CONFIRMATION:
        raise SafetyError(f"--apply requires --confirm-copy {COPY_CONFIRMATION}")
    if copy_root is None:
        raise SafetyError("--apply requires --copy-root for the approved copy directory")

    raw_root = copy_root.expanduser()
    if not raw_root.is_absolute():
        raw_root = Path.cwd() / raw_root
    raw_root = Path(os.path.abspath(raw_root))
    if not raw_root.is_dir() or raw_root.is_symlink():
        raise SafetyError("--copy-root must be an existing non-symlink directory")
    root = raw_root.resolve()
    if not _contains_no_symlink(raw_path, raw_root):
        raise SafetyError("database must be inside --copy-root and must not traverse symlinks")
    path = raw_path.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise SafetyError("database must resolve inside --copy-root") from error
    if path.stat().st_nlink != 1:
        raise SafetyError("database must not have multiple hard links")
    if _same_file_as_known_live(path):
        raise SafetyError("refusing to target a known live database")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or backfill inventory material-cost snapshots on a database copy."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-copy")
    parser.add_argument(
        "--copy-root",
        type=Path,
        help="Existing directory that contains the explicitly approved database copy.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        path = validate_database_safety(
            database=args.database,
            apply=args.apply,
            confirm_copy=args.confirm_copy,
            copy_root=args.copy_root,
        )
    except SafetyError as error:
        parser.error(str(error))
    revisions = _database_revision(path)
    if revisions != [EXPECTED_REVISION]:
        parser.error(
            f"database revision must be {EXPECTED_REVISION}; current={revisions}"
        )

    report = build_report(path, apply=args.apply)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

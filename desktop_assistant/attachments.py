"""Rebind the known absolute PDF path column in a PRIVATE imported/restored copy."""
from contextlib import closing
from pathlib import Path
import sqlite3

from desktop_assistant.storage import sha
from desktop_assistant.source_paths import relative_source_path


def rebind_pdf_sources(database: Path, old_root: str | Path, files_root: Path, recorded_root: Path, *, importing=False) -> int:
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=rw', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_order_training_samples'").fetchone():
            return 0
        rows = db.execute("SELECT id,file_path,file_sha256 FROM pdf_order_training_samples WHERE file_path IS NOT NULL AND trim(file_path)<>''").fetchall()
        changes = []
        for identity, raw, expected in rows:
            try:
                relative = relative_source_path(raw, old_root)
            except ValueError:
                raise ValueError(f'PDF源不在完整备份目录内，需工厂先整理：样本{identity}') from None
            if importing:
                for original, managed in (("static/uploads", "legacy_uploads"), ("factory_twin/data", "factory_twin_data")):
                    if relative.is_relative_to(original):
                        relative = Path(managed) / relative.relative_to(original)
                        break
            actual = files_root / relative
            if (not actual.resolve().is_relative_to(files_root.resolve())
                    or not actual.is_file() or sha(actual) != expected):
                raise ValueError(f'PDF附件缺失或校验失败：样本{identity}')
            changes.append((str(recorded_root / relative), identity))
        with db:
            db.executemany('UPDATE pdf_order_training_samples SET file_path=? WHERE id=?', changes)
        return len(changes)

"""Produce a non-destructive, derived normalization index for historical material text.

The source archive is always opened SQLite read-only and is never changed.  A
derived CSV is deliberately an auxiliary artifact: consumers may use a
``derived_material_code`` only when its decision is ``format_safe``.  Missing
or ambiguous flute evidence remains pending instead of being guessed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


VALID_FLUTES: dict[int, frozenset[str]] = {
    3: frozenset({"A", "B", "E"}),
    5: frozenset({"AB", "BE"}),
    7: frozenset({"AAA", "ABC"}),
}
MATERIAL_WITH_FLUTE = re.compile(
    r"^(?P<base>[A-Z0-9]{3}|[A-Z0-9]{5}|[A-Z0-9]{7})\s*(?:/|-)\s*"
    r"(?P<flute>AAA|ABC|AB|BE|A|B|E)\s*(?:瓦楞|楞|瓦)?$"
)
BARE_MATERIAL = re.compile(r"^[A-Z0-9]{3}$|^[A-Z0-9]{5}$|^[A-Z0-9]{7}$")


@dataclass(frozen=True, slots=True)
class Decision:
    source_table: str
    source_row_id: int
    source_material_code: str
    derived_material_code: str
    decision_status: str
    decision_reason: str
    source_workbook: str
    source_sheet: str
    source_row: int


def _fingerprint(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    stat = path.stat()
    return {
        "sha256": digest.hexdigest(),
        "size_bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def decide(raw_value: object) -> tuple[str, str, str]:
    """Return (derived_code, status, reason), without inferring absent evidence."""

    raw = unicodedata.normalize("NFKC", str(raw_value or "")).strip().upper()
    compact = re.sub(r"\s+", "", raw).replace("／", "/")
    match = MATERIAL_WITH_FLUTE.fullmatch(compact)
    if match:
        base, flute = match.group("base"), match.group("flute")
        if flute in VALID_FLUTES[len(base)]:
            return f"{base}/{flute}", "format_safe", "仅统一分隔符、空白或楞型后缀；不改变纸板语义"
        return "", "pending_invalid_layer_flute", "代码层数与楞型不匹配；禁止猜测替代楞型"
    if BARE_MATERIAL.fullmatch(compact):
        return "", "pending_missing_flute", "原始文本没有楞型证据；禁止补写 A/B/E 或 AB/BE"
    return "", "pending_ambiguous", "复合、非标准或不可解析文本；保留原始档案"


def build_decisions(connection: sqlite3.Connection) -> list[Decision]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT id, material_code, source_workbook, source_sheet, source_row
        FROM historical_requisition_maps
        ORDER BY id
        """
    ).fetchall()
    decisions: list[Decision] = []
    for row in rows:
        derived, status, reason = decide(row["material_code"])
        decisions.append(
            Decision(
                source_table="historical_requisition_maps",
                source_row_id=int(row["id"]),
                source_material_code=str(row["material_code"]),
                derived_material_code=derived,
                decision_status=status,
                decision_reason=reason,
                source_workbook=str(row["source_workbook"]),
                source_sheet=str(row["source_sheet"]),
                source_row=int(row["source_row"]),
            )
        )
    return decisions


def _write_csv(path: Path, decisions: list[Decision]) -> None:
    fields = list(Decision.__dataclass_fields__)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(decision) for decision in decisions)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only historical-material derived-normalization export."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    output_dir = args.output_dir.resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在：{database}")
    output_dir.mkdir(parents=True, exist_ok=True)
    before = _fingerprint(database)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        decisions = build_decisions(connection)
    after = _fingerprint(database)
    if before != after:
        raise RuntimeError("只读导出期间数据库指纹变化；拒绝发布结果")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"HISTORICAL_MATERIAL_DERIVED_{stamp}.csv"
    json_path = output_dir / f"HISTORICAL_MATERIAL_DERIVED_{stamp}.json"
    _write_csv(csv_path, decisions)
    summary = {
        "mode": "read_only_derived_export",
        "source_database": str(database),
        "database_before": before,
        "database_after": after,
        "integrity_check": integrity,
        "foreign_key_errors": foreign_key_errors,
        "total_rows": len(decisions),
        "statuses": dict(sorted(Counter(d.decision_status for d in decisions).items())),
        "derived_csv": str(csv_path),
        "raw_archive_modified": False,
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

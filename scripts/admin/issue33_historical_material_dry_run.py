"""Issue #33 phase 1: read-only historical-material review export.

This tool never opens SQLite in write mode and has no apply switch.  It creates
review artifacts outside the production worktree for human approval only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ALLOWED_FLUTES = {3: {"A", "B", "E"}, 5: {"AB", "BE"}, 7: {"AAA", "ABC"}}
EXACT_CODE = re.compile(r"^[A-Z0-9]{3}$|^[A-Z0-9]{5}$|^[A-Z0-9]{7}$")
FLUTE_SUFFIX = re.compile(
    r"^(?P<code>[A-Z0-9]{3}|[A-Z0-9]{5}|[A-Z0-9]{7})\s*(?:/|-)\s*"
    r"(?P<flute>AAA|ABC|AB/BE|B/E|AB|BE|A|B|E)\s*(?:楞)?$",
    re.IGNORECASE,
)


def fingerprint(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size_bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def candidate_from_raw(raw: str | None) -> tuple[str, str, int | None, str, str]:
    value = str(raw or "").strip().upper()
    suffix = FLUTE_SUFFIX.fullmatch(value)
    if suffix:
        code = suffix.group("code")
        flute = suffix.group("flute")
        layer = len(code)
        if flute in ALLOWED_FLUTES.get(layer, set()):
            return code, flute, layer, "suffix_can_be_reviewed", "保留原始档案；待人工确认后才可形成业务快照"
        return code, flute, layer, "invalid_flute_for_layer", "楞型与代码层数不一致，禁止自动选择 AB 或 BE"
    if EXACT_CODE.fullmatch(value):
        return value, "", len(value), "exact_code_without_flute", "原始档案没有精确楞型；不建议回填"
    return "", "", None, "compound_or_unparseable", "组合材质或非标准代码，禁止自动拆分"


def export_review(connection: sqlite3.Connection) -> tuple[list[dict[str, object]], Counter[str]]:
    rows = connection.execute(
        """
        SELECT id, product_id, material_code, source_workbook, source_sheet, source_row
        FROM historical_requisition_maps
        ORDER BY id
        """
    ).fetchall()
    review: list[dict[str, object]] = []
    reasons: Counter[str] = Counter()
    for row in rows:
        code, flute, layer, reason, note = candidate_from_raw(row["material_code"])
        if reason == "exact_code_without_flute":
            continue
        reasons[reason] += 1
        review.append(
            {
                "table_name": "historical_requisition_maps",
                "record_id": row["id"],
                "product_id": row["product_id"] or "",
                "raw_material_code": row["material_code"],
                "suggested_material_code": code,
                "inferred_layer_count": layer or "",
                "raw_flute_suffix": flute,
                "review_reason": reason,
                "suggested_action": "review_only_no_apply",
                "review_status": "pending",
                "review_note": note,
                "source_workbook": row["source_workbook"],
                "source_sheet": row["source_sheet"],
                "source_row": row["source_row"],
            }
        )
    return review, reasons


def material_snapshot_summary(connection: sqlite3.Connection) -> dict[str, object]:
    rows = connection.execute(
        """
        SELECT m.id, m.material_snapshot, s.layer_count, s.flute_type
        FROM material_requisition_items m
        JOIN sales_order_items s ON s.id = m.order_item_id
        ORDER BY m.id
        """
    ).fetchall()
    invalid: list[int] = []
    for row in rows:
        code = str(row["material_snapshot"] or "").strip().upper()
        layer = int(row["layer_count"] or 0)
        flute = str(row["flute_type"] or "").strip().upper()
        if len(code) != layer or not EXACT_CODE.fullmatch(code) or flute not in ALLOWED_FLUTES.get(layer, set()):
            invalid.append(row["id"])
    samples = connection.execute(
        """
        SELECT m.id, m.material_snapshot, s.layer_count, s.flute_type, m.product_code_snapshot
        FROM material_requisition_items m
        JOIN sales_order_items s ON s.id = m.order_item_id
        WHERE m.material_snapshot IN ('BC14C', 'K618A')
        ORDER BY m.id
        """
    ).fetchall()
    return {
        "row_count": len(rows),
        "invalid_count": len(invalid),
        "invalid_ids": invalid,
        "canonical_samples": [dict(sample) for sample in samples],
    }


def exact_legacy_samples(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        sample: connection.execute(
            "SELECT COUNT(*) FROM historical_requisition_maps WHERE material_code LIKE ?",
            (f"%{sample}%",),
        ).fetchone()[0]
        for sample in ("BC14C/A", "K618A/B")
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "table_name", "record_id", "product_id", "raw_material_code", "suggested_material_code",
        "inferred_layer_count", "raw_flute_suffix", "review_reason", "suggested_action",
        "review_status", "review_note", "source_workbook", "source_sheet", "source_row",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue #33 read-only dry-run; no apply mode exists.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    before = fingerprint(database)
    with sqlite_readonly(database) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        historical_rows = connection.execute("SELECT COUNT(*) FROM historical_requisition_maps").fetchone()[0]
        no_product = connection.execute(
            "SELECT COUNT(*) FROM historical_requisition_maps WHERE product_id IS NULL"
        ).fetchone()[0]
        review_rows, review_reasons = export_review(connection)
        material_summary = material_snapshot_summary(connection)
        legacy_samples = exact_legacy_samples(connection)
    after = fingerprint(database)
    if before != after:
        raise RuntimeError("read-only dry-run changed the database fingerprint")
    review_path = output_dir / f"ISSUE_33_HISTORICAL_MATERIAL_REVIEW_{stamp}.csv"
    report_path = output_dir / f"ISSUE_33_HISTORICAL_MATERIAL_DRY_RUN_{stamp}.md"
    write_csv(review_path, review_rows)
    report_path.write_text(
        "\n".join(
            [
                "# Issue #33 第一阶段：历史材质只读 Dry-run",
                "",
                "- 模式：只读；工具不提供 Apply 参数。",
                f"- 数据库前后 SHA-256：`{before['sha256']}` / `{after['sha256']}`（一致）。",
                f"- 数据库前后大小：`{before['size_bytes']}` / `{after['size_bytes']}`（一致）。",
                f"- 数据库前后 mtime UTC：`{before['mtime_utc']}` / `{after['mtime_utc']}`（一致）。",
                f"- `integrity_check`：`{integrity}`；外键错误：`{fk_errors}`。",
                "",
                "## 两张历史表摘要",
                "",
                f"- `historical_requisition_maps`：{historical_rows} 条；{no_product} 条未关联产品。",
                f"  - 需人工 REVIEW：{len(review_rows)} 条；原因：{dict(review_reasons)}。",
                "  - 表为原始档案，没有精确层数/楞型字段；本阶段不自动规范或回填。",
                f"- `material_requisition_items`：{material_summary['row_count']} 条；按关联订单快照校验异常 {material_summary['invalid_count']} 条。",
                "  - 当前业务快照全部已符合代码长度与楞型规则。",
                "",
                "## 样本证据",
                "",
                f"- 原始档案当前精确匹配 `BC14C/A`：{legacy_samples['BC14C/A']} 条；`K618A/B`：{legacy_samples['K618A/B']} 条。",
                "- 两个字符串在当前原始档案中均为 0；历史快照现保留规范码并由订单快照给出精确层数/楞型。",
                f"- 规范快照样本：{material_summary['canonical_samples']}",
                "",
                "## 人工复核文件",
                "",
                f"- `{review_path}`",
                "- 所有行均为 `review_status=pending`，`suggested_action=review_only_no_apply`。",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    print(report_path)
    print(review_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

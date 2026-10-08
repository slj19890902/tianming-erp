"""Read-only inventory of legacy cutting semantics; never applies a migration."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.requisition_quantities import cutting_factor, normalize_cutting_mode


PRODUCT_FIELDS = (
    "id", "product_code", "box_category", "box_style", "production_process",
    "mold_tool_id", "default_cutting_mode", "report_length_mm", "report_width_mm",
    "base_report_length_mm", "base_report_width_mm", "splice_mode", "pieces_per_box",
    "version", "is_active", "supply_mode", "is_virtual_composite_parent",
)


def classify_product(row: dict, mold_ids: set[int], bom_yields: set[int]) -> dict:
    result = {"before": dict(row), "proposed": None, "review_reasons": []}
    reasons = result["review_reasons"]
    processes = {part.strip() for part in re.split(r"[,，、;；]", row["production_process"] or "")}
    die_cut = "模切" in processes
    try:
        old_mode = normalize_cutting_mode(row["default_cutting_mode"], strict=True)
        old_factor = cutting_factor(old_mode)
    except ValueError:
        reasons.append("旧开料方式无效")
        old_factor = None
    if row["supply_mode"] == "external_purchase" or row["is_virtual_composite_parent"] or row["box_style"] == "BOM组合":
        result["status"] = "no_own_sheet"
        return result
    if row["box_category"] == "die_cut" and not die_cut:
        reasons.append("类别为模切但生产工艺未登记模切")
    if die_cut and row["mold_tool_id"] not in mold_ids:
        reasons.append("模切工艺缺少有效模具绑定")
    if not die_cut and old_factor is not None and old_factor > 1:
        reasons.append("非模切的一开多含义待核对，不能直接转为模数")
    if die_cut and old_factor is not None and any(value != old_factor for value in bom_yields):
        reasons.append("BOM登记出数与旧开料出数不一致")
    if any(row[field] is None or row[field] <= 0 for field in ("report_length_mm", "report_width_mm")):
        reasons.append("理论报料尺寸尚未完整登记")
    result["is_die_cut_by_process"] = die_cut
    result["bom_registered_yields"] = sorted(bom_yields)
    result["status"] = "needs_review" if reasons else "candidate"
    if not reasons:
        result["proposed"] = {
            "length_parts": 1, "width_parts": 1,
            "mold_count": old_factor if die_cut else 1,
            "cutting_mode": "一开一",
            "theoretical_length_mm": row["report_length_mm"],
            "theoretical_width_mm": row["report_width_mm"],
        }
    return result


def audit(database: Path) -> dict:
    source = database.resolve(strict=True)
    if not source.is_file():
        raise ValueError("数据库路径须为现有文件")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")  # One short consistent read snapshot, released on close.
        revision = [r[0] for r in db.execute("SELECT version_num FROM alembic_version")]
        molds = {r[0] for r in db.execute("SELECT id FROM mold_tools")}
        yields: dict[int, set[int]] = {}
        for row in db.execute("SELECT component_product_id, mold_max_yield_per_sheet FROM product_bom_components WHERE is_die_cut=1 AND mold_max_yield_per_sheet IS NOT NULL"):
            yields.setdefault(row[0], set()).add(row[1])
        rows = [classify_product(dict(r), molds, yields.get(r["id"], set()))
                for r in db.execute("SELECT " + ",".join(PRODUCT_FIELDS) + " FROM products ORDER BY id")]
    return {
        "audit_only": True, "business_data_written": False,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": str(source), "revision": revision,
        "total": len(rows), "statuses": dict(Counter(r["status"] for r in rows)),
        "review_reasons": dict(Counter(reason for r in rows for reason in r["review_reasons"])),
        "products": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = args.database.resolve(strict=True)
    output = args.output.resolve()
    # Evidence cannot overwrite the source, a SQLite sidecar or another DB.
    if output == source or output.name.startswith(source.name) or output.suffix.lower() != ".json":
        parser.error("审计输出必须是独立 JSON 文件，不能覆盖数据库或其附属文件")
    result = audit(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in result.items() if key not in {"products", "database"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.admin import import_yl_yke_kew_common_boxes as base


EXPECTED_PACKAGE_ID = "YL_20260805_SUPPLEMENT_28"
EXPECTED_ROW_COUNT = 28
EXPECTED_BOX_COUNTS = {"normal": 27, "die_cut": 1}
EXPECTED_CUSTOMER = {
    "customer_code": "YL",
    "name": "苏州工业园区驿力机车科技有限公司",
    "customer_number": 131,
    "id": 136,
}
EXPECTED_SCHEMA_REVISION = "dk93v8x9z82"
FORMAL_DATABASE = Path(
    r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3"
).resolve()
REHEARSAL_CONFIRMATION = "APPLY_YL_SUPPLEMENT_REHEARSAL"
FORMAL_CONFIRMATION = "APPLY_YL_SUPPLEMENT_FORMAL_20260805"
REHEARSAL_SOURCE = "controlled_import.yl_20260805_supplement_rehearsal"
FORMAL_SOURCE = "controlled_import.yl_20260805_supplement_formal"
REHEARSAL_REASON = (
    "老板于2026-08-05要求补导YL常用箱；仅在隔离副本演练28条完整记录"
)
FORMAL_REASON = (
    "老板于2026-08-05明确要求把YL也导入常用箱；"
    "按最终核对表仅导入28条字段完整记录"
)
VALID_CUTTING_MODES = {"一开一", "一开二", "一开三", "一开四", "一开五", "一开六"}
VALID_FLUTES_BY_LAYER = {
    3: {"A", "B", "E"},
    5: {"AB", "BE"},
    7: {"AAA", "ABC"},
}
EXISTING_UNCHANGED_CODES = {
    "Z.001.000001",
    "Z.001.000002",
    "Z.001.000003",
}
ERP_CREASE_TYPE_OVERRIDES = {
    # YL source row 54 records explicit three-part pressure-line dimensions for a
    # die-cut inner box.  The ERP rule does not expose "压线" for that box type;
    # store it as the traceable manual category "其他" and retain the original
    # pressure-line values and source wording in the report fields.
    "Z.001.000130": "其他",
}


def _erp_crease_type(row: dict[str, Any]) -> str | None:
    code = str(row.get("product_code") or "").strip()
    override = ERP_CREASE_TYPE_OVERRIDES.get(code)
    if override is not None:
        if row.get("box_style") != "模切内盒" or row.get("crease_type") != "压线":
            raise RuntimeError(f"YL压线归类证据已变化，拒绝自动转换：{code}")
        return override
    return str(row.get("crease_type") or "").strip() or None


def load_manifest(
    path: Path,
    expected_sha256: str | None,
) -> tuple[dict[str, Any], str]:
    actual_sha256 = base.sha256_file(path)
    if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
        raise RuntimeError("清单 SHA-256 已变化，拒绝执行")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("package_id") != EXPECTED_PACKAGE_ID:
        raise RuntimeError("清单 package_id 不符合 YL 补导批次")
    if payload.get("safety", {}).get("formal_database_write_allowed") is not False:
        raise RuntimeError("清单未明确禁止直接写正式库")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != EXPECTED_ROW_COUNT:
        raise RuntimeError(f"YL 补导清单必须恰好包含 {EXPECTED_ROW_COUNT} 条")
    source = payload.get("source_workbook", {})
    source_path = Path(str(source.get("path") or ""))
    source_sha256 = str(source.get("sha256") or "")
    if not source_path.is_file() or base.sha256_file(source_path).lower() != source_sha256.lower():
        raise RuntimeError("最终核对表不存在或 SHA-256 已变化，拒绝执行")
    return payload, actual_sha256


def _validate_manifest_shape(rows: list[dict[str, Any]]) -> None:
    if len(rows) != EXPECTED_ROW_COUNT:
        raise RuntimeError(f"YL 补导条数不符合冻结范围：{len(rows)}")
    keys: set[str] = set()
    box_counts = {key: 0 for key in EXPECTED_BOX_COUNTS}
    for row in rows:
        code = str(row.get("product_code") or "").strip()
        if row.get("customer_code") != "YL":
            raise RuntimeError(f"清单含计划外客户：{row.get('customer_code')}")
        if not code or code in keys or code in EXISTING_UNCHANGED_CODES:
            raise RuntimeError(f"产品编码为空、重复或命中既有保护记录：{code}")
        keys.add(code)
        if not str(row.get("product_name") or "").strip():
            raise RuntimeError(f"产品名称为空：{code}")
        category = str(row.get("box_category") or "")
        if category not in box_counts:
            raise RuntimeError(f"非法箱类：{code}/{category}")
        box_counts[category] += 1
        if row.get("review_status") not in {"可预填", "待确认"}:
            raise RuntimeError(f"核对状态不在冻结允许值：{code}")
        if not row.get("is_active") or row.get("requires_manual_confirmation"):
            raise RuntimeError(f"YL 本批28条必须为已确认启用记录：{code}")
        if row.get("image_flag") != "否":
            raise RuntimeError(f"YL 图片标记与冻结证据不一致：{code}")
        if row.get("default_cutting_mode") not in VALID_CUTTING_MODES:
            raise RuntimeError(f"开料方式非法：{code}")
        if not row.get("material_id") or not row.get("material_code") or not row.get("material_supplier"):
            raise RuntimeError(f"材质映射不完整：{code}")
        if base._normalize_decimal(row.get("sale_unit_price")) is None:
            raise RuntimeError(f"天明含税单价为空：{code}")
        if row.get("sale_unit_price_no_tax") is not None:
            raise RuntimeError(f"YL 本批不得把含税价写入未税价：{code}")
        if not row.get("report_length_mm") or not row.get("report_width_mm"):
            raise RuntimeError(f"报料长宽不完整：{code}")
        layer_count = int(row["layer_count"])
        flute = str(row.get("flute_type") or "").strip() or None
        if flute is not None and flute not in VALID_FLUTES_BY_LAYER.get(layer_count, set()):
            raise RuntimeError(f"楞型与层数不一致：{code}/{layer_count}/{flute}")
        if category == "normal" and any(
            base._normalize_decimal(row.get(field)) is None
            for field in ("length_mm", "width_mm", "height_mm")
        ):
            raise RuntimeError(f"普通箱三维尺寸不完整：{code}")
        base.normalize_box_configuration(
            box_style=row.get("box_style"),
            splice_mode=row.get("splice_mode"),
            pieces_per_box=row.get("pieces_per_box"),
            flap_mm=row.get("flap_mm"),
            default_cutting_mode=row.get("default_cutting_mode"),
            crease_type=_erp_crease_type(row),
        )
        if row.get("box_style") == "A1/0201 普通开槽箱":
            creases = [
                int(row["crease_left_mm"]),
                int(row["crease_middle_mm"]),
                int(row["crease_right_mm"]),
            ]
            if sum(creases) != int(row["report_width_mm"]):
                raise RuntimeError(f"A1压线合计不等于报料宽：{code}")
            length = int(row["length_mm"])
            width = int(row["width_mm"])
            expected_length = (
                length + width + 35
                if row.get("splice_mode") == "double"
                else 2 * (length + width) + 35
            )
            if int(row["report_length_mm"]) != expected_length:
                raise RuntimeError(f"A1报料长与单/双拼公式不一致：{code}")
            if row.get("flap_mm") != 35:
                raise RuntimeError(f"YL A1舌头必须沿用现有35mm口径：{code}")
            expected_pieces = 2 if row.get("splice_mode") == "double" else 1
            if row.get("pieces_per_box") != expected_pieces:
                raise RuntimeError(f"A1单/双拼片数不一致：{code}")
    if box_counts != EXPECTED_BOX_COUNTS:
        raise RuntimeError(f"箱类条数不符合冻结范围：{box_counts}")


def _customer_master(payload: dict[str, Any]) -> list[dict[str, Any]]:
    customers = payload.get("scope", {}).get("customers")
    if not isinstance(customers, list) or len(customers) != 1:
        raise RuntimeError("YL 补导清单必须恰好包含一个既有客户")
    row = customers[0]
    normalized = {
        "customer_code": str(row.get("code") or "").strip(),
        "name": str(row.get("name") or "").strip(),
        "customer_number": row.get("customer_number"),
    }
    expected = {key: EXPECTED_CUSTOMER[key] for key in normalized}
    if normalized != expected:
        raise RuntimeError(f"YL 客户主档冻结值不一致：{normalized}")
    return [normalized]


_original_target_product_values = base._target_product_values


def _target_product_values(
    row: dict[str, Any],
    *,
    customer_id: int,
    source: str = REHEARSAL_SOURCE,
) -> dict[str, Any]:
    normalized_row = dict(row)
    normalized_row["crease_type"] = _erp_crease_type(row)
    values = _original_target_product_values(
        normalized_row,
        customer_id=customer_id,
        source=source,
    )
    configuration = base.normalize_box_configuration(
        box_style=row.get("box_style"),
        splice_mode=row.get("splice_mode"),
        pieces_per_box=row.get("pieces_per_box"),
        flap_mm=row.get("flap_mm"),
        default_cutting_mode=row.get("default_cutting_mode"),
        crease_type=normalized_row.get("crease_type"),
    )
    values.update(
        box_style=configuration["box_style"],
        splice_mode=configuration["splice_mode"],
        pieces_per_box=configuration["pieces_per_box"],
        flap_mm=configuration["flap_mm"],
        default_cutting_mode=configuration["default_cutting_mode"],
        crease_type=configuration["crease_type"],
        remark=(
            f"{source}；源表=YL；源Excel行={row['source_excel_row']}；"
            f"最终核对表行={row['review_excel_row']}；"
            f"核对状态={row['review_status']}"
            + (
                "；源压线类型=压线，ERP按模切内盒规则归类为其他"
                if row["product_code"] in ERP_CREASE_TYPE_OVERRIDES
                else ""
            )
        ),
    )
    return values


_original_build_plan = base.build_plan


def build_plan(
    db: Session,
    payload: dict[str, Any],
    *,
    source: str = REHEARSAL_SOURCE,
) -> dict[str, Any]:
    plan = _original_build_plan(db, payload, source=source)
    customer_item = plan["customer_items"][0]
    if (
        plan["customer_create_count"] != 0
        or customer_item.get("status") != "already_applied"
        or customer_item.get("id") != EXPECTED_CUSTOMER["id"]
    ):
        raise RuntimeError("YL 补导只能挂到正式既有客户 ID 136，不得创建或改绑客户")
    return plan


base.EXPECTED_PACKAGE_ID = EXPECTED_PACKAGE_ID
base.EXPECTED_SCHEMA_REVISION = EXPECTED_SCHEMA_REVISION
base.FORMAL_DATABASE = FORMAL_DATABASE
base.REHEARSAL_CONFIRMATION = REHEARSAL_CONFIRMATION
base.FORMAL_CONFIRMATION = FORMAL_CONFIRMATION
base.REHEARSAL_SOURCE = REHEARSAL_SOURCE
base.FORMAL_SOURCE = FORMAL_SOURCE
base.REHEARSAL_REASON = REHEARSAL_REASON
base.FORMAL_REASON = FORMAL_REASON
base.EXPECTED_COUNTS = {"YL": EXPECTED_ROW_COUNT}
base.EXPECTED_CUSTOMER_NUMBERS = {"YL": EXPECTED_CUSTOMER["customer_number"]}
base.EXPECTED_BOX_COUNTS = EXPECTED_BOX_COUNTS
base.load_manifest = load_manifest
base._validate_manifest_shape = _validate_manifest_shape
base._customer_master = _customer_master
base._target_product_values = _target_product_values
base.build_plan = build_plan


def main() -> int:
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())

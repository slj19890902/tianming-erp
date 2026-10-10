from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.master_data_common import audit_master_change  # noqa: E402
from app.core.database import create_sqlite_engine  # noqa: E402
from app.models.audit import OperationLog  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.material import Material  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.product_drawing import ProductDrawing  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.box_type_rules import normalize_box_configuration  # noqa: E402
from app.services.flute_mapping import validate_flute_for_write  # noqa: E402
from app.services.master_data_versioning import (  # noqa: E402
    apply_versioned_update,
    record_versioned_create,
)


BATCH_ID = "YKE_KEW_SAMPLE_SYNC_20260807"
CONFIRMATION = "APPLY_YKE_KEW_SAMPLE_SYNC_20260807"
EXPECTED_SCHEMA_REVISION = "dl94v8x9z83"
EXPECTED_SAMPLE_COUNT = 163
EXPECTED_SOURCE_IMAGE_COUNT = 211
EXPECTED_PRIOR_SAMPLE_COUNT = 104
EXPECTED_PRIOR_PRODUCT_COUNT = 131
EXPECTED_NEW_PRODUCT_COUNT = 35
SOURCE = "controlled_import.yke_kew_sample_sync_20260807"
REASON = "老板于2026-08-07要求：四卷样品图片及楞型、成型、印刷、颜色和结合方式与常用箱逐项一致"
KNOWN_PROCESSES = (
    "印刷",
    "开槽",
    "模切",
    "成型待确认",
    "打钉",
    "粘合",
    "二次粘合",
)
KEEP_CURRENT = "__KEEP_CURRENT__"
PRODUCT_UPDATE_FIELDS = (
    "flute_type",
    "box_category",
    "box_style",
    "print_content",
    "printing_colors",
    "production_process",
)
WATCH_TABLES = (
    "products",
    "product_drawings",
    "master_data_object_versions",
    "operation_logs",
    "sales_orders",
    "sales_order_items",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    "inventory_lots",
    "finished_goods_lots",
    "delivery_orders",
    "purchase_orders",
)
PROTECTED_TABLES = (
    "sales_orders",
    "sales_order_items",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    "inventory_lots",
    "finished_goods_lots",
    "delivery_orders",
    "purchase_orders",
)
SOURCE_ROW_OVERRIDES = {("YP133", "YKE"): 75}


@dataclass(frozen=True, slots=True)
class DatabaseChecks:
    sha256: str
    integrity_check: str
    foreign_key_violations: int
    alembic_revision: str | None
    counts: dict[str, int]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _readonly(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro", uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def database_checks(database: Path) -> DatabaseChecks:
    with closing(_readonly(database)) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        revision_row = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        counts = {
            table: int(
                connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            )
            for table in WATCH_TABLES
            if table in tables
        }
    return DatabaseChecks(
        sha256=sha256_file(database),
        integrity_check=integrity,
        foreign_key_violations=foreign_keys,
        alembic_revision=str(revision_row[0]) if revision_row else None,
        counts=counts,
    )


def _process_tokens(value: str | None) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、]", value or "") if item.strip()]


def desired_sample_fields(sample: dict[str, Any]) -> dict[str, Any]:
    printed = str(sample.get("printed") or "").strip() == "是"
    forming = str(sample.get("forming") or "").strip()
    joining = str(sample.get("joining") or "").strip()
    selected: list[str] = []
    if printed:
        selected.append("印刷")
    if forming == "不确定":
        selected.append("成型待确认")
    elif forming != "无需":
        selected.append(forming)
    if joining != "无需结合":
        selected.append(joining)
    if str(sample.get("secondary_gluing") or "").strip() == "是":
        selected.append("二次粘合")
    if forming == "模切":
        box_category, requested_style = "die_cut", "模切内盒"
    elif forming == "开槽":
        box_category, requested_style = "normal", "A1"
    elif forming == "无需":
        box_category, requested_style = "normal", None
    elif forming == "不确定":
        box_category, requested_style = KEEP_CURRENT, KEEP_CURRENT
    else:
        raise ValueError(f"非法成型方式：{forming!r}")
    if requested_style == KEEP_CURRENT:
        box_style = KEEP_CURRENT
    else:
        configuration = normalize_box_configuration(
            box_style=requested_style,
            splice_mode="single",
            pieces_per_box=None,
            flap_mm=None,
            default_cutting_mode="一开一",
            crease_type="压线" if forming == "开槽" else "净料",
        )
        box_style = configuration["box_style"]
    color = str(sample.get("printing_colors") or "").strip()
    return {
        "flute_type": str(sample.get("flute_type") or "").strip().upper() or None,
        "box_category": box_category,
        "box_style": box_style,
        "print_content": "印刷" if printed else "无印刷",
        "printing_colors": color if printed and color not in {"", "无"} else None,
        "production_process": "、".join(dict.fromkeys(selected)) or None,
    }


def merge_sample_fields(
    rows: list[dict[str, Any]],
    *,
    existing_process: str | None,
    existing_box_category: str,
    existing_box_style: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not rows:
        return None, ["缺少样品字段"]
    desired_rows = [desired_sample_fields(row) for row in rows]
    errors: list[str] = []
    merged = dict(desired_rows[0])
    for field in PRODUCT_UPDATE_FIELDS:
        if field == "production_process":
            continue
        values = {
            json.dumps(row[field], ensure_ascii=False)
            for row in desired_rows
            if row[field] != KEEP_CURRENT
        }
        if len(values) > 1:
            errors.append(f"同一常用箱的多个样品字段冲突：{field}")
        elif len(values) == 1 and merged[field] == KEEP_CURRENT:
            merged[field] = json.loads(next(iter(values)))
    if merged["box_category"] == KEEP_CURRENT:
        merged["box_category"] = existing_box_category
    if merged["box_style"] == KEEP_CURRENT:
        merged["box_style"] = existing_box_style
    selected: list[str] = []
    for desired in desired_rows:
        selected.extend(_process_tokens(desired["production_process"]))
    selected = list(dict.fromkeys(selected))
    unknown = [
        token
        for token in _process_tokens(existing_process)
        if token not in KNOWN_PROCESSES and token not in selected
    ]
    merged["production_process"] = "、".join(selected + unknown) or None
    return (merged if not errors else None), errors


def _normalized_code(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value or "").strip()


def _material_base_code(value: str) -> str:
    code = value.strip()
    code = re.sub(r"/(?:AB|BE|A|B|E)$", "", code, flags=re.IGNORECASE)
    code = re.sub(r"[（(](?:AB|BE|A|B|E)[）)]$", "", code, flags=re.IGNORECASE)
    return code.strip().upper()


def _parse_three_dimensions(value: Any) -> tuple[Decimal, Decimal, Decimal]:
    text = str(value or "").strip()
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*[xX*×]\s*(\d+(?:\.\d+)?)\s*[xX*×]\s*(\d+(?:\.\d+)?)\s*",
        text,
    )
    if match is None:
        raise ValueError(f"三维尺寸无法解析：{text!r}")
    return tuple(Decimal(item) for item in match.groups())  # type: ignore[return-value]


def _parse_crease(value: Any) -> tuple[int, int, int]:
    text = str(value or "").split("=", 1)[0].strip()
    match = re.fullmatch(r"\s*(\d+)\s*[xX*×]\s*(\d+)\s*[xX*×]\s*(\d+)\s*", text)
    if match is None:
        raise ValueError(f"压线尺寸无法解析：{value!r}")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _cutting_mode(forming: str, report_raw: Any) -> str:
    if forming == "开槽":
        return "一开一"
    match = re.search(r"=\s*([1-6])\s*$", str(report_raw or ""))
    if match is None:
        raise ValueError(f"模切/平片缺少一开几证据：{report_raw!r}")
    return f"一开{match.group(1).translate(str.maketrans('123456', '一二三四五六'))}"


def _select_source_row(
    sample_no: str, customer_code: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(rows) == 1:
        return rows[0]
    expected_row = SOURCE_ROW_OVERRIDES.get((sample_no, customer_code))
    selected = [row for row in rows if int(row["source_excel_row"]) == expected_row]
    if len(selected) != 1:
        raise ValueError(
            f"{sample_no}/{customer_code} 原始主档命中{len(rows)}行，缺少唯一选择证据"
        )
    return selected[0]


def _source_product_values(
    *,
    sample: dict[str, Any],
    source_match: dict[str, Any],
    customer_id: int,
    material: dict[str, Any],
) -> dict[str, Any]:
    raw = source_match["row"]
    forming = str(sample["forming"])
    desired = desired_sample_fields(sample)
    source_report_width = int(raw[10])
    source_report_length = int(raw[12])
    if forming == "开槽":
        length, width, height = _parse_three_dimensions(raw[8])
        left, middle, right = _parse_crease(raw[9])
        if left + middle + right != source_report_width:
            raise ValueError("压线之和与报料宽不一致")
        crease_type = "压线"
    else:
        length = width = height = None
        left = middle = right = None
        crease_type = "净料"
    configuration = normalize_box_configuration(
        box_style=desired["box_style"],
        splice_mode="single",
        pieces_per_box=None,
        flap_mm=None,
        default_cutting_mode=_cutting_mode(forming, raw[9]),
        crease_type=crease_type,
    )
    price = Decimal(str(raw[17])) if raw[17] not in (None, "") else None
    if price is None:
        raise ValueError("现行交易未税价为空")
    product_code = _normalized_code(raw[3])
    return {
        "customer_id": customer_id,
        "product_code": product_code,
        "customer_material_code": product_code,
        "product_name": str(raw[1] or "").strip(),
        "material_id": int(material["id"]),
        "mold_tool_id": None,
        "legacy_material_text": str(raw[15] or "").strip() or None,
        "length_mm": length,
        "width_mm": width,
        "height_mm": height,
        "box_category": desired["box_category"],
        "box_style": configuration["box_style"],
        "print_content": desired["print_content"],
        "printing_colors": desired["printing_colors"],
        "production_process": desired["production_process"],
        "unit": "只",
        "sale_unit_price": price,
        "sale_unit_price_no_tax": price,
        "cost_unit_price": None,
        "board_price": None,
        "suggested_price": None,
        "default_cardboard_length": None,
        "default_cardboard_width": None,
        "default_score_lines": None,
        "default_material_code": None,
        "die_cut_path": None,
        "remark": (
            f"{SOURCE}；样品={sample['sample_no']}；源表={source_match['sheet']}；"
            f"源Excel行={source_match['source_excel_row']}；图号={raw[2] or ''}"
        ),
        "legacy_customer_material_code": None,
        # 产品身份、材质、层数和楞型以客户基础资料 Excel 及其匹配材质为准；
        # 现场手写楞型仅用于发现冲突，不能覆盖正式主数据。
        "flute_type": str(material.get("flute_type") or "").strip().upper() or None,
        "layer_count": int(material["layer_count"]),
        "surface_paper_type": None,
        "legacy_flute_text": str(raw[15] or "").strip() or None,
        "report_length_mm": source_report_length,
        "report_width_mm": source_report_width,
        "crease_type": configuration["crease_type"],
        "crease_left_mm": left,
        "crease_middle_mm": middle,
        "crease_right_mm": right,
        "report_notes": None,
        "base_report_length_mm": None,
        "base_report_width_mm": None,
        "base_crease_type": None,
        "base_crease_left_mm": None,
        "base_crease_middle_mm": None,
        "base_crease_right_mm": None,
        "base_report_notes": None,
        "splice_mode": configuration["splice_mode"],
        "pieces_per_box": configuration["pieces_per_box"],
        "default_cutting_mode": configuration["default_cutting_mode"],
        "flap_mm": configuration["flap_mm"],
        "is_composite": False,
        "combination_mode": "parent_priced_set",
        "is_internal_component": False,
        "is_active": True,
        "manual_modified": False,
    }


def _comparable(value: Any) -> Any:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (Decimal, int, float)):
        return format(Decimal(str(value)).normalize(), "f")
    return value


def _product_differences(current: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    return {
        field: {"current": _comparable(current.get(field)), "expected": _comparable(value)}
        for field, value in expected.items()
        if _comparable(current.get(field)) != _comparable(value)
    }


def _identity_resolution_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("resolutions") or []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        sample_no = str(row.get("sample_no") or "").strip()
        if not sample_no:
            raise ValueError("身份纠正清单缺少 sample_no")
        if sample_no in result:
            raise ValueError(f"身份纠正清单存在重复样品号：{sample_no}")
        if row.get("owner_confirmed") is not True:
            raise ValueError(f"{sample_no} 身份纠正尚未取得老板确认")
        required = (
            "original_product_code",
            "corrected_customer_code",
            "corrected_product_code",
            "source_sheet",
            "source_excel_row",
            "source_row",
            "reason",
        )
        missing = [field for field in required if row.get(field) in (None, "", [])]
        if missing:
            raise ValueError(f"{sample_no} 身份纠正缺少字段：{missing}")
        result[sample_no] = row
    return result


def build_plan(
    *,
    database: Path,
    source_manifest: Path,
    source_matches: Path,
    prior_drawing_report: Path,
    composite_manifest: Path,
    attribute_resolution: Path | None = None,
    identity_resolution: Path | None = None,
) -> dict[str, Any]:
    checks = database_checks(database)
    source_payload = _read_json(source_manifest)
    matches_payload = _read_json(source_matches)
    prior_payload = _read_json(prior_drawing_report)
    composites = _read_json(composite_manifest)
    resolution_payload = _read_json(attribute_resolution) if attribute_resolution else {"resolutions": []}
    resolution_rows = resolution_payload.get("resolutions") or []
    resolutions = {
        (
            str(row.get("customer_code") or "").strip().upper(),
            str(row.get("product_code") or "").strip().casefold(),
        ): row
        for row in resolution_rows
    }
    identity_payload = _read_json(identity_resolution) if identity_resolution else {"resolutions": []}
    identity_resolutions = _identity_resolution_map(identity_payload)
    if len(resolutions) != len(resolution_rows):
        raise ValueError("属性冲突解决清单存在重复客户/产品编码")
    errors: list[str] = []
    if checks.integrity_check.lower() != "ok" or checks.foreign_key_violations:
        errors.append("数据库完整性或外键检查未通过")
    if checks.alembic_revision != EXPECTED_SCHEMA_REVISION:
        errors.append(
            f"数据库版本不匹配：{checks.alembic_revision} != {EXPECTED_SCHEMA_REVISION}"
        )
    samples = source_payload.get("rows") or []
    if len(samples) != EXPECTED_SAMPLE_COUNT:
        errors.append(f"样品数量不是{EXPECTED_SAMPLE_COUNT}")
    if source_payload.get("validation_errors"):
        errors.extend(str(item) for item in source_payload["validation_errors"])
    if sum(len(row.get("images") or []) for row in samples) != EXPECTED_SOURCE_IMAGE_COUNT:
        errors.append(f"源图片数量不是{EXPECTED_SOURCE_IMAGE_COUNT}")
    samples_by_no = {str(row["sample_no"]): row for row in samples}
    matches_by_no = {str(row["sample_no"]): row for row in matches_payload["rows"]}
    composites_by_no = {str(row["sample_no"]): row for row in composites}
    prior_by_sample: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in prior_payload["assignments"]:
        prior_by_sample[str(row["sample_no"])].append(row)
    if len(prior_by_sample) != EXPECTED_PRIOR_SAMPLE_COUNT:
        errors.append(f"历史样品覆盖不是{EXPECTED_PRIOR_SAMPLE_COUNT}")
    for sample_no, rows in prior_by_sample.items():
        composite = composites_by_no.get(sample_no)
        if composite is None:
            errors.append(f"{sample_no} 缺少本批合并图")
            continue
        prior_hashes = {str(row["source_sha256"]) for row in rows}
        if prior_hashes != {str(composite["sha256"])}:
            errors.append(f"{sample_no} 本批图片与已导入历史图片不一致")
    for sample_no, resolution in identity_resolutions.items():
        sample = samples_by_no.get(sample_no)
        if sample is None:
            errors.append(f"身份纠正样品不存在：{sample_no}")
            continue
        if _normalized_code(sample.get("product_code")) != _normalized_code(
            resolution["original_product_code"]
        ):
            errors.append(f"{sample_no} 身份纠正的原产品编码与四卷Excel不一致")
        if sample_no not in prior_by_sample:
            errors.append(f"{sample_no} 身份纠正缺少历史图片绑定")

    with closing(_readonly(database)) as connection:
        customers = {
            str(row["customer_code"]).strip().upper(): dict(row)
            for row in connection.execute(
                """
                SELECT id, customer_code, name, is_active
                FROM customers WHERE upper(trim(customer_code)) IN ('YKE','KEW')
                """
            )
        }
        products = {
            int(row["id"]): dict(row)
            for row in connection.execute(
                """
                SELECT p.*, c.customer_code, c.name AS customer_name
                FROM products p JOIN customers c ON c.id=p.customer_id
                WHERE upper(trim(c.customer_code)) IN ('YKE','KEW')
                """
            )
        }
        products_by_key = {
            (str(row["customer_code"]).upper(), str(row["product_code"]).strip().casefold()): row
            for row in products.values()
        }
        materials_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            "SELECT id, code, supplier_name, layer_count, flute_type, is_active FROM materials"
        ):
            materials_by_code[str(row["code"]).strip().upper()].append(dict(row))
        drawing_batch_logs = {
            (int(row["entity_id"]), json.loads(row["details"])["sample_no"]): dict(row)
            for row in connection.execute(
                """
                SELECT id, entity_id, details FROM operation_logs
                WHERE batch_id=? AND action='IMPORT_REFERENCE_IMAGE'
                """,
                (BATCH_ID,),
            )
        }
        reference_image_logs: dict[tuple[int, str], dict[str, Any]] = {}
        for row in connection.execute(
            """
            SELECT id, entity_id, details, batch_id FROM operation_logs
            WHERE action='IMPORT_REFERENCE_IMAGE' AND entity_type='product'
            ORDER BY id
            """
        ):
            details = json.loads(row["details"] or "{}")
            sample_no = str(details.get("sample_no") or "")
            if sample_no:
                reference_image_logs[(int(row["entity_id"]), sample_no)] = {
                    **dict(row),
                    "parsed_details": details,
                }
        reassign_logs: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            """
            SELECT id, entity_id, details, batch_id FROM operation_logs
            WHERE action='REASSIGN_REFERENCE_IMAGE' AND batch_id=?
            ORDER BY id
            """,
            (BATCH_ID,),
        ):
            details = json.loads(row["details"] or "{}")
            sample_no = str(details.get("sample_no") or "")
            if sample_no:
                reassign_logs[sample_no] = {**dict(row), "parsed_details": details}
        drawings_by_id = {
            int(row["id"]): dict(row)
            for row in connection.execute(
                "SELECT id, product_id, image_path, thumbnail_path, uploaded_at FROM product_drawings"
            )
        }

    samples_by_product: dict[int, list[dict[str, Any]]] = defaultdict(list)
    prior_targets: dict[int, dict[str, Any]] = {}
    for sample_no, assignments in prior_by_sample.items():
        sample = samples_by_no[sample_no]
        for assignment in assignments:
            product_id = int(assignment["product_id"])
            product = products.get(product_id)
            if product is None:
                errors.append(f"历史目标常用箱不存在：product_id={product_id}")
                continue
            if (
                str(product["customer_code"]).upper() != assignment["customer_code"]
                or str(product["product_code"]).casefold()
                != str(assignment["product_code"]).casefold()
            ):
                errors.append(f"历史目标常用箱身份已变化：product_id={product_id}")
                continue
            prior_targets[product_id] = product
            if sample_no not in identity_resolutions:
                samples_by_product[product_id].append(sample)
    if len(prior_targets) != EXPECTED_PRIOR_PRODUCT_COUNT:
        errors.append(f"历史目标常用箱数量不是{EXPECTED_PRIOR_PRODUCT_COUNT}")

    field_updates: list[dict[str, Any]] = []
    layer_flute_conflicts: list[dict[str, Any]] = []
    applied_resolutions: list[dict[str, Any]] = []
    for product_id, product_samples in sorted(samples_by_product.items()):
        product = prior_targets[product_id]
        desired, merge_errors = merge_sample_fields(
            product_samples,
            existing_process=product.get("production_process"),
            existing_box_category=str(product["box_category"]),
            existing_box_style=product.get("box_style"),
        )
        if merge_errors or desired is None:
            errors.extend(
                f"{product['customer_code']}/{product['product_code']}: {error}"
                for error in merge_errors
            )
            continue
        resolution_key = (
            str(product["customer_code"]).upper(),
            str(product["product_code"]).casefold(),
        )
        resolution = resolutions.get(resolution_key)
        resolution_updates: dict[str, Any] = {}
        if resolution is not None:
            reason = str(resolution.get("reason") or "").strip()
            resolved_flute = str(
                resolution.get("flute_type") or desired["flute_type"] or ""
            ).strip().upper()
            material_code = str(resolution.get("material_code") or "").strip().upper()
            supplier_name = str(resolution.get("supplier_name") or "").strip()
            if not reason or not material_code or not resolved_flute:
                errors.append(
                    f"{product['customer_code']}/{product['product_code']} 冲突解决清单缺少reason、material_code或flute_type"
                )
            else:
                candidates = [
                    row
                    for row in materials_by_code.get(material_code, [])
                    if row["is_active"]
                    and (not supplier_name or row["supplier_name"] == supplier_name)
                ]
                if len(candidates) != 1:
                    errors.append(
                        f"{product['customer_code']}/{product['product_code']} 解决材质{material_code}启用候选不是唯一一条"
                    )
                else:
                    selected_material = candidates[0]
                    desired["flute_type"] = resolved_flute
                    resolution_updates = {
                        "material_id": int(selected_material["id"]),
                        "layer_count": int(selected_material["layer_count"]),
                        "legacy_material_text": material_code,
                    }
                    applied_resolutions.append(
                        {
                            "customer_code": product["customer_code"],
                            "product_code": product["product_code"],
                            "material_id": int(selected_material["id"]),
                            "material_code": material_code,
                            "supplier_name": selected_material["supplier_name"],
                            "layer_count": int(selected_material["layer_count"]),
                            "flute_type": resolved_flute,
                            "reason": reason,
                        }
                    )
        target_layer_count = resolution_updates.get("layer_count", product["layer_count"])
        flute_error = validate_flute_for_write(desired["flute_type"], target_layer_count)
        if flute_error:
            conflict = {
                "product_id": product_id,
                "customer_code": product["customer_code"],
                "product_code": product["product_code"],
                "material_id": product["material_id"],
                "layer_count": product["layer_count"],
                "current_flute_type": product["flute_type"],
                "sample_flute_type": desired["flute_type"],
                "samples": [row["sample_no"] for row in product_samples],
                "error": flute_error,
                "resolution_present": resolution is not None,
            }
            layer_flute_conflicts.append(conflict)
            errors.append(
                f"{product['customer_code']}/{product['product_code']} 手填楞型与绑定材质层数冲突"
            )
            continue
        updates = {
            field: value
            for field, value in desired.items()
            if _comparable(product.get(field)) != _comparable(value)
        }
        updates.update(
            {
                field: value
                for field, value in resolution_updates.items()
                if _comparable(product.get(field)) != _comparable(value)
            }
        )
        if updates:
            field_updates.append(
                {
                    "product_id": product_id,
                    "customer_code": product["customer_code"],
                    "product_code": product["product_code"],
                    "expected_version": int(product["version"]),
                    "samples": [row["sample_no"] for row in product_samples],
                    "updates": updates,
                }
            )

    skipped: list[dict[str, Any]] = []
    new_products: list[dict[str, Any]] = []
    for sample in samples:
        sample_no = str(sample["sample_no"])
        if sample_no in prior_by_sample:
            continue
        match = matches_by_no[sample_no]
        exact = match.get("exact_matches") or []
        code = _normalized_code(sample["product_code"])
        if code == "80012500":
            skipped.append(
                {
                    "sample_no": sample_no,
                    "product_code": code,
                    "reason": "同一客户同一编码对应本体和加强板两种材质，需另建组合结构",
                }
            )
            continue
        if not exact:
            skipped.append(
                {
                    "sample_no": sample_no,
                    "product_code": code,
                    "reason": "YKE/KEW原始主档无完全匹配，禁止猜测客户",
                }
            )
            continue
        by_customer: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in exact:
            by_customer[str(row["sheet"]).strip().upper()].append(row)
        for customer_code, source_rows in sorted(by_customer.items()):
            customer = customers.get(customer_code)
            if customer is None or not customer["is_active"]:
                errors.append(f"客户主档不存在或停用：{customer_code}")
                continue
            try:
                source_match = _select_source_row(sample_no, customer_code, source_rows)
                raw = source_match["row"]
                material_code = _material_base_code(str(raw[15] or ""))
                candidates = [row for row in materials_by_code[material_code] if row["is_active"]]
                if len(candidates) != 1:
                    raise ValueError(
                        f"材质{material_code}启用候选不是唯一一条：{len(candidates)}"
                    )
                values = _source_product_values(
                    sample=sample,
                    source_match=source_match,
                    customer_id=int(customer["id"]),
                    material=candidates[0],
                )
                flute_error = validate_flute_for_write(
                    values["flute_type"], values["layer_count"]
                )
                if flute_error:
                    raise ValueError(flute_error)
            except (KeyError, TypeError, ValueError) as error:
                errors.append(f"{sample_no}/{customer_code}: {error}")
                continue
            key = (customer_code, str(values["product_code"]).casefold())
            existing = products_by_key.get(key)
            composite = composites_by_no[sample_no]
            if existing is None:
                status = "create"
                product_id = None
                differences = {}
            else:
                product_id = int(existing["id"])
                differences = _product_differences(existing, values)
                has_batch_drawing = (product_id, sample_no) in drawing_batch_logs
                status = "already_applied" if not differences and has_batch_drawing else "conflict"
                if status == "conflict":
                    errors.append(
                        f"{sample_no}/{customer_code}/{values['product_code']} 已存在但不是本批幂等结果"
                    )
            new_products.append(
                {
                    "sample_no": sample_no,
                    "customer_code": customer_code,
                    "customer_name": customer["name"],
                    "customer_id": int(customer["id"]),
                    "source_excel_row": int(source_match["source_excel_row"]),
                    "source_sheet": source_match["sheet"],
                    "product_code": values["product_code"],
                    "status": status,
                    "product_id": product_id,
                    "differences": differences,
                    "values": values,
                    "composite": composite,
                    "drawing_action": "create",
                }
            )
    drawing_reassignments: list[dict[str, Any]] = []
    for sample_no, resolution in sorted(identity_resolutions.items()):
        sample = samples_by_no.get(sample_no)
        if sample is None:
            continue
        corrected_customer = str(resolution["corrected_customer_code"]).strip().upper()
        corrected_code = _normalized_code(resolution["corrected_product_code"])
        source_sheet = str(resolution["source_sheet"]).strip().upper()
        source_row = resolution["source_row"]
        if source_sheet != corrected_customer:
            errors.append(f"{sample_no} 身份纠正的源工作表与目标客户不一致")
            continue
        if len(source_row) < 18 or _normalized_code(source_row[3]) != corrected_code:
            errors.append(f"{sample_no} 身份纠正的源Excel行与纠正后产品编码不一致")
            continue
        assignments = [
            row
            for row in prior_by_sample.get(sample_no, [])
            if str(row["customer_code"]).strip().upper() == corrected_customer
        ]
        if len(assignments) != 1:
            errors.append(f"{sample_no} 无法唯一确定需要撤下图片的历史客户产品")
            continue
        assignment = assignments[0]
        if _normalized_code(assignment["product_code"]) != _normalized_code(
            resolution["original_product_code"]
        ):
            errors.append(f"{sample_no} 历史图片产品与身份纠正原产品不一致")
            continue
        source_product_id = int(assignment["product_id"])
        customer = customers.get(corrected_customer)
        if customer is None or not customer["is_active"]:
            errors.append(f"{sample_no} 纠正目标客户不存在或已停用：{corrected_customer}")
            continue
        source_match = {
            "sheet": source_sheet,
            "source_excel_row": int(resolution["source_excel_row"]),
            "row": source_row,
        }
        try:
            material_code = _material_base_code(str(source_row[15] or ""))
            candidates = [
                row for row in materials_by_code[material_code] if row["is_active"]
            ]
            if len(candidates) != 1:
                raise ValueError(
                    f"材质{material_code}启用候选不是唯一一条：{len(candidates)}"
                )
            corrected_sample = {**sample, "product_code": corrected_code}
            values = _source_product_values(
                sample=corrected_sample,
                source_match=source_match,
                customer_id=int(customer["id"]),
                material=candidates[0],
            )
            flute_error = validate_flute_for_write(
                values["flute_type"], values["layer_count"]
            )
            if flute_error:
                raise ValueError(flute_error)
        except (KeyError, TypeError, ValueError) as error:
            errors.append(f"{sample_no}/{corrected_customer}: {error}")
            continue
        target_key = (corrected_customer, corrected_code.casefold())
        existing = products_by_key.get(target_key)
        differences = _product_differences(existing, values) if existing else {}
        if differences:
            errors.append(
                f"{sample_no}/{corrected_customer}/{corrected_code} 已存在但与Excel权威行不一致"
            )
            continue

        previous_log = reference_image_logs.get((source_product_id, sample_no))
        applied_log = reassign_logs.get(sample_no)
        drawing_id: int | None = None
        target_product_id = int(existing["id"]) if existing else None
        if applied_log is not None:
            details = applied_log["parsed_details"]
            drawing_id = int(details.get("drawing_id") or 0)
            logged_target_id = int(details.get("target_product_id") or 0)
            drawing = drawings_by_id.get(drawing_id)
            if (
                existing is None
                or logged_target_id != int(existing["id"])
                or drawing is None
                or int(drawing["product_id"]) != logged_target_id
            ):
                errors.append(f"{sample_no} 已有图片纠正日志但数据库结果不一致")
                continue
            status = "already_applied"
        else:
            if previous_log is None:
                errors.append(f"{sample_no} 缺少原图片导入审计日志，禁止移动图片")
                continue
            drawing_id = int(previous_log["parsed_details"].get("drawing_id") or 0)
            drawing = drawings_by_id.get(drawing_id)
            if drawing is None or int(drawing["product_id"]) != source_product_id:
                errors.append(f"{sample_no} 原图片记录已变化，禁止移动图片")
                continue
            status = "reassign_existing" if existing else "create"

        composite = composites_by_no[sample_no]
        new_products.append(
            {
                "sample_no": sample_no,
                "customer_code": corrected_customer,
                "customer_name": customer["name"],
                "customer_id": int(customer["id"]),
                "source_excel_row": int(source_match["source_excel_row"]),
                "source_sheet": source_sheet,
                "product_code": corrected_code,
                "status": status,
                "product_id": target_product_id,
                "differences": differences,
                "values": values,
                "composite": composite,
                "drawing_action": "reassign",
            }
        )
        drawing_reassignments.append(
            {
                "sample_no": sample_no,
                "status": "already_applied" if status == "already_applied" else "ready",
                "drawing_id": drawing_id,
                "source_product_id": source_product_id,
                "source_customer_code": assignment["customer_code"],
                "source_product_code": assignment["product_code"],
                "target_product_id": target_product_id,
                "target_customer_code": corrected_customer,
                "target_product_code": corrected_code,
                "source_excel_row": int(source_match["source_excel_row"]),
                "reason": str(resolution["reason"]),
            }
        )
    distinct_new_keys = {
        (row["customer_code"], str(row["product_code"]).casefold()) for row in new_products
    }
    if len(distinct_new_keys) != EXPECTED_NEW_PRODUCT_COUNT:
        errors.append(f"新常用箱目标数量不是{EXPECTED_NEW_PRODUCT_COUNT}")
    duplicate_new_keys = [key for key, count in Counter(
        (row["customer_code"], str(row["product_code"]).casefold()) for row in new_products
    ).items() if count > 1]
    if duplicate_new_keys:
        errors.append(f"新常用箱计划重复：{duplicate_new_keys}")
    unused_resolutions = [
        row
        for key, row in resolutions.items()
        if key
        not in {
            (str(item["customer_code"]).upper(), str(item["product_code"]).casefold())
            for item in applied_resolutions
        }
    ]
    if unused_resolutions:
        errors.append("属性冲突解决清单含未命中的记录")

    return {
        "batch_id": BATCH_ID,
        "status": "blocked" if errors else (
            "already_applied"
            if not field_updates and all(row["status"] == "already_applied" for row in new_products)
            else "ready"
        ),
        "database": str(database.resolve()),
        "database_checks": asdict(checks),
        "source_files": {
            "source_manifest": str(source_manifest.resolve()),
            "source_manifest_sha256": sha256_file(source_manifest),
            "source_matches": str(source_matches.resolve()),
            "source_matches_sha256": sha256_file(source_matches),
            "prior_drawing_report": str(prior_drawing_report.resolve()),
            "prior_drawing_report_sha256": sha256_file(prior_drawing_report),
            "composite_manifest": str(composite_manifest.resolve()),
            "composite_manifest_sha256": sha256_file(composite_manifest),
            "attribute_resolution": str(attribute_resolution.resolve()) if attribute_resolution else None,
            "attribute_resolution_sha256": sha256_file(attribute_resolution) if attribute_resolution else None,
            "identity_resolution": str(identity_resolution.resolve()) if identity_resolution else None,
            "identity_resolution_sha256": sha256_file(identity_resolution) if identity_resolution else None,
        },
        "source_sample_count": len(samples),
        "source_image_count": sum(len(row.get("images") or []) for row in samples),
        "prior_sample_count": len(prior_by_sample),
        "prior_product_count": len(prior_targets),
        "prior_images_unchanged": not identity_resolutions and not any(
            "本批图片与已导入历史图片不一致" in error for error in errors
        ),
        "identity_resolutions": list(identity_resolutions.values()),
        "drawing_reassignments": drawing_reassignments,
        "field_update_count": len(field_updates),
        "field_updates": field_updates,
        "layer_flute_conflicts": layer_flute_conflicts,
        "applied_resolutions": applied_resolutions,
        "new_product_count": len(new_products),
        "new_products": new_products,
        "new_products_to_create": sum(row["status"] == "create" for row in new_products),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "errors": errors,
    }


def _backup_database(database: Path, backup_dir: Path) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / f"carton_erp_{timestamp}_before_YKE_KEW_sample_sync.sqlite3"
    temporary = target.with_suffix(".sqlite3.tmp")
    try:
        with closing(sqlite3.connect(database, timeout=30)) as source:
            source.execute("PRAGMA busy_timeout=30000")
            with closing(sqlite3.connect(temporary, timeout=30)) as destination:
                source.backup(destination)
                destination.commit()
        checks = database_checks(temporary)
        if checks.integrity_check.lower() != "ok" or checks.foreign_key_violations:
            raise RuntimeError("数据库备份校验失败")
        temporary.replace(target)
        return {
            "path": str(target),
            "sha256": sha256_file(target),
            "size": target.stat().st_size,
            "integrity_check": checks.integrity_check,
            "foreign_key_violations": checks.foreign_key_violations,
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def apply_plan(
    *,
    plan: dict[str, Any],
    database: Path,
    upload_root: Path,
    backup_dir: Path,
    actor_username: str,
    expected_database_sha256: str,
) -> dict[str, Any]:
    if plan["status"] == "blocked" or plan["errors"]:
        raise RuntimeError("导入计划存在冲突，禁止写入")
    if plan["database_checks"]["sha256"].lower() != expected_database_sha256.lower():
        raise RuntimeError("数据库 SHA-256 与授权前置值不一致")
    if plan["status"] == "already_applied":
        return {**plan, "changed": False}
    before = DatabaseChecks(**plan["database_checks"])
    backup = _backup_database(database, backup_dir)
    if sha256_file(database).lower() != expected_database_sha256.lower():
        raise RuntimeError("备份后数据库已变化，停止写入")

    os.environ["ERP_FILE_STORAGE_DIR"] = str(upload_root.resolve())
    from app.services.product_drawings import remove_drawing_files, save_product_drawing_files
    from app.services.secure_uploads import ValidatedUpload

    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    stored: list[tuple[str, str]] = []
    created_product_ids: list[int] = []
    created_target_ids: dict[tuple[str, str, str], int] = {}
    updated_product_ids: list[int] = []
    drawing_ids: list[int] = []
    reassigned_drawing_ids: list[int] = []
    try:
        with factory() as db:
            actor = db.scalar(select(User).where(User.username == actor_username))
            if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
                raise RuntimeError("执行账号必须是启用的 admin 或 boss")
            for item in plan["field_updates"]:
                product = db.get(Product, int(item["product_id"]))
                if product is None:
                    raise RuntimeError(f"更新目标不存在：{item['product_id']}")
                apply_versioned_update(
                    db,
                    object_type="product",
                    entity=product,
                    updates=item["updates"],
                    expected_version=int(item["expected_version"]),
                    user=actor,
                    reason=REASON,
                    source=SOURCE,
                    action="system_consistency_fix",
                )
                audit_master_change(
                    db,
                    user=actor,
                    action="SYNC_SAMPLE_ATTRIBUTES",
                    resource="Product",
                    resource_id=int(product.id),
                    details={
                        "batch_id": BATCH_ID,
                        "samples": item["samples"],
                        "updated_fields": sorted(item["updates"]),
                    },
                )
                updated_product_ids.append(int(product.id))

            for item in plan["new_products"]:
                if item["status"] != "create":
                    continue
                product = Product(**item["values"])
                db.add(product)
                db.flush()
                record_versioned_create(
                    db,
                    object_type="product",
                    entity=product,
                    user=actor,
                    reason=REASON,
                    source=SOURCE,
                )
                audit_master_change(
                    db,
                    user=actor,
                    action="CREATE_SAMPLE_PRODUCT",
                    resource="Product",
                    resource_id=int(product.id),
                    details={
                        "batch_id": BATCH_ID,
                        "sample_no": item["sample_no"],
                        "customer_code": item["customer_code"],
                        "source_sheet": item["source_sheet"],
                        "source_excel_row": item["source_excel_row"],
                    },
                )
                created_product_ids.append(int(product.id))
                created_target_ids[
                    (
                        str(item["sample_no"]),
                        str(item["customer_code"]).upper(),
                        str(item["product_code"]).casefold(),
                    )
                ] = int(product.id)
                if item.get("drawing_action") != "create":
                    continue
                image_path = Path(item["composite"]["path"])
                content = image_path.read_bytes()
                actual_sha256 = hashlib.sha256(content).hexdigest()
                if actual_sha256 != item["composite"]["sha256"]:
                    raise RuntimeError(f"合并图已变化：{image_path}")
                upload = ValidatedUpload(
                    content=content,
                    original_filename=image_path.name,
                    extension=".jpg",
                    content_type="image/jpeg",
                    size=len(content),
                    sha256=actual_sha256,
                )
                saved = save_product_drawing_files(product_id=int(product.id), upload=upload)
                stored.append((saved.image_path, saved.thumbnail_path))
                drawing = ProductDrawing(
                    product_id=int(product.id),
                    image_path=saved.image_path,
                    thumbnail_path=saved.thumbnail_path,
                    uploaded_by=actor.id,
                )
                db.add(drawing)
                db.flush()
                db.add(
                    OperationLog(
                        user_id=actor.id,
                        action="IMPORT_REFERENCE_IMAGE",
                        resource="Product",
                        details=json.dumps(
                            {
                                "drawing_id": int(drawing.id),
                                "batch_id": BATCH_ID,
                                "sample_no": item["sample_no"],
                                "source_sha256": actual_sha256,
                                "source_image_sha256": item["composite"]["source_image_sha256"],
                                "customer_code": item["customer_code"],
                                "product_code": item["product_code"],
                                "printing_independent": True,
                            },
                            ensure_ascii=False,
                        ),
                        username=actor.username,
                        role=actor.role,
                        entity_type="product",
                        entity_id=int(product.id),
                        description="IMPORT_REFERENCE_IMAGE Product",
                        event_category="master_data",
                        result="success",
                        source="offline_tool",
                        module_code="products",
                        action_code="product.import_reference_image",
                        actor_user_id_snapshot=actor.id,
                        operator_name_snapshot=actor.username,
                        object_ref=f"Product:{product.id}",
                        customer_id_snapshot=int(item["customer_id"]),
                        customer_name_snapshot=item["customer_name"],
                        batch_id=BATCH_ID,
                        schema_version=1,
                    )
                )
                drawing_ids.append(int(drawing.id))

            for item in plan["drawing_reassignments"]:
                if item["status"] == "already_applied":
                    continue
                target_product_id = item.get("target_product_id") or created_target_ids.get(
                    (
                        str(item["sample_no"]),
                        str(item["target_customer_code"]).upper(),
                        str(item["target_product_code"]).casefold(),
                    )
                )
                if target_product_id is None:
                    raise RuntimeError(f"{item['sample_no']} 图片纠正目标产品不存在")
                drawing = db.get(ProductDrawing, int(item["drawing_id"]))
                if drawing is None or int(drawing.product_id) != int(item["source_product_id"]):
                    raise RuntimeError(f"{item['sample_no']} 原图片绑定已变化")
                source_product = db.get(Product, int(item["source_product_id"]))
                target_product = db.get(Product, int(target_product_id))
                if source_product is None or target_product is None:
                    raise RuntimeError(f"{item['sample_no']} 图片纠正产品不存在")
                drawing.product_id = int(target_product_id)
                db.flush()
                details = {
                    "batch_id": BATCH_ID,
                    "sample_no": item["sample_no"],
                    "drawing_id": int(drawing.id),
                    "source_product_id": int(item["source_product_id"]),
                    "source_customer_code": item["source_customer_code"],
                    "source_product_code": item["source_product_code"],
                    "target_product_id": int(target_product_id),
                    "target_customer_code": item["target_customer_code"],
                    "target_product_code": item["target_product_code"],
                    "source_excel_row": int(item["source_excel_row"]),
                    "reason": item["reason"],
                }
                db.add(
                    OperationLog(
                        user_id=actor.id,
                        action="REASSIGN_REFERENCE_IMAGE",
                        resource="ProductDrawing",
                        details=json.dumps(details, ensure_ascii=False),
                        username=actor.username,
                        role=actor.role,
                        entity_type="product",
                        entity_id=int(target_product_id),
                        description="REASSIGN_REFERENCE_IMAGE ProductDrawing",
                        event_category="master_data",
                        result="success",
                        source="offline_tool",
                        module_code="products",
                        action_code="product.reassign_reference_image",
                        actor_user_id_snapshot=actor.id,
                        operator_name_snapshot=actor.username,
                        object_ref=f"ProductDrawing:{drawing.id}",
                        customer_id_snapshot=int(target_product.customer_id),
                        customer_name_snapshot=item["target_customer_code"],
                        batch_id=BATCH_ID,
                        schema_version=1,
                    )
                )
                audit_master_change(
                    db,
                    user=actor,
                    action="REASSIGN_SAMPLE_DRAWING_OUT",
                    resource="Product",
                    resource_id=int(source_product.id),
                    details=details,
                )
                audit_master_change(
                    db,
                    user=actor,
                    action="REASSIGN_SAMPLE_DRAWING_IN",
                    resource="Product",
                    resource_id=int(target_product.id),
                    details=details,
                )
                reassigned_drawing_ids.append(int(drawing.id))
            db.commit()
    except Exception:
        for image_path, thumbnail_path in stored:
            remove_drawing_files(image_path, thumbnail_path)
        raise
    finally:
        engine.dispose()

    after = database_checks(database)
    if after.integrity_check.lower() != "ok" or after.foreign_key_violations:
        raise RuntimeError("写入后完整性检查失败，请立即按备份回退")
    for table in PROTECTED_TABLES:
        if before.counts.get(table) != after.counts.get(table):
            raise RuntimeError(f"保护表意外变化：{table}")
    if after.counts["products"] - before.counts["products"] != len(created_product_ids):
        raise RuntimeError("常用箱增量不符合计划")
    if after.counts["product_drawings"] - before.counts["product_drawings"] != len(drawing_ids):
        raise RuntimeError("图纸增量不符合计划")
    with closing(_readonly(database)) as connection:
        for item in plan["drawing_reassignments"]:
            expected_product_id = item.get("target_product_id") or created_target_ids.get(
                (
                    str(item["sample_no"]),
                    str(item["target_customer_code"]).upper(),
                    str(item["target_product_code"]).casefold(),
                )
            )
            row = connection.execute(
                "SELECT product_id FROM product_drawings WHERE id=?",
                (int(item["drawing_id"]),),
            ).fetchone()
            if row is None or int(row["product_id"]) != int(expected_product_id):
                raise RuntimeError(f"{item['sample_no']} 图片纠正回读失败")
    return {
        **plan,
        "status": "applied",
        "changed": True,
        "backup": backup,
        "database_checks_after": asdict(after),
        "database_sha256_after": after.sha256,
        "updated_product_ids": updated_product_ids,
        "created_product_ids": created_product_ids,
        "created_drawing_ids": drawing_ids,
        "reassigned_drawing_ids": reassigned_drawing_ids,
        "upload_root": str(upload_root.resolve()),
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="YKE/KEW四卷样品常用箱属性与图片受控同步")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--formal-database-path", type=Path)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-matches", type=Path, required=True)
    parser.add_argument("--prior-drawing-report", type=Path, required=True)
    parser.add_argument("--composite-manifest", type=Path, required=True)
    parser.add_argument("--attribute-resolution", type=Path)
    parser.add_argument("--identity-resolution", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--upload-root", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--expected-database-sha256")
    parser.add_argument("--confirm")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    inputs = {
        "source_manifest": args.source_manifest.resolve(strict=True),
        "source_matches": args.source_matches.resolve(strict=True),
        "prior_drawing_report": args.prior_drawing_report.resolve(strict=True),
        "composite_manifest": args.composite_manifest.resolve(strict=True),
        "attribute_resolution": (
            args.attribute_resolution.resolve(strict=True)
            if args.attribute_resolution
            else None
        ),
        "identity_resolution": (
            args.identity_resolution.resolve(strict=True)
            if args.identity_resolution
            else None
        ),
    }
    plan = build_plan(database=database, **inputs)
    result = plan
    if args.apply:
        if args.confirm != CONFIRMATION:
            raise SystemExit(f"--apply 必须同时提供 --confirm {CONFIRMATION}")
        if args.formal_database_path is None:
            raise SystemExit("--apply 必须提供 --formal-database-path")
        formal_database = args.formal_database_path.resolve(strict=True)
        if database != formal_database:
            raise SystemExit("写入目标不是显式确认的唯一正式数据库")
        if not args.expected_database_sha256:
            raise SystemExit("--apply 必须提供 --expected-database-sha256")
        if args.upload_root is None or args.backup_dir is None:
            raise SystemExit("--apply 必须提供 --upload-root 与 --backup-dir")
        result = apply_plan(
            plan=plan,
            database=database,
            upload_root=args.upload_root,
            backup_dir=args.backup_dir,
            actor_username=args.actor,
            expected_database_sha256=args.expected_database_sha256,
        )
    report = args.report.resolve()
    _write_report(report, result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "database_sha256": result["database_checks"]["sha256"],
                "database_sha256_after": result.get("database_sha256_after"),
                "prior_images_unchanged": result["prior_images_unchanged"],
                "field_update_count": result["field_update_count"],
                "layer_flute_conflict_count": len(result["layer_flute_conflicts"]),
                "new_product_count": result["new_product_count"],
                "new_products_to_create": result["new_products_to_create"],
                "skipped_count": result["skipped_count"],
                "error_count": len(result["errors"]),
                "backup": result.get("backup"),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

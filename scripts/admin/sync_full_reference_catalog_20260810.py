from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.master_data_common import audit_master_change  # noqa: E402
from app.api.product_import import _create_product, _update_product  # noqa: E402
from app.api.products import ProductPayload, _product_payload_snapshot  # noqa: E402
from app.core.database import create_sqlite_engine  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.material import Material  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.product_drawing import ProductDrawing  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.mixed_sample_import import (  # noqa: E402
    merge_registration,
    normalize_sample_code,
    read_mixed_reference_workbook,
    read_mixed_sample_workbook,
    reference_candidates,
)
from app.services.master_data_versioning import apply_versioned_update  # noqa: E402
from scripts.admin.sync_mixed_sample_catalog_20260810 import (  # noqa: E402
    _add_drawings,
    _find_product,
    remove_drawing_files,
)


BATCH_ID = "FULL_REFERENCE_CATALOG_20260810"
FORMAL_CONFIRMATION = "APPLY_FULL_REFERENCE_CATALOG_20260810"
REHEARSAL_CONFIRMATION = "APPLY_FULL_REFERENCE_CATALOG_REHEARSAL_20260810"
EXPECTED_REFERENCE_SHA256 = "C4F60DB29EB9A9B48B1BEED40522D024D0F0B3AEE39F75DCEB0AE695D3F43303"
EXPECTED_VOLUME_SHA256 = (
    "30E5952EE7DF4EB457E4F69F40F0187A3973B3106D6649FD52BD8B342E76D5A3",
    "A475998C5E6ACE3FF5908A9597E7A12FF39CEB3FE0DD7304F7BDAAC580105CD6",
    "421753ED7572703FF9A57B26FC5B3F6E2D9F6B65A4D6E23B41CB67122E848D57",
    "4D19622397BA540A0A5712B8777448346B4376CA68F47FD10DBBA04F97EC994A",
    "8F425E6A5DA699B0C3222C13720E9876B150CC1F75B4D650D07B165B482EFECC",
)
EXPECTED_REFERENCE_ROWS = 454
EXPECTED_UNIQUE_PRODUCTS = 433
EXPECTED_INITIAL_MISSING_PRODUCTS = 186
EXPECTED_SCHEMA_REVISION = "dz08v8x9z97"
FORMAL_DATABASE = Path(r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3").resolve()
OLD_VIK_MATERIAL_ID = 609
VIK_MATERIAL_ID = 631
KIV_MATERIAL_ID = 632
SOURCE_BLOCK_START = "【基础资料原始行20260810】"
SOURCE_BLOCK_END = "【基础资料原始行结束】"

WATCH_TABLES = (
    "customers",
    "products",
    "product_drawings",
    "product_bom_components",
    "materials",
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
    "customers",
    "materials",
    "product_bom_components",
    "sales_orders",
    "sales_order_items",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    "inventory_lots",
    "finished_goods_lots",
    "delivery_orders",
    "purchase_orders",
)


@dataclass(frozen=True, slots=True)
class DatabaseChecks:
    sha256: str
    integrity_check: str
    foreign_key_violations: int
    alembic_revision: str | None
    counts: dict[str, int]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _database_checks(path: Path) -> DatabaseChecks:
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in WATCH_TABLES
            if table in tables
        }
        revision_row = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    return DatabaseChecks(
        sha256=_sha256_file(path),
        integrity_check=integrity,
        foreign_key_violations=len(foreign_keys),
        alembic_revision=str(revision_row[0]) if revision_row else None,
        counts=counts,
    )


def _assert_database_target(path: Path, *, allow_formal_database: bool) -> bool:
    resolved = path.resolve()
    is_formal = resolved == FORMAL_DATABASE
    if is_formal:
        if not allow_formal_database:
            raise RuntimeError("正式数据库必须显式提供 --allow-formal-database")
        return True
    if resolved.name.lower() == "carton_erp.sqlite3":
        raise RuntimeError("隔离副本不得伪装成正式数据库文件名")
    if not resolved.is_file():
        raise RuntimeError(f"数据库副本不存在：{resolved}")
    return False


def _text(value: object) -> str:
    return str(value or "").strip()


def _material_source_code(value: object) -> str | None:
    text = _text(value).upper().replace("（", "(").replace("）", ")")
    if not text:
        return None
    if text.startswith("450克灰底白板"):
        return "450克灰底白板"
    # A normal corrugated material expression has at most one slash, used only
    # for its flute suffix (for example VIK/B).  Two slash-qualified expressions
    # in the same cell are alternative material directions, not one code.
    if text.count("/") > 1:
        return None
    text = text.replace(" ", "")
    if re.fullmatch(r"Z\+B(?:/B|\(B\))?", text):
        return "KIV"
    if re.fullmatch(r"B\+Z(?:/B|\(B\))?", text):
        return "VIK"
    parenthesized = re.fullmatch(r"(.+?)\((ABC|AAA|AB|BE|A|B|E)\)", text)
    if parenthesized:
        text = parenthesized.group(1)
    flute_suffix = re.fullmatch(r"(.+?)/(ABC|AAA|AB|BE|A|B|E)", text)
    if flute_suffix:
        return flute_suffix.group(1)
    if "/" in text:
        return None
    return text


def _material_resolution(
    db: Session,
    references: list[dict[str, Any]],
) -> tuple[Material | None, list[str], str | None]:
    source_codes = sorted(
        {
            code
            for code in (
                _material_source_code(row.get("legacy_material_text"))
                for row in references
            )
            if code
        }
    )
    if not source_codes:
        return None, [], "基础资料没有可绑定的瓦楞材质"
    if len(source_codes) != 1:
        return None, source_codes, "同一客户款号存在多个材质方向"
    source_code = source_codes[0]
    rows = db.scalars(
        select(Material).where(
            func.upper(func.trim(Material.code)) == source_code.upper(),
            Material.is_active.is_(True),
        )
    ).all()
    if len(rows) != 1:
        reason = (
            "非瓦楞供应商产品主档待建立"
            if source_code == "450克灰底白板"
            else "材质主档未唯一匹配"
        )
        return None, source_codes, reason
    return rows[0], source_codes, None


def _yield_is_explicit(reference: dict[str, Any]) -> bool:
    if reference.get("box_category") != "die_cut":
        return True
    return bool(
        re.search(
            r"=\s*[1-6](?:PCS)?\s*$",
            _text(reference.get("report_raw")),
            re.IGNORECASE,
        )
    )


def _reference_score(product: Product, reference: dict[str, Any]) -> tuple[int, int]:
    score = 0
    for field in (
        "product_name",
        "length_mm",
        "width_mm",
        "height_mm",
        "report_width_mm",
        "report_length_mm",
        "default_cutting_mode",
    ):
        expected = reference.get(field)
        if expected is not None and not _same_value(getattr(product, field, None), expected):
            score += 1
    return score, int(reference["source_row"])


def _canonical_reference(
    references: list[dict[str, Any]],
    existing: Product | None,
) -> dict[str, Any]:
    if existing is None:
        return sorted(references, key=lambda row: row["source_row"])[0]
    return min(references, key=lambda row: _reference_score(existing, row))


def _source_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _source_block(references: list[dict[str, Any]]) -> str:
    rows = []
    for reference in sorted(references, key=lambda row: row["source_row"]):
        rows.append(
            {
                "工作表": reference["source_sheet"],
                "Excel行": reference["source_row"],
                "列值": {
                    key: _source_json_value(value)
                    for key, value in reference.get("source_values", {}).items()
                },
                "字段口径": {
                    "B使用型番": reference.get("product_name"),
                    "C图号": reference.get("drawing_number"),
                    "D品目号": reference.get("product_code"),
                    "E箱型": reference.get("source_box_type"),
                    "F生产数量": _source_json_value(reference.get("production_quantity")),
                    "G预留损耗数": _source_json_value(reference.get("reserved_loss_quantity")),
                    "H存放位置": reference.get("storage_location"),
                    "J压线或模切用纸尺寸": reference.get("report_raw"),
                    "K用纸宽度": reference.get("report_width_mm"),
                    "M用纸长度": reference.get("report_length_mm"),
                    "N计算用纸系数": _source_json_value(reference.get("paper_calculation_coefficient")),
                    "O用纸张数": _source_json_value(reference.get("paper_sheet_quantity")),
                    "P材质": reference.get("legacy_material_text"),
                },
            }
        )
    return SOURCE_BLOCK_START + json.dumps(
        rows, ensure_ascii=False, separators=(",", ":")
    ) + SOURCE_BLOCK_END


def _merge_source_remark(current: str | None, references: list[dict[str, Any]]) -> str:
    text = _text(current)
    pattern = re.compile(
        re.escape(SOURCE_BLOCK_START) + r".*?" + re.escape(SOURCE_BLOCK_END),
        re.DOTALL,
    )
    preserved = pattern.sub("", text).strip("； \r\n")
    block = _source_block(references)
    return f"{preserved}；{block}" if preserved else block


def _source_process(reference: dict[str, Any]) -> tuple[str | None, str, str | None]:
    raw = _text(reference.get("base_process"))
    tokens: list[str] = []
    if "轧" in raw or reference.get("box_category") == "die_cut":
        tokens.append("模切")
    elif reference.get("box_category") == "normal":
        tokens.append("开槽")
    if "钉" in raw:
        tokens.append("打钉")
    if "贴" in raw:
        tokens.append("粘贴")
    if raw == "裁切" or raw == "其他":
        tokens.append("其他")
    tokens = list(dict.fromkeys(tokens))
    if "红" in raw:
        return "、".join(tokens) or None, "单色印刷", "红色"
    if "黑" in raw:
        return "、".join(tokens) or None, "单色印刷", "黑色"
    return "、".join(tokens) or None, "无印刷", None


def _new_product_safety(
    references: list[dict[str, Any]],
    canonical: dict[str, Any],
    material: Material | None,
    material_error: str | None,
) -> list[str]:
    reasons: list[str] = []
    if len(references) > 1:
        reasons.append("同一客户款号存在多行资料，结构/BOM待人工确认")
    if material is None:
        reasons.append(material_error or "材质待确认")
    if not _yield_is_explicit(canonical):
        reasons.append("模切用纸尺寸未明确一开几")
    if canonical.get("report_width_mm") is None or canonical.get("report_length_mm") is None:
        reasons.append("用纸长宽不完整")
    if canonical.get("box_category") == "normal" and any(
        canonical.get(field) is None for field in ("length_mm", "width_mm", "height_mm")
    ):
        reasons.append("纸箱成品长宽高不完整")
    if canonical.get("sale_unit_price") is None:
        reasons.append("基础资料现行单价为空")
    return list(dict.fromkeys(reasons))


def _payload(
    *,
    customer: Customer,
    references: list[dict[str, Any]],
    canonical: dict[str, Any],
    material: Material | None,
    existing: Product | None,
    unsafe_reasons: list[str],
) -> ProductPayload:
    if existing is not None:
        # Historical products can contain legacy zero/partial values that strict
        # create validation intentionally rejects.  Build the update carrier
        # from the stored snapshot, then replace every source-owned field below.
        payload = ProductPayload.model_construct(
            **_product_payload_snapshot(existing)
        )
        for field in ("length_mm", "width_mm", "height_mm"):
            value = getattr(payload, field, None)
            if value is not None and Decimal(str(value)) <= 0:
                setattr(payload, field, None)
    else:
        process, print_content, printing_colors = _source_process(canonical)
        payload = ProductPayload(
            customer_id=customer.id,
            product_code=canonical["product_code"],
            customer_material_code=canonical["product_code"],
            product_name=canonical["product_name"],
            material_id=None,
            legacy_material_text=canonical.get("legacy_material_text"),
            length_mm=canonical.get("length_mm"),
            width_mm=canonical.get("width_mm"),
            height_mm=canonical.get("height_mm"),
            box_category=canonical["box_category"],
            box_style="A1" if canonical["box_category"] == "normal" else "模切内盒",
            print_content=print_content,
            printing_colors=printing_colors,
            production_process=process,
            unit="只",
            sale_unit_price=canonical.get("sale_unit_price"),
            sale_unit_price_no_tax=(
                canonical.get("sale_unit_price")
                if customer.customer_code in {"YKE", "KEW"}
                else None
            ),
            report_length_mm=canonical.get("report_length_mm"),
            report_width_mm=canonical.get("report_width_mm"),
            crease_type=canonical.get("crease_type"),
            crease_left_mm=canonical.get("crease_left_mm"),
            crease_middle_mm=canonical.get("crease_middle_mm"),
            crease_right_mm=canonical.get("crease_right_mm"),
            flute_type=canonical.get("flute_type"),
            layer_count=canonical.get("layer_count"),
            default_cutting_mode=canonical.get("default_cutting_mode") or "一开一",
            is_active=not unsafe_reasons,
        )
    payload.customer_id = customer.id
    payload.product_code = canonical["product_code"]
    payload.customer_material_code = canonical["product_code"]
    if not (existing is not None and canonical["product_code"] == "80012500"):
        payload.product_name = canonical["product_name"]
    payload.material_id = material.id if material is not None else None
    payload.legacy_material_text = canonical.get("legacy_material_text")
    for field in (
        "length_mm",
        "width_mm",
        "height_mm",
        "report_length_mm",
        "report_width_mm",
        "crease_type",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
        "flute_type",
        "layer_count",
    ):
        value = canonical.get(field)
        if value is not None or existing is None:
            setattr(payload, field, value)
    payload.default_cutting_mode = canonical.get("default_cutting_mode") or "一开一"
    if canonical.get("sale_unit_price") is not None:
        payload.sale_unit_price = canonical["sale_unit_price"]
        if customer.customer_code in {"YKE", "KEW"}:
            payload.sale_unit_price_no_tax = canonical["sale_unit_price"]
    payload.remark = _merge_source_remark(
        existing.remark if existing is not None else None,
        references,
    )
    if existing is None:
        payload.is_active = not unsafe_reasons
    elif unsafe_reasons and material is None:
        payload.is_active = False
    return payload


def _load_inputs(
    reference_path: Path,
    volume_paths: tuple[Path, ...],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    reference_hash = _sha256_file(reference_path)
    if reference_hash != EXPECTED_REFERENCE_SHA256:
        raise RuntimeError(f"基础资料SHA-256变化：{reference_hash}")
    volume_hashes = tuple(_sha256_file(path) for path in volume_paths)
    if volume_hashes != EXPECTED_VOLUME_SHA256:
        raise RuntimeError(f"五卷SHA-256变化：{volume_hashes}")
    references, errors = read_mixed_reference_workbook(reference_path.read_bytes())
    if errors:
        raise RuntimeError(f"基础资料解析异常：{errors}")
    if len(references) != EXPECTED_REFERENCE_ROWS:
        raise RuntimeError(f"基础资料有编码行数变化：{len(references)}")
    registrations: dict[str, dict[str, Any]] = {}
    registration_errors: list[dict[str, Any]] = []
    for volume_path in volume_paths:
        rows, row_errors = read_mixed_sample_workbook(volume_path.read_bytes())
        registration_errors.extend(row_errors)
        for incoming in rows:
            sample_id = incoming["sample_id"]
            existing = registrations.get(sample_id)
            if existing is None:
                registrations[sample_id] = incoming
            else:
                error = merge_registration(existing, incoming)
                if error:
                    raise RuntimeError(f"{sample_id}跨卷合并失败：{error}")
    if registration_errors:
        raise RuntimeError(f"样品卷解析异常：{registration_errors}")
    return references, registrations, {
        "reference": {"path": str(reference_path), "sha256": reference_hash},
        "volumes": [
            {"path": str(path), "sha256": digest}
            for path, digest in zip(volume_paths, volume_hashes, strict=True)
        ],
        "reference_rows": len(references),
        "registrations": len(registrations),
    }


def _registration_map(
    references: list[dict[str, Any]],
    registrations: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for registration in registrations.values():
        for reference in reference_candidates(
            references,
            sample_code=registration["product_code"],
            customer_hint=registration.get("customer_hint"),
        ):
            key = (reference["customer_code"], reference["product_code"])
            if registration not in result[key]:
                result[key].append(registration)
    return result


def _changed_fields(existing: Product | None, payload: ProductPayload) -> list[str]:
    if existing is None:
        return sorted(payload.model_dump())
    result = []
    for field, value in payload.model_dump().items():
        current = getattr(existing, field, None)
        if not _same_value(current, value):
            result.append(field)
    return sorted(result)


def _same_value(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float, Decimal)) and isinstance(
        right, (int, float, Decimal)
    ):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except InvalidOperation:
            return str(left) == str(right)
    return str(left) == str(right)


def _build_plan(
    db: Session,
    references: list[dict[str, Any]],
    registrations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    customers = {
        row.customer_code: row
        for row in db.scalars(
            select(Customer).where(
                func.upper(func.trim(Customer.customer_code)).in_(("YL", "YKE", "KEW"))
            )
        ).all()
    }
    if set(customers) != {"YL", "YKE", "KEW"}:
        raise RuntimeError(f"三客户主档不完整：{sorted(customers)}")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for reference in references:
        groups[(reference["customer_code"], reference["product_code"])].append(reference)
    if len(groups) != EXPECTED_UNIQUE_PRODUCTS:
        raise RuntimeError(f"基础资料唯一款号数变化：{len(groups)}")
    registrations_by_key = _registration_map(references, registrations)
    rows = []
    for key, group_references in sorted(groups.items()):
        customer = customers[key[0]]
        existing = _find_product(db, customer.id, key[1])
        canonical = _canonical_reference(group_references, existing)
        material, material_codes, material_error = _material_resolution(
            db, group_references
        )
        if existing is not None and key == ("YKE", "80012500"):
            material = db.get(Material, existing.material_id)
            material_error = None
        unsafe_reasons = _new_product_safety(
            group_references,
            canonical,
            material,
            material_error,
        )
        try:
            payload = _payload(
                customer=customer,
                references=group_references,
                canonical=canonical,
                material=material,
                existing=existing,
                unsafe_reasons=unsafe_reasons,
            )
        except Exception as error:
            raise RuntimeError(f"生成产品计划失败 {key[0]}/{key[1]}：{error}") from error
        changes = _changed_fields(existing, payload)
        drawing_count = 0
        if existing is not None:
            drawing_count = int(
                db.scalar(
                    select(func.count(ProductDrawing.id)).where(
                        ProductDrawing.product_id == existing.id
                    )
                )
                or 0
            )
        attached_registrations = registrations_by_key.get(key, [])
        rows.append(
            {
                "customer_code": key[0],
                "product_code": key[1],
                "source_rows": [row["source_row"] for row in group_references],
                "canonical_source_row": canonical["source_row"],
                "status": "create" if existing is None else ("update" if changes else "unchanged"),
                "existing_id": existing.id if existing is not None else None,
                "changed_fields": changes,
                "material_codes": material_codes,
                "material_id": material.id if material is not None else None,
                "material_error": material_error,
                "unsafe_reasons": unsafe_reasons,
                "is_active_after": payload.is_active,
                "drawing_count_before": drawing_count,
                "sample_ids": [row["sample_id"] for row in attached_registrations],
                "_customer": customer,
                "_existing": existing,
                "_payload": payload,
                "_references": group_references,
                "_registrations": attached_registrations,
            }
        )
    remaining_old_vik = db.scalars(
        select(Product).where(
            Product.material_id == OLD_VIK_MATERIAL_ID,
            Product.deleted_at.is_(None),
        )
    ).all()
    create_count = sum(row["status"] == "create" for row in rows)
    return {
        "customers": customers,
        "rows": rows,
        "remaining_old_vik": remaining_old_vik,
        "summary": {
            "reference_rows": len(references),
            "unique_products": len(rows),
            "create": create_count,
            "update": sum(row["status"] == "update" for row in rows),
            "unchanged": sum(row["status"] == "unchanged" for row in rows),
            "inactive_after": sum(not row["is_active_after"] for row in rows),
            "new_inactive": sum(
                row["status"] == "create" and not row["is_active_after"] for row in rows
            ),
            "duplicate_source_products": sum(len(row["source_rows"]) > 1 for row in rows),
            "unresolved_material_products": sum(row["material_id"] is None for row in rows),
            "remaining_old_vik": len(remaining_old_vik),
            "new_products_with_sample_drawings": sum(
                row["status"] == "create" and bool(row["sample_ids"]) for row in rows
            ),
            "by_customer": {
                code: sum(row["customer_code"] == code for row in rows)
                for code in ("YL", "YKE", "KEW")
            },
        },
    }


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": plan["summary"],
        "products": [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in plan["rows"]
        ],
        "remaining_old_vik_product_ids": [row.id for row in plan["remaining_old_vik"]],
    }


def _upsert(db: Session, row: dict[str, Any], user: User) -> tuple[Product, str]:
    existing = _find_product(
        db,
        row["_customer"].id,
        row["product_code"],
    )
    payload: ProductPayload = row["_payload"]
    if existing is None:
        product = _create_product(
            db,
            payload=payload,
            user=user,
            allow_unregistered_sample_mold=True,
        )
        return product, "created"
    before_version = existing.version
    product = _update_product(
        db,
        product=existing,
        payload=payload,
        expected_version=existing.version,
        user=user,
        allow_unregistered_sample_mold=True,
    )
    return product, "updated" if product.version != before_version else "unchanged"


def _rebind_remaining_vik(db: Session, user: User) -> list[int]:
    new_material = db.get(Material, VIK_MATERIAL_ID)
    if new_material is None or not new_material.is_active or new_material.code != "VIK":
        raise RuntimeError("VIK正式主档不存在、停用或代码不一致")
    changed: list[int] = []
    for product in db.scalars(
        select(Product).where(
            Product.material_id == OLD_VIK_MATERIAL_ID,
            Product.deleted_at.is_(None),
        )
    ).all():
        before = product.material_id
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates={"material_id": VIK_MATERIAL_ID},
            expected_version=product.version,
            user=user,
            reason="老板确认B+Z与既有VIK统一改绑正式VIK主档",
            source="controlled_import.full_reference_catalog_20260810",
            action="system_mapping",
        )
        audit_master_change(
            db,
            user=user,
            action="UPDATE",
            resource="Product",
            resource_id=product.id,
            details={
                "batch_id": BATCH_ID,
                "field": "material_id",
                "before": before,
                "after": VIK_MATERIAL_ID,
            },
        )
        changed.append(product.id)
    return changed


def _verify(db: Session, plan: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    source_row_count = 0
    unresolved_active = []
    for row in plan["rows"]:
        product = _find_product(
            db,
            plan["customers"][row["customer_code"]].id,
            row["product_code"],
        )
        if product is None:
            problems.append(f"缺少产品 {row['customer_code']}/{row['product_code']}")
            continue
        remark = product.remark or ""
        match = re.search(
            re.escape(SOURCE_BLOCK_START) + r"(.*?)" + re.escape(SOURCE_BLOCK_END),
            remark,
            re.DOTALL,
        )
        if match is None:
            problems.append(f"缺少完整源行 {row['customer_code']}/{row['product_code']}")
        else:
            try:
                source_row_count += len(json.loads(match.group(1)))
            except json.JSONDecodeError:
                problems.append(f"源行JSON损坏 {row['customer_code']}/{row['product_code']}")
        if row["material_id"] is None and product.is_active:
            unresolved_active.append(f"{row['customer_code']}/{row['product_code']}")
    old_vik = int(
        db.scalar(
            select(func.count(Product.id)).where(
                Product.material_id == OLD_VIK_MATERIAL_ID,
                Product.deleted_at.is_(None),
            )
        )
        or 0
    )
    if old_vik:
        problems.append(f"仍有{old_vik}个产品绑定旧VIK身份")
    if source_row_count != EXPECTED_REFERENCE_ROWS:
        problems.append(f"完整源行回读数量错误：{source_row_count}")
    if unresolved_active:
        problems.append(f"未解决材质仍启用：{unresolved_active}")
    if problems:
        raise RuntimeError("；".join(problems))
    return {
        "unique_products": len(plan["rows"]),
        "source_rows_preserved": source_row_count,
        "old_vik_remaining": old_vik,
        "unresolved_material_active": len(unresolved_active),
    }


def _apply(
    db: Session,
    *,
    plan: dict[str, Any],
    user: User,
    fail_after_products: int | None,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    result_rows = []
    saved_paths: list[tuple[str, str]] = []
    try:
        for row in plan["rows"]:
            product, status = _upsert(db, row, user)
            drawing_created = drawing_skipped = 0
            if row["_registrations"] and row["drawing_count_before"] == 0:
                drawing_created, drawing_skipped, _evidence = _add_drawings(
                    db,
                    product=product,
                    registrations=row["_registrations"],
                    user=user,
                    saved_paths=saved_paths,
                )
            result_rows.append(
                {
                    "customer_code": row["customer_code"],
                    "product_code": row["product_code"],
                    "product_id": product.id,
                    "status": status,
                    "drawings_created": drawing_created,
                    "drawings_skipped": drawing_skipped,
                }
            )
            if fail_after_products and len(result_rows) >= fail_after_products:
                raise RuntimeError("隔离演练注入故障：验证整批事务与图片回滚")
        rebound_ids = _rebind_remaining_vik(db, user)
        readback = _verify(db, plan)
        for customer_code, customer in plan["customers"].items():
            customer_rows = [row for row in result_rows if row["customer_code"] == customer_code]
            audit_master_change(
                db,
                user=user,
                action="BATCH_IMPORT",
                resource="FullReferenceCatalogSync",
                resource_id=customer.id,
                details={
                    "batch_id": BATCH_ID,
                    "customer_code": customer_code,
                    "products": len(customer_rows),
                    "created": sum(row["status"] == "created" for row in customer_rows),
                    "updated": sum(row["status"] == "updated" for row in customer_rows),
                    "drawings_created": sum(row["drawings_created"] for row in customer_rows),
                },
            )
        return {
            "summary": {
                "products": len(result_rows),
                "created": sum(row["status"] == "created" for row in result_rows),
                "updated": sum(row["status"] == "updated" for row in result_rows),
                "unchanged": sum(row["status"] == "unchanged" for row in result_rows),
                "drawings_created": sum(row["drawings_created"] for row in result_rows),
                "drawings_skipped": sum(row["drawings_skipped"] for row in result_rows),
                "vik_rebound_after_catalog_updates": len(rebound_ids),
            },
            "readback": readback,
            "products": result_rows,
            "vik_rebound_product_ids": rebound_ids,
        }, saved_paths
    except Exception:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise


def _write_report(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _run(args: argparse.Namespace) -> dict[str, Any]:
    database = args.database.resolve()
    reference = args.reference.resolve()
    volumes = tuple(path.resolve() for path in args.volume)
    if len(volumes) != 5:
        raise RuntimeError("必须提供五卷样品登记文件")
    is_formal = _assert_database_target(
        database,
        allow_formal_database=args.allow_formal_database,
    )
    references, registrations, input_evidence = _load_inputs(reference, volumes)
    before = _database_checks(database)
    if before.integrity_check.lower() != "ok" or before.foreign_key_violations:
        raise RuntimeError(f"数据库写入前检查失败：{asdict(before)}")
    if before.alembic_revision != EXPECTED_SCHEMA_REVISION:
        raise RuntimeError(
            f"数据库版本不匹配：{before.alembic_revision} != {EXPECTED_SCHEMA_REVISION}"
        )
    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            if not args.apply:
                db.connection().exec_driver_sql("PRAGMA query_only = ON")
            plan = _build_plan(db, references, registrations)
        if not args.apply:
            return {
                "batch_id": BATCH_ID,
                "mode": "dry_run",
                "database": str(database),
                "formal_database": is_formal,
                "input": input_evidence,
                "checks": asdict(before),
                "plan": _public_plan(plan),
            }
        expected_confirmation = (
            FORMAL_CONFIRMATION if is_formal else REHEARSAL_CONFIRMATION
        )
        if args.confirm != expected_confirmation:
            raise RuntimeError(
                f"--apply 必须同时提供 --confirm {expected_confirmation}"
            )
        if not args.expected_database_sha256:
            raise RuntimeError("--apply 必须提供 --expected-database-sha256")
        if before.sha256 != args.expected_database_sha256.upper():
            raise RuntimeError("目标数据库SHA-256已变化，拒绝执行")
        if is_formal and plan["summary"]["create"] != EXPECTED_INITIAL_MISSING_PRODUCTS:
            raise RuntimeError(
                f"正式首次补录必须恰好新增{EXPECTED_INITIAL_MISSING_PRODUCTS}款，实际{plan['summary']['create']}"
            )
        with factory() as db:
            actor = db.scalar(select(User).where(User.username == args.actor))
            if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
                raise RuntimeError("执行人必须是启用的admin或boss账号")
            live_plan = _build_plan(db, references, registrations)
            if live_plan["summary"] != plan["summary"]:
                raise RuntimeError("事务开始前计划已变化，拒绝执行")
            apply_result, saved_paths = _apply(
                db,
                plan=live_plan,
                user=actor,
                fail_after_products=args.fail_after_products,
            )
            db.commit()
        after = _database_checks(database)
        for table in PROTECTED_TABLES:
            if before.counts.get(table) != after.counts.get(table):
                raise RuntimeError(f"保护表意外变化：{table}")
        if after.counts["products"] - before.counts["products"] != plan["summary"]["create"]:
            raise RuntimeError("产品数量增量不符合计划")
        if after.integrity_check.lower() != "ok" or after.foreign_key_violations:
            raise RuntimeError(f"数据库写入后检查失败：{asdict(after)}")
        with factory() as db:
            db.connection().exec_driver_sql("PRAGMA query_only = ON")
            after_plan = _build_plan(db, references, registrations)
        return {
            "batch_id": BATCH_ID,
            "mode": "apply",
            "database": str(database),
            "formal_database": is_formal,
            "input": input_evidence,
            "database_before": asdict(before),
            "plan_before": _public_plan(plan),
            "apply": apply_result,
            "database_after": asdict(after),
            "plan_after": _public_plan(after_plan),
        }
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="YL/YKE/KEW基础资料433款完整同步与VIK统一改绑"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--volume", type=Path, action="append", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--expected-database-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--allow-formal-database", action="store_true")
    parser.add_argument("--fail-after-products", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = _run(args)
    except Exception as error:
        payload = {"batch_id": BATCH_ID, "ok": False, "error": str(error)}
        _write_report(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    _write_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

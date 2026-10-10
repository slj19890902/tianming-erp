from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
import json
import os
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
from app.api.product_import import (  # noqa: E402
    _create_product,
    _existing_drawing_source_hashes,
    _mixed_registration_row,
    _update_product,
)
from app.api.products import ProductPayload  # noqa: E402
from app.core.database import create_sqlite_engine  # noqa: E402
from app.models.audit import OperationLog  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.material import Material  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.product_bom import ProductBomComponent  # noqa: E402
from app.models.product_drawing import ProductDrawing  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.composite_bom import replace_product_bom  # noqa: E402
from app.services.mixed_sample_import import (  # noqa: E402
    collapse_reference_candidates,
    merge_registration,
    normalize_sample_code,
    read_mixed_reference_workbook,
    read_mixed_sample_workbook,
    reference_candidates,
)
from app.services.product_drawings import (  # noqa: E402
    remove_drawing_files,
    save_product_drawing_files,
)
from app.services.secure_uploads import (  # noqa: E402
    DRAWING_POLICY,
    validate_upload_bytes,
)


BATCH_ID = "MIXED_SAMPLE_CATALOG_20260810"
FORMAL_CONFIRMATION = "APPLY_MIXED_SAMPLE_CATALOG_FORMAL_20260810"
REHEARSAL_CONFIRMATION = "APPLY_MIXED_SAMPLE_CATALOG_REHEARSAL_20260810"
FORMAL_DATABASE = Path(
    os.getenv("ERP_FORMAL_DATABASE_PATH", str(ROOT / "data" / "carton_erp.sqlite3"))
).resolve()
EXPECTED_SCHEMA_REVISION = "dy07v8x9z96"
EXPECTED_REFERENCE_SHA256 = (
    "C4F60DB29EB9A9B48B1BEED40522D024D0F0B3AEE39F75DCEB0AE695D3F43303"
)
EXPECTED_VOLUME_SHA256 = (
    "30E5952EE7DF4EB457E4F69F40F0187A3973B3106D6649FD52BD8B342E76D5A3",
    "A475998C5E6ACE3FF5908A9597E7A12FF39CEB3FE0DD7304F7BDAAC580105CD6",
    "421753ED7572703FF9A57B26FC5B3F6E2D9F6B65A4D6E23B41CB67122E848D57",
    "4D19622397BA540A0A5712B8777448346B4376CA68F47FD10DBBA04F97EC994A",
    "8F425E6A5DA699B0C3222C13720E9876B150CC1F75B4D650D07B165B482EFECC",
)
EXPECTED_INPUT_COUNTS = {
    "registrations": 210,
    "unique_product_codes": 207,
    "embedded_drawings": 268,
}

EXCLUDED_SAMPLES: dict[str, str] = {
    "YP017": "三客户基础资料无精确型号",
    "YP034": "三客户基础资料无精确型号",
    "YP037": "三客户基础资料无精确型号",
    "YP038": "三客户基础资料无精确型号",
    "YP056": "三客户基础资料无精确型号",
    "YP066": "三客户基础资料无精确型号",
    "YP071": "三客户基础资料无精确型号",
    "YP084": "三客户基础资料无精确型号",
    "YP100": "三客户基础资料无精确型号",
    "YP107": "三客户基础资料无精确型号",
    "YP108": "三客户基础资料无精确型号",
    "YP109": "三客户基础资料无精确型号",
    "YP113": "三客户基础资料无精确型号",
    "YP118": "三客户基础资料无精确型号",
    "YP121": "三客户基础资料无精确型号",
    "YP122": "三客户基础资料无精确型号",
    "YP123": "三客户基础资料无精确型号",
    "YP144": "三客户基础资料无精确型号",
    "YP172": "三客户基础资料无精确型号",
    "YP208": "三客户基础资料无精确型号",
    "YP149": "Z+B 材质主档未建立",
    "YP201": "450克灰底白板材质主档未建立",
}

# These are stable ERP material identities confirmed by the preceding 131-row
# common-box import and the 2026-08-10 owner decisions.  Matching is performed
# on the source's legacy material text; no new material master is invented here.
MATERIAL_RULES: tuple[tuple[str, int], ...] = (
    (r"^VIK(?:[/（(]B[）)]?)?$", 609),
    (r"^B\+Z/B$", 609),
    (r"^DRD(?:[/（(]A[）)]?)?$", 619),
    (r"^VSNIV(?:/AB)?$", 610),
    (r"^6N6(?:[/（(]E[）)]?)?$", 616),
    (r"^8IV(?:[/（(]B[）)]?)?$", 614),
    (r"^KSNIK$", 615),
    (r"^PSNSP/AB$", 539),
    (r"^6N1\+5(?:\(AB\)|/AB)?$", 621),
    (r"^VINIV/AB$", 629),
    (r"^D\+D(?:[/（(]A[）)]?)?$", 622),
    (r"^611\+5(?:/AB)?$", 412),
    (r"^7R1S6/AB$", 405),
    (r"^9\+BR9/AB$", 413),
    (r"^D\+1RC/AB$", 415),
    (r"^JRBSJ/AB$", 406),
    (r"^PSP\(E\)$", 502),
    (r"^VSV\(B\)$", 520),
    (r"^B\+\+\+Z$", 613),
    (r"^8INIV$", 612),
)

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
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _database_checks(path: Path) -> DatabaseChecks:
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        fk_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in WATCH_TABLES
            if table in tables
        }
        revision_row = (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()
            if "alembic_version" in tables
            else None
        )
    return DatabaseChecks(
        sha256=_sha256_file(path),
        integrity_check=integrity,
        foreign_key_violations=len(fk_rows),
        alembic_revision=str(revision_row[0]) if revision_row else None,
        counts=counts,
    )


def _online_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"备份目标已存在，拒绝覆盖：{destination}")
    with closing(sqlite3.connect(source, timeout=30)) as source_db:
        source_db.execute("PRAGMA busy_timeout=30000")
        with closing(sqlite3.connect(destination, timeout=30)) as target_db:
            source_db.backup(target_db)
            target_db.commit()


def _normalize_material_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def _material_id_for(reference: dict[str, Any]) -> int:
    text = _normalize_material_text(reference.get("legacy_material_text"))
    for pattern, material_id in MATERIAL_RULES:
        if re.fullmatch(pattern, text):
            return material_id
    raise RuntimeError(
        f"没有冻结材质映射：{reference['customer_code']}/{reference['product_code']}={text!r}"
    )


def _load_inputs(
    reference_path: Path,
    volume_paths: tuple[Path, ...],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    if _sha256_file(reference_path) != EXPECTED_REFERENCE_SHA256:
        raise RuntimeError("三客户基础资料 SHA-256 已变化，拒绝执行")
    if len(volume_paths) != 5:
        raise RuntimeError("样品工作簿必须恰好为5卷")
    actual_hashes = tuple(_sha256_file(path) for path in volume_paths)
    if actual_hashes != EXPECTED_VOLUME_SHA256:
        raise RuntimeError(f"五卷样品 SHA-256 已变化：{actual_hashes}")

    references, reference_errors = read_mixed_reference_workbook(
        reference_path.read_bytes()
    )
    if reference_errors:
        raise RuntimeError(f"基础资料解析异常：{reference_errors}")
    registrations: dict[str, dict[str, Any]] = {}
    drawing_count = 0
    for path in volume_paths:
        rows, errors = read_mixed_sample_workbook(path.read_bytes())
        if errors:
            raise RuntimeError(f"样品卷解析异常 {path.name}：{errors}")
        for row in rows:
            incoming = dict(row)
            incoming["_source_rows"] = [
                {"file": path.name, "row_number": row["row_number"]}
            ]
            key = normalize_sample_code(row["sample_id"])
            existing = registrations.get(key)
            if existing is None:
                registrations[key] = incoming
            else:
                error = merge_registration(existing, incoming)
                existing.setdefault("_source_rows", []).extend(
                    incoming["_source_rows"]
                )
                if error:
                    raise RuntimeError(f"样品 {key} 跨卷合并失败：{error}")
            drawing_count += len(row.get("_embedded_drawings") or ())

    counts = {
        "registrations": len(registrations),
        "unique_product_codes": len(
            {normalize_sample_code(row["product_code"]) for row in registrations.values()}
        ),
        "embedded_drawings": drawing_count,
    }
    if counts != EXPECTED_INPUT_COUNTS:
        raise RuntimeError(f"冻结输入数量已变化：{counts}")
    return references, registrations, {
        "reference": {
            "path": str(reference_path),
            "sha256": EXPECTED_REFERENCE_SHA256,
        },
        "volumes": [
            {"path": str(path), "sha256": digest}
            for path, digest in zip(volume_paths, actual_hashes, strict=True)
        ],
        "counts": counts,
    }


def _special_candidates(
    sample_id: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if sample_id == "YP133":
        selected = [
            row
            for row in candidates
            if (row["customer_code"], row["source_row"])
            in {("YKE", 75), ("KEW", 44)}
        ]
        if len(selected) != 2:
            raise RuntimeError("YP133 冻结候选行已变化")
        return selected
    if sample_id == "YP148":
        selected = [
            row
            for row in candidates
            if row["customer_code"] == "YL" and row["source_row"] == 19
        ]
        if len(selected) != 1:
            raise RuntimeError("YP148 冻结候选行已变化")
        return selected
    return []


def _select_references(
    references: list[dict[str, Any]],
    sample_id: str,
    registration: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates = collapse_reference_candidates(
        reference_candidates(
            references,
            sample_code=registration["product_code"],
            customer_hint=registration.get("customer_hint"),
        )
    )
    special = _special_candidates(sample_id, candidates)
    if special:
        return special
    if not candidates:
        raise RuntimeError(f"未排除的样品没有基础资料：{sample_id}")
    by_customer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_customer[candidate["customer_code"]].append(candidate)
    if any(len(rows) != 1 for rows in by_customer.values()):
        raise RuntimeError(f"未冻结的同客户多候选：{sample_id}")
    # Cross-customer samples are common products: one product per customer.
    return [rows[0] for _customer, rows in sorted(by_customer.items())]


def _canonical_process(
    registrations: Iterable[dict[str, Any]],
    references: Iterable[dict[str, Any]],
) -> tuple[str, str, bool, str | None, list[str]]:
    registration_rows = list(registrations)
    reference_rows = list(references)
    forming_values = {row.get("forming_method") for row in registration_rows}
    joining_values = {row.get("joining_method") for row in registration_rows}
    base_process = "、".join(str(row.get("base_process") or "") for row in reference_rows)
    if not ({"模切", "开槽"} & forming_values):
        if "轧" in base_process or any(row["box_category"] == "die_cut" for row in reference_rows):
            forming_values.add("模切")
        else:
            forming_values.add("开槽")
    if not ({"打钉", "粘合"} & joining_values):
        if "钉" in base_process:
            joining_values.add("打钉")
        elif "贴" in base_process:
            joining_values.add("粘合")
    printed = any(bool(row.get("printed")) for row in registration_rows)
    colors = [
        str(row.get("printing_colors") or "").strip()
        for row in registration_rows
        if row.get("printed")
    ]
    color = next((item for item in colors if item and item != "无"), None)
    if printed and not color:
        color = "黑色"
    tokens: list[str] = []
    if "模切" in forming_values:
        tokens.append("模切")
    elif "开槽" in forming_values:
        tokens.append("开槽")
    if "打钉" in joining_values:
        tokens.append("打钉")
    if "粘合" in joining_values:
        tokens.append("粘贴")
    if any(bool(row.get("secondary_gluing")) for row in registration_rows):
        tokens.append("二次粘合")
    if not tokens and "其他" in base_process:
        tokens.append("其他")
    box_category = "die_cut" if "模切" in tokens else "normal"
    box_style = "模切内盒" if box_category == "die_cut" else "A1"
    return box_category, box_style, printed, color, tokens


def _target_payload(
    *,
    db: Session,
    customer: Customer,
    reference: dict[str, Any],
    registrations: list[dict[str, Any]],
    product_code: str,
    product_name: str | None = None,
    component: bool = False,
) -> ProductPayload:
    material = db.get(Material, _material_id_for(reference))
    if material is None or not material.is_active:
        raise RuntimeError(f"冻结材质主档不存在或已停用：{reference['legacy_material_text']}")
    box_category, box_style, printed, color, process_tokens = _canonical_process(
        registrations,
        [reference],
    )
    flute = material.flute_type or reference.get("flute_type")
    layer_count = material.layer_count or reference.get("layer_count")
    report_length = reference.get("report_length_mm")
    report_width = reference.get("report_width_mm")
    if product_code == "80011980":
        report_length, report_width = 570, 425
    price = None if component else reference.get("sale_unit_price")
    return ProductPayload(
        customer_id=customer.id,
        product_code=product_code,
        customer_material_code=product_code,
        product_name=product_name or reference["product_name"],
        material_id=material.id,
        mold_tool_id=None,
        legacy_material_text=reference.get("legacy_material_text"),
        length_mm=reference.get("length_mm"),
        width_mm=reference.get("width_mm"),
        height_mm=reference.get("height_mm"),
        box_category=box_category,
        box_style=box_style,
        print_content="单色印刷" if printed else "无印刷",
        printing_colors=color if printed else None,
        production_process="、".join(process_tokens) or None,
        unit="只",
        sale_unit_price=price,
        sale_unit_price_no_tax=(
            price if customer.customer_code in {"YKE", "KEW"} else None
        ),
        report_length_mm=report_length,
        report_width_mm=report_width,
        crease_type=reference.get("crease_type"),
        crease_left_mm=reference.get("crease_left_mm"),
        crease_middle_mm=reference.get("crease_middle_mm"),
        crease_right_mm=reference.get("crease_right_mm"),
        flute_type=flute,
        layer_count=layer_count,
        default_cutting_mode=reference.get("default_cutting_mode") or "一开一",
        remark=(
            f"{BATCH_ID}；样品={','.join(row['sample_id'] for row in registrations)}；"
            f"基础资料={reference['customer_code']}行{reference['source_row']}"
        ),
        is_active=True,
    )


def _find_product(db: Session, customer_id: int, product_code: str) -> Product | None:
    rows = db.scalars(
        select(Product).where(
            Product.customer_id == customer_id,
            or_(
                func.upper(Product.product_code) == product_code.upper(),
                func.upper(Product.customer_material_code) == product_code.upper(),
            ),
        )
    ).all()
    if len(rows) > 1:
        raise RuntimeError(
            f"同一客户的存货编码/客户料号分别命中多条产品：{customer_id}/{product_code}"
        )
    return rows[0] if rows else None


def _upsert_product(
    db: Session,
    *,
    customer: Customer,
    payload: ProductPayload,
    user: User,
) -> tuple[Product, str, tuple[str, ...]]:
    existing = _find_product(db, customer.id, payload.product_code)
    if existing is None:
        product = _create_product(
            db,
            payload=payload,
            user=user,
            allow_unregistered_sample_mold=True,
        )
        return product, "created", tuple(sorted(payload.model_dump()))
    payload = payload.model_copy(deep=True)
    # These fields are ERP-owned operational bindings/settings and are not
    # present in the customer's sample/reference workbooks.
    for field_name in (
        "mold_tool_id",
        "printing_plate_mode",
        "printing_plate_1_id",
        "printing_plate_2_id",
        "printing_plate_3_id",
        "plate_alignment_value_mm",
        "plate_mount_value_mm",
        "machine_set_length_mm",
        "machine_set_width_mm",
        "machine_set_height_mm",
        "surface_paper_type",
        "cost_unit_price",
        "board_price",
        "suggested_price",
        "die_cut_path",
        "base_report_length_mm",
        "base_report_width_mm",
        "base_crease_type",
        "base_crease_left_mm",
        "base_crease_middle_mm",
        "base_crease_right_mm",
        "base_report_notes",
        "splice_mode",
        "pieces_per_box",
        "flap_mm",
        "combination_mode",
    ):
        setattr(payload, field_name, getattr(existing, field_name))
    # A blank cell in the customer reference workbook is not evidence that a
    # previously accepted ERP dimension/crease value should be erased.
    for field_name in (
        "mold_tool_id",
        "printing_plate_mode",
        "printing_plate_1_id",
        "printing_plate_2_id",
        "printing_plate_3_id",
        "plate_alignment_value_mm",
        "plate_mount_value_mm",
        "machine_set_length_mm",
        "machine_set_width_mm",
        "machine_set_height_mm",
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
        "surface_paper_type",
        "sale_unit_price_no_tax",
        "cost_unit_price",
        "board_price",
        "suggested_price",
        "die_cut_path",
        "base_report_length_mm",
        "base_report_width_mm",
        "base_crease_type",
        "base_crease_left_mm",
        "base_crease_middle_mm",
        "base_crease_right_mm",
        "base_report_notes",
        "splice_mode",
        "pieces_per_box",
        "flap_mm",
    ):
        if getattr(payload, field_name) is None:
            setattr(payload, field_name, getattr(existing, field_name))
    before_version = existing.version
    before = {key: getattr(existing, key, None) for key in payload.model_dump()}
    product = _update_product(
        db,
        product=existing,
        payload=payload,
        expected_version=existing.version,
        user=user,
        allow_unregistered_sample_mold=True,
    )
    changed = tuple(
        sorted(
            key
            for key, value in payload.model_dump().items()
            if str(before.get(key)) != str(value)
        )
    )
    return product, ("updated" if product.version != before_version else "unchanged"), changed


def _validated_upload(filename: str, content: bytes):
    suffix = Path(filename).suffix.lower()
    content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return validate_upload_bytes(
        content=content,
        filename=filename,
        content_type=content_type,
        policy=DRAWING_POLICY,
    )


def _add_drawings(
    db: Session,
    *,
    product: Product,
    registrations: Iterable[dict[str, Any]],
    user: User,
    saved_paths: list[tuple[str, str]],
) -> tuple[int, int, list[dict[str, Any]]]:
    created = 0
    skipped = 0
    evidence: list[dict[str, Any]] = []
    existing_hashes = _existing_drawing_source_hashes(db, product.id)
    seen_batch: set[str] = set()
    for registration in registrations:
        names = list(registration.get("drawing_filenames") or ())
        drawings = list(registration.get("_embedded_drawings") or ())
        if len(names) != len(drawings):
            raise RuntimeError(f"{registration['sample_id']} 图片名与嵌入图数量不一致")
        for filename, drawing in zip(names, drawings, strict=True):
            digest = sha256(drawing.content).hexdigest()
            if digest in seen_batch:
                continue
            seen_batch.add(digest)
            if digest in existing_hashes:
                skipped += 1
                evidence.append({"filename": filename, "sha256": digest, "status": "already_exists"})
                continue
            upload = _validated_upload(filename, drawing.content)
            saved = save_product_drawing_files(product_id=product.id, upload=upload)
            saved_paths.append((saved.image_path, saved.thumbnail_path))
            row = ProductDrawing(
                product_id=product.id,
                image_path=saved.image_path,
                thumbnail_path=saved.thumbnail_path,
                uploaded_by=user.id,
            )
            db.add(row)
            db.flush()
            audit_master_change(
                db,
                user=user,
                action="UPLOAD_DRAWING",
                resource="Product",
                resource_id=product.id,
                details={
                    "drawing_id": row.id,
                    "original_filename": filename,
                    "source": BATCH_ID,
                    "sha256": digest,
                    "sample_id": registration["sample_id"],
                },
            )
            existing_hashes.add(digest)
            created += 1
            evidence.append({"filename": filename, "sha256": digest, "status": "created"})
    return created, skipped, evidence


def _build_plan(
    db: Session,
    references: list[dict[str, Any]],
    registrations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    customers = {
        str(row.customer_code).strip().upper(): row
        for row in db.scalars(
            select(Customer).where(func.upper(func.trim(Customer.customer_code)).in_(("YL", "YKE", "KEW")))
        ).all()
    }
    if set(customers) != {"YL", "YKE", "KEW"}:
        raise RuntimeError(f"三客户主档不完整：{sorted(customers)}")
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    for sample_id, registration in sorted(registrations.items()):
        if sample_id in EXCLUDED_SAMPLES:
            excluded.append(
                {
                    "sample_id": sample_id,
                    "product_code": registration["product_code"],
                    "reason": EXCLUDED_SAMPLES[sample_id],
                }
            )
            continue
        if registration["product_code"] == "80012500":
            continue
        selected = _select_references(references, sample_id, registration)
        for reference in selected:
            key = (reference["customer_code"], normalize_sample_code(reference["product_code"]))
            group = grouped.setdefault(
                key,
                {"customer": customers[key[0]], "reference": reference, "registrations": []},
            )
            group["registrations"].append(registration)
            # Use YP148's explicitly selected row and otherwise keep the first
            # reference identity for duplicate photos of the same product.
            if sample_id == "YP148":
                group["reference"] = reference

    plan_rows: list[dict[str, Any]] = []
    for (customer_code, product_code), group in sorted(grouped.items()):
        reference = group["reference"]
        payload = _target_payload(
            db=db,
            customer=group["customer"],
            reference=reference,
            registrations=group["registrations"],
            product_code=product_code,
        )
        existing = _find_product(db, group["customer"].id, product_code)
        plan_rows.append(
            {
                "customer_code": customer_code,
                "product_code": product_code,
                "sample_ids": [row["sample_id"] for row in group["registrations"]],
                "reference_key": reference["key"],
                "material_id": payload.material_id,
                "production_process": payload.production_process,
                "print_content": payload.print_content,
                "printing_colors": payload.printing_colors,
                "drawings": sum(len(row.get("_embedded_drawings") or ()) for row in group["registrations"]),
                "action": "create" if existing is None else "update_or_drawing",
                "_payload": payload,
                "_group": group,
            }
        )

    # Frozen composite BOM: one delivery/order parent, two internal components.
    bom_registration_rows = [registrations["YP026"], registrations["YP030"]]
    bom_reference_rows = [
        row
        for row in references
        if row["customer_code"] == "YKE"
        and row["product_code"] == "80012500"
        and row["source_row"] in {143, 144}
    ]
    if {(row["source_row"]) for row in bom_reference_rows} != {143, 144}:
        raise RuntimeError("80012500 两条基础资料行已变化")
    bom_reference_rows.sort(key=lambda row: row["source_row"])
    parent_payload = _target_payload(
        db=db,
        customer=customers["YKE"],
        reference=bom_reference_rows[0],
        registrations=bom_registration_rows,
        product_code="80012500",
    )
    parent_payload.product_name = "EA1-S6ML 组合套装"
    parent_payload.combination_mode = "parent_priced_set"
    plan_rows.append(
        {
            "customer_code": "YKE",
            "product_code": "80012500",
            "sample_ids": ["YP026", "YP030"],
            "reference_key": "YKE:rows143+144:BOM",
            "material_id": parent_payload.material_id,
            "production_process": parent_payload.production_process,
            "print_content": parent_payload.print_content,
            "printing_colors": parent_payload.printing_colors,
            "drawings": sum(len(row.get("_embedded_drawings") or ()) for row in bom_registration_rows),
            "action": "create_bom" if _find_product(db, customers["YKE"].id, "80012500") is None else "update_bom",
            "_payload": parent_payload,
            "_group": {
                "customer": customers["YKE"],
                "reference": bom_reference_rows[0],
                "registrations": bom_registration_rows,
            },
            "_bom_references": bom_reference_rows,
        }
    )
    if len(excluded) != 22:
        raise RuntimeError(f"排除清单数量必须为22，实际 {len(excluded)}")
    return {"rows": plan_rows, "excluded": excluded, "customers": customers}


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in plan["rows"]
    ]
    return {
        "summary": {
            "target_products": len(rows),
            "create": sum(row["action"].startswith("create") for row in rows),
            "existing": sum(not row["action"].startswith("create") for row in rows),
            "excluded_samples": len(plan["excluded"]),
            "printed_products": sum(row["print_content"] == "单色印刷" for row in rows),
            "red_print_products": sum(row["printing_colors"] == "红色" for row in rows),
            "black_print_products": sum(row["printing_colors"] == "黑色" for row in rows),
            "source_drawings_assigned": sum(row["drawings"] for row in rows),
            "by_customer": dict(Counter(row["customer_code"] for row in rows)),
        },
        "products": rows,
        "excluded": plan["excluded"],
    }


def _ensure_internal_component(
    db: Session,
    *,
    customer: Customer,
    reference: dict[str, Any],
    registration: dict[str, Any],
    product_code: str,
    product_name: str,
    user: User,
) -> tuple[Product, str]:
    payload = _target_payload(
        db=db,
        customer=customer,
        reference=reference,
        registrations=[registration],
        product_code=product_code,
        product_name=product_name,
        component=True,
    )
    product, status, _changed = _upsert_product(
        db, customer=customer, payload=payload, user=user
    )
    # replace_product_bom owns the composite/internal flags and their audit.
    return product, status


def _bom_matches(db: Session, parent: Product, components: list[tuple[Product, Decimal]]) -> bool:
    rows = db.scalars(
        select(ProductBomComponent)
        .where(ProductBomComponent.parent_product_id == parent.id)
        .order_by(ProductBomComponent.display_order)
    ).all()
    if len(rows) != len(components):
        return False
    return all(
        row.component_product_id == component.id
        and Decimal(str(row.quantity_per_set)) == quantity
        for row, (component, quantity) in zip(rows, components, strict=True)
    )


def _apply(
    db: Session,
    *,
    plan: dict[str, Any],
    user: User,
    fail_after_products: int | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    result_rows: list[dict[str, Any]] = []
    saved_paths: list[tuple[str, str]] = []
    try:
        for row in plan["rows"]:
            group = row["_group"]
            product, status, changed_fields = _upsert_product(
                db,
                customer=group["customer"],
                payload=row["_payload"],
                user=user,
            )
            drawing_created, drawing_skipped, evidence = _add_drawings(
                db,
                product=product,
                registrations=group["registrations"],
                user=user,
                saved_paths=saved_paths,
            )
            bom_status = None
            if row["product_code"] == "80012500":
                body_ref, reinforcement_ref = row["_bom_references"]
                body, body_status = _ensure_internal_component(
                    db,
                    customer=group["customer"],
                    reference=body_ref,
                    registration=group["registrations"][1],
                    product_code="80012500-S01",
                    product_name="EA1-S6ML 本体",
                    user=user,
                )
                reinforcement, reinforcement_status = _ensure_internal_component(
                    db,
                    customer=group["customer"],
                    reference=reinforcement_ref,
                    registration=group["registrations"][0],
                    product_code="80012500-S02",
                    product_name="EA1-S6ML 补强板",
                    user=user,
                )
                components = [(body, Decimal("1")), (reinforcement, Decimal("2"))]
                if _bom_matches(db, product, components):
                    bom_status = "unchanged"
                else:
                    replace_product_bom(
                        db,
                        parent_product_id=product.id,
                        components=[
                            {
                                "component_product_id": body.id,
                                "quantity_per_set": Decimal("1"),
                                "is_die_cut": False,
                                "display_mode": "internal_only",
                                "remark": "本体1只",
                            },
                            {
                                "component_product_id": reinforcement.id,
                                "quantity_per_set": Decimal("2"),
                                "is_die_cut": False,
                                "display_mode": "internal_only",
                                "remark": "补强板2片",
                            },
                        ],
                        expected_version=product.version,
                        user=user,
                        change_reason="老板确认80012500本体1+补强板2组合BOM",
                    )
                    bom_status = f"updated;components={body_status},{reinforcement_status}"
            result_rows.append(
                {
                    "customer_code": row["customer_code"],
                    "product_code": row["product_code"],
                    "product_id": product.id,
                    "status": status,
                    "changed_fields": changed_fields,
                    "drawings_created": drawing_created,
                    "drawings_skipped": drawing_skipped,
                    "drawing_evidence": evidence,
                    "bom_status": bom_status,
                }
            )
            if fail_after_products and len(result_rows) >= fail_after_products:
                raise RuntimeError("隔离演练注入故障：验证整批事务与图片文件回滚")

        by_customer: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in result_rows:
            customer = plan["customers"][row["customer_code"]]
            by_customer[customer.id].append(row)
        for customer_id, rows in by_customer.items():
            audit_master_change(
                db,
                user=user,
                action="BATCH_IMPORT",
                resource="MixedSampleCatalogSync",
                resource_id=customer_id,
                details={
                    "batch_id": BATCH_ID,
                    "customer_id": customer_id,
                    "products": len(rows),
                    "created": sum(row["status"] == "created" for row in rows),
                    "updated": sum(row["status"] == "updated" for row in rows),
                    "drawings_created": sum(row["drawings_created"] for row in rows),
                    "drawings_skipped": sum(row["drawings_skipped"] for row in rows),
                    "excluded_samples": plan["excluded"],
                },
            )
    except Exception:
        db.rollback()
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise
    return {
        "summary": {
            "products": len(result_rows),
            "created": sum(row["status"] == "created" for row in result_rows),
            "updated": sum(row["status"] == "updated" for row in result_rows),
            "unchanged": sum(row["status"] == "unchanged" for row in result_rows),
            "drawings_created": sum(row["drawings_created"] for row in result_rows),
            "drawings_skipped": sum(row["drawings_skipped"] for row in result_rows),
        },
        "products": result_rows,
    }, saved_paths


def _verify_business_readback(db: Session, plan: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    rows: list[dict[str, Any]] = []
    for plan_row in plan["rows"]:
        customer = plan_row["_group"]["customer"]
        product = _find_product(db, customer.id, plan_row["product_code"])
        if product is None:
            problems.append(f"缺少产品 {plan_row['customer_code']}/{plan_row['product_code']}")
            continue
        drawing_count = int(
            db.scalar(
                select(func.count(ProductDrawing.id)).where(ProductDrawing.product_id == product.id)
            )
            or 0
        )
        if drawing_count < 1:
            problems.append(f"缺少图纸 {plan_row['customer_code']}/{plan_row['product_code']}")
        if product.print_content != plan_row["print_content"]:
            problems.append(f"印刷不同步 {plan_row['customer_code']}/{plan_row['product_code']}")
        if product.production_process != plan_row["production_process"]:
            problems.append(f"工艺不同步 {plan_row['customer_code']}/{plan_row['product_code']}")
        rows.append(
            {
                "customer_code": plan_row["customer_code"],
                "product_code": plan_row["product_code"],
                "product_id": product.id,
                "drawing_count": drawing_count,
                "production_process": product.production_process,
                "print_content": product.print_content,
                "printing_colors": product.printing_colors,
                "material_id": product.material_id,
            }
        )
    parent = _find_product(db, plan["customers"]["YKE"].id, "80012500")
    bom_rows = []
    if parent is None:
        problems.append("缺少80012500组合父项")
    else:
        bom_rows = [
            {
                "component_product_id": row.component_product_id,
                "quantity_per_set": str(row.quantity_per_set),
                "internal_code": getattr(row, "internal_component_code", None),
                "display_mode": getattr(row, "display_mode", None),
            }
            for row in db.scalars(
                select(ProductBomComponent)
                .where(ProductBomComponent.parent_product_id == parent.id)
                .order_by(ProductBomComponent.display_order)
            ).all()
        ]
        if [Decimal(row["quantity_per_set"]) for row in bom_rows] != [
            Decimal("1"),
            Decimal("2"),
        ]:
            problems.append(f"80012500 BOM数量异常：{bom_rows}")
    return {"ok": not problems, "problems": problems, "products": rows, "bom": bom_rows}


def _assert_target(path: Path, allow_formal: bool) -> bool:
    resolved = path.resolve()
    is_formal = resolved == FORMAL_DATABASE
    if is_formal and not allow_formal:
        raise RuntimeError("正式数据库必须显式提供 --allow-formal-database")
    if not is_formal and resolved.name.lower() == "carton_erp.sqlite3":
        raise RuntimeError("非标准路径不得伪装成正式数据库文件名")
    if not resolved.is_file():
        raise RuntimeError(f"数据库不存在：{resolved}")
    return is_formal


def main() -> int:
    parser = argparse.ArgumentParser(description="2026-08-10 混合样品常用箱受控同步")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--volume", type=Path, action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation")
    parser.add_argument("--expected-database-sha256")
    parser.add_argument("--allow-formal-database", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--file-storage-dir", type=Path)
    parser.add_argument("--test-fail-after-products", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()

    database = args.database.resolve()
    is_formal = _assert_target(database, args.allow_formal_database)
    before = _database_checks(database)
    if before.integrity_check != "ok" or before.foreign_key_violations:
        raise RuntimeError(f"数据库预检失败：{asdict(before)}")
    if before.alembic_revision != EXPECTED_SCHEMA_REVISION:
        raise RuntimeError(f"数据库版本不符：{before.alembic_revision}")
    if args.expected_database_sha256 and before.sha256 != args.expected_database_sha256.upper():
        raise RuntimeError("数据库 SHA-256 与门禁值不符")
    if args.file_storage_dir:
        os.environ["ERP_FILE_STORAGE_DIR"] = str(args.file_storage_dir.resolve())

    references, registrations, input_evidence = _load_inputs(
        args.reference.resolve(), tuple(path.resolve() for path in args.volume)
    )
    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with SessionLocal() as db:
        user = db.scalar(
            select(User).where(User.id == 1, User.role == "admin", User.is_active.is_(True))
        )
        if user is None:
            raise RuntimeError("冻结执行账号 admin/id=1 不存在或未启用")
        plan = _build_plan(db, references, registrations)
        public_plan = _public_plan(plan)

    report: dict[str, Any] = {
        "batch_id": BATCH_ID,
        "mode": "apply" if args.apply else "dry_run",
        "database": str(database),
        "formal_database": is_formal,
        "input": input_evidence,
        "database_before": asdict(before),
        "plan": public_plan,
        "backup": None,
        "apply": None,
        "readback": None,
        "database_after": None,
    }
    if not args.apply:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report["plan"]["summary"], ensure_ascii=False, indent=2))
        return 0

    expected_confirmation = FORMAL_CONFIRMATION if is_formal else REHEARSAL_CONFIRMATION
    if args.confirmation != expected_confirmation:
        raise RuntimeError(f"确认字符串错误；本次必须为 {expected_confirmation}")
    if not args.expected_database_sha256:
        raise RuntimeError("执行写入必须提供 --expected-database-sha256")
    if not args.file_storage_dir:
        raise RuntimeError("执行写入必须显式提供 --file-storage-dir")
    if is_formal:
        if args.test_fail_after_products:
            raise RuntimeError("正式数据库禁止使用故障注入参数")
        if args.backup_dir is None:
            raise RuntimeError("正式写入必须提供 --backup-dir")
        # Path.resolve() may raise WinError 1005 on a healthy mapped NAS drive.
        backup_root = Path(os.path.abspath(args.backup_dir))
        backup_path = backup_root / f"carton_erp_before_{BATCH_ID}.sqlite3"
        _online_backup(database, backup_path)
        backup_checks = _database_checks(backup_path)
        if (
            backup_checks.integrity_check != "ok"
            or backup_checks.foreign_key_violations
            or backup_checks.alembic_revision != before.alembic_revision
            or backup_checks.counts != before.counts
        ):
            raise RuntimeError(f"正式备份校验失败：{asdict(backup_checks)}")
        report["backup"] = {"path": str(backup_path), **asdict(backup_checks)}
        if _sha256_file(database) != before.sha256:
            raise RuntimeError("备份后正式数据库发生变化，拒绝写入")

    saved_paths: list[tuple[str, str]] = []
    try:
        with SessionLocal() as db:
            user = db.get(User, 1)
            if user is None or not user.is_active or user.role != "admin":
                raise RuntimeError("执行账号已变化")
            # Rebuild under the write session so versions and existing rows are current.
            live_plan = _build_plan(db, references, registrations)
            apply_result, saved_paths = _apply(
                db,
                plan=live_plan,
                user=user,
                fail_after_products=args.test_fail_after_products,
            )
            readback = _verify_business_readback(db, live_plan)
            if not readback["ok"]:
                raise RuntimeError(f"事务后业务回读失败：{readback['problems']}")
            db.commit()
            report["apply"] = apply_result
            report["readback"] = readback
    except Exception:
        for image_path, thumbnail_path in saved_paths:
            remove_drawing_files(image_path, thumbnail_path)
        raise
    finally:
        engine.dispose()

    after = _database_checks(database)
    report["database_after"] = asdict(after)
    if after.integrity_check != "ok" or after.foreign_key_violations:
        raise RuntimeError(f"写入后数据库完整性失败：{asdict(after)}")
    protected_changes = {
        table: (before.counts.get(table), after.counts.get(table))
        for table in PROTECTED_TABLES
        if before.counts.get(table) != after.counts.get(table)
    }
    if protected_changes:
        raise RuntimeError(f"保护表数量发生变化：{protected_changes}")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["apply"]["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

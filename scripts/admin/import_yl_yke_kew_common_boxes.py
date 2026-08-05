from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.master_data_common import audit_master_change  # noqa: E402
from app.core.database import create_sqlite_engine  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.material import Material  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.box_type_rules import normalize_box_configuration  # noqa: E402
from app.services.master_data_versioning import record_versioned_create  # noqa: E402


REHEARSAL_CONFIRMATION = "APPLY_YL_YKE_KEW_REHEARSAL"
FORMAL_CONFIRMATION = "APPLY_YL_YKE_KEW_FORMAL_20260805"
EXPECTED_PACKAGE_ID = "YL_YKE_KEW_20260805_REHEARSAL_V2_ONE_TO_SIX"
EXPECTED_SCHEMA_REVISION = "dk93v8x9z82"
FORMAL_DATABASE = (ROOT / "data" / "carton_erp.sqlite3").resolve()
REHEARSAL_SOURCE = "controlled_import.yl_yke_kew_20260805_one_to_six_rehearsal"
FORMAL_SOURCE = "controlled_import.yl_yke_kew_20260805_one_to_six_formal"
REHEARSAL_REASON = "老板批准：按原表字段口径和一开六规则重做 YKE/KEW 首批常用箱隔离演练"
FORMAL_REASON = "老板于2026-08-05明确批准：正式迁移并统一导入YKE/KEW首批131条常用箱"
VALID_CUTTING_MODES = {"一开一", "一开二", "一开三", "一开四", "一开五", "一开六"}
EXPECTED_COUNTS = {"YKE": 100, "KEW": 31}
EXPECTED_CUSTOMER_NUMBERS = {"YKE": 135, "KEW": 136}
EXPECTED_BOX_COUNTS = {"normal": 20, "die_cut": 111}
WATCH_TABLES = (
    "customers",
    "products",
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
PROTECTED_UNCHANGED_TABLES = (
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_checks(path: Path) -> DatabaseChecks:
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
        revision = None
        if "alembic_version" in tables:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = str(row[0]) if row else None
    return DatabaseChecks(
        sha256=sha256_file(path),
        integrity_check=integrity,
        foreign_key_violations=len(foreign_keys),
        alembic_revision=revision,
        counts=counts,
    )


def load_manifest(path: Path, expected_sha256: str | None) -> tuple[dict[str, Any], str]:
    actual_sha256 = sha256_file(path)
    if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
        raise RuntimeError("清单 SHA-256 已变化，拒绝执行")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("package_id") != EXPECTED_PACKAGE_ID:
        raise RuntimeError("清单 package_id 不符合本批次")
    if payload.get("safety", {}).get("formal_database_write_allowed") is not False:
        raise RuntimeError("清单未明确禁止正式库写入")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 131:
        raise RuntimeError("本批清单必须恰好包含 131 条")
    return payload, actual_sha256


def _assert_database_target(path: Path, *, allow_formal_database: bool) -> bool:
    resolved = path.resolve()
    is_formal = resolved == FORMAL_DATABASE
    if is_formal:
        if not allow_formal_database:
            raise RuntimeError("正式数据库必须显式提供 --allow-formal-database")
        return True
    if resolved.name.lower() == "carton_erp.sqlite3":
        raise RuntimeError("非标准路径不得伪装为正式数据库")
    if ROOT.resolve() in resolved.parents and resolved.parent.name.lower() == "data":
        raise RuntimeError("项目 data 目录只允许唯一正式数据库路径")
    if not resolved.is_file():
        raise RuntimeError(f"数据库副本不存在：{resolved}")
    return False


def _normalize_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _target_product_values(
    row: dict[str, Any],
    *,
    customer_id: int,
    source: str = REHEARSAL_SOURCE,
) -> dict[str, Any]:
    configuration = normalize_box_configuration(
        box_style=row.get("box_style"),
        splice_mode="single",
        pieces_per_box=None,
        flap_mm=None,
        default_cutting_mode=row.get("default_cutting_mode"),
        crease_type=row.get("crease_type"),
    )
    return {
        "customer_id": customer_id,
        "product_code": str(row["product_code"]).strip(),
        "customer_material_code": str(row["customer_material_code"]).strip(),
        "product_name": str(row["product_name"]).strip(),
        "material_id": int(row["material_id"]),
        "mold_tool_id": None,
        "legacy_material_text": str(row.get("legacy_material_text") or "").strip() or None,
        "length_mm": _normalize_decimal(row.get("length_mm")),
        "width_mm": _normalize_decimal(row.get("width_mm")),
        "height_mm": _normalize_decimal(row.get("height_mm")),
        "box_category": str(row["box_category"]),
        "box_style": configuration["box_style"],
        "print_content": None,
        "printing_colors": None,
        "production_process": str(row.get("production_process") or "").strip() or None,
        "unit": "只",
        "sale_unit_price": _normalize_decimal(row.get("sale_unit_price")),
        "sale_unit_price_no_tax": _normalize_decimal(
            row.get("sale_unit_price_no_tax")
        ),
        "cost_unit_price": None,
        "board_price": None,
        "suggested_price": None,
        "default_cardboard_length": None,
        "default_cardboard_width": None,
        "default_score_lines": None,
        "default_material_code": None,
        "die_cut_path": None,
        "remark": (
            f"{source}；源表={row['source_sheet']}；源Excel行={row['source_excel_row']}；"
            f"图号={row.get('drawing_number') or ''}；源标记={row.get('legacy_source_marker') or ''}；"
            f"生产数量={row.get('production_quantity') or ''}；"
            f"预留损耗数={row.get('reserved_loss_quantity') or ''}；"
            f"存放位置={row.get('storage_location') or ''}"
        ),
        "legacy_customer_material_code": None,
        "flute_type": str(row.get("flute_type") or "").strip() or None,
        "layer_count": int(row["layer_count"]) if row.get("layer_count") is not None else None,
        "surface_paper_type": None,
        "legacy_flute_text": str(row.get("legacy_material_text") or "").strip() or None,
        "report_length_mm": row.get("report_length_mm"),
        "report_width_mm": row.get("report_width_mm"),
        "crease_type": configuration["crease_type"],
        "crease_left_mm": row.get("crease_left_mm"),
        "crease_middle_mm": row.get("crease_middle_mm"),
        "crease_right_mm": row.get("crease_right_mm"),
        "report_notes": str(row.get("report_notes") or "").strip() or None,
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
        "is_active": bool(row["is_active"]),
        "manual_modified": False,
    }


def _comparable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    return value


def _product_mismatches(product: Product, target: dict[str, Any]) -> dict[str, Any]:
    mismatches: dict[str, Any] = {}
    for field, expected in target.items():
        current = getattr(product, field)
        if _comparable(current) != _comparable(expected):
            mismatches[field] = {
                "current": _comparable(current),
                "expected": _comparable(expected),
            }
    return mismatches


def _validate_manifest_shape(rows: list[dict[str, Any]]) -> None:
    keys: set[tuple[str, str]] = set()
    customer_counts: dict[str, int] = {}
    box_counts: dict[str, int] = {}
    inactive_count = 0
    for row in rows:
        customer_code = str(row.get("customer_code") or "").strip()
        product_code = str(row.get("product_code") or "").strip()
        if customer_code not in EXPECTED_COUNTS:
            raise RuntimeError(f"清单含计划外客户：{customer_code}")
        key = (customer_code, product_code)
        if not product_code or key in keys:
            raise RuntimeError(f"清单客户内产品编码重复或为空：{key}")
        keys.add(key)
        customer_counts[customer_code] = customer_counts.get(customer_code, 0) + 1
        box_category = str(row.get("box_category") or "")
        if box_category not in EXPECTED_BOX_COUNTS:
            raise RuntimeError(f"非法箱类：{box_category}")
        box_counts[box_category] = box_counts.get(box_category, 0) + 1
        if product_code == "80012500" and customer_code == "YKE":
            raise RuntimeError("重复编码 YKE/80012500 不得进入清单")
        if row.get("image_flag") != "是":
            raise RuntimeError(f"记录未满足有产品图门禁：{key}")
        if row.get("default_cutting_mode") not in VALID_CUTTING_MODES:
            raise RuntimeError(f"ERP开料方式非法：{key}")
        if row.get("requires_manual_confirmation") and row.get("is_active"):
            raise RuntimeError(f"待人工确认记录必须保持停用：{key}")
        if not row.get("is_active"):
            inactive_count += 1
        if not row.get("material_id") or not row.get("material_code") or not row.get("material_supplier"):
            raise RuntimeError(f"材质映射不完整：{key}")
        if _normalize_decimal(row.get("sale_unit_price_no_tax")) is None:
            raise RuntimeError(f"现行交易价格未税为空：{key}")
        if _normalize_decimal(row.get("paper_calculation_coefficient")) is None:
            raise RuntimeError(f"计算用纸系数为空：{key}")
        if _normalize_decimal(row.get("paper_sheet_quantity")) is None:
            raise RuntimeError(f"用纸张数为空：{key}")
        if box_category == "normal":
            if any(_normalize_decimal(row.get(field)) is None for field in ("length_mm", "width_mm", "height_mm")):
                raise RuntimeError(f"普通箱三维尺寸不完整：{key}")
        elif not row.get("report_length_mm") or not row.get("report_width_mm"):
            raise RuntimeError(f"异型/模切箱报料尺寸不完整：{key}")
    if customer_counts != EXPECTED_COUNTS:
        raise RuntimeError(f"客户条数不符合冻结范围：{customer_counts}")
    if box_counts != EXPECTED_BOX_COUNTS:
        raise RuntimeError(f"箱类条数不符合冻结范围：{box_counts}")
    if inactive_count != 14:
        raise RuntimeError(f"J列未标注一开几的停用条数应为14，实际为{inactive_count}")


def _customer_master(payload: dict[str, Any]) -> list[dict[str, Any]]:
    customers = payload.get("scope", {}).get("customers")
    if not isinstance(customers, list) or len(customers) != 2:
        raise RuntimeError("清单客户主档必须恰好是 YKE、KEW")
    normalized: list[dict[str, Any]] = []
    for row in customers:
        customer_code = str(row["code"]).strip()
        expected_number = EXPECTED_CUSTOMER_NUMBERS.get(customer_code)
        manifest_number = row.get("customer_number")
        if expected_number is None:
            raise RuntimeError(f"清单含计划外客户代码：{customer_code}")
        if manifest_number not in (None, expected_number):
            raise RuntimeError(
                f"客户编号与冻结值不一致：{customer_code}/{manifest_number}"
            )
        normalized.append(
            {
                "customer_code": customer_code,
                "name": str(row["name"]).strip(),
                "customer_number": expected_number,
            }
        )
    if {(row["customer_code"], row["name"]) for row in normalized} != {
        ("YKE", "研光"),
        ("KEW", "光洋"),
    }:
        raise RuntimeError("客户主档与老板确认的简称/代码不一致")
    return normalized


def build_plan(
    db: Session,
    payload: dict[str, Any],
    *,
    source: str = REHEARSAL_SOURCE,
) -> dict[str, Any]:
    rows = payload["rows"]
    _validate_manifest_shape(rows)
    customer_plan: list[dict[str, Any]] = []
    customer_ids: dict[str, int | None] = {}
    for target in _customer_master(payload):
        code = target["customer_code"]
        name = target["name"]
        by_code = db.scalar(select(Customer).where(func.upper(Customer.customer_code) == code))
        by_name = db.scalar(select(Customer).where(Customer.name == name))
        if by_code is not None and by_name is not None and by_code.id != by_name.id:
            raise RuntimeError(f"客户代码和名称分别命中不同主档：{code}/{name}")
        existing = by_code or by_name
        if existing is None:
            customer_plan.append({**target, "status": "create", "id": None})
            customer_ids[code] = None
            continue
        if (
            existing.customer_number != target["customer_number"]
            or existing.customer_code != code
            or existing.name != name
            or not existing.is_active
        ):
            raise RuntimeError(f"现有客户主档与清单不一致：{code}/{name}")
        customer_plan.append({**target, "status": "already_applied", "id": int(existing.id)})
        customer_ids[code] = int(existing.id)

    material_cache: dict[int, Material] = {}
    for row in rows:
        material_id = int(row["material_id"])
        if material_id not in material_cache:
            material = db.get(Material, material_id)
            if material is None:
                raise RuntimeError(f"ERP材质ID不存在：{material_id}")
            material_cache[material_id] = material
        material = material_cache[material_id]
        if (
            not material.is_active
            or material.code != row["material_code"]
            or material.supplier_name != row["material_supplier"]
            or material.layer_count != row["layer_count"]
        ):
            raise RuntimeError(
                f"材质ID/代码/供应商/层数不一致：{material_id}/{row['material_code']}"
            )

    product_plan: list[dict[str, Any]] = []
    for row in rows:
        code = row["customer_code"]
        customer_id = customer_ids[code]
        if customer_id is None:
            product_plan.append(
                {
                    "customer_code": code,
                    "product_code": row["product_code"],
                    "status": "create",
                    "id": None,
                }
            )
            continue
        by_product = db.scalar(
            select(Product).where(
                Product.customer_id == customer_id,
                Product.product_code == row["product_code"],
            )
        )
        by_material_code = db.scalar(
            select(Product).where(
                Product.customer_id == customer_id,
                Product.customer_material_code == row["customer_material_code"],
            )
        )
        if by_product is not None and by_material_code is not None and by_product.id != by_material_code.id:
            raise RuntimeError(f"产品编码与客户料号分别命中不同记录：{code}/{row['product_code']}")
        existing = by_product or by_material_code
        if existing is None:
            product_plan.append(
                {
                    "customer_code": code,
                    "product_code": row["product_code"],
                    "status": "create",
                    "id": None,
                }
            )
            continue
        mismatches = _product_mismatches(
            existing,
            _target_product_values(row, customer_id=customer_id, source=source),
        )
        if mismatches:
            raise RuntimeError(
                f"现有常用箱与清单不一致，拒绝覆盖：{code}/{row['product_code']} {mismatches}"
            )
        product_plan.append(
            {
                "customer_code": code,
                "product_code": row["product_code"],
                "status": "already_applied",
                "id": int(existing.id),
            }
        )

    customer_create_count = sum(row["status"] == "create" for row in customer_plan)
    product_create_count = sum(row["status"] == "create" for row in product_plan)
    status = "already_applied" if customer_create_count == 0 and product_create_count == 0 else "ready"
    return {
        "status": status,
        "customer_total": len(customer_plan),
        "customer_create_count": customer_create_count,
        "product_total": len(product_plan),
        "product_create_count": product_create_count,
        "active_product_count": sum(bool(row["is_active"]) for row in rows),
        "inactive_product_count": sum(not bool(row["is_active"]) for row in rows),
        "box_category_counts": {
            category: sum(row["box_category"] == category for row in rows)
            for category in EXPECTED_BOX_COUNTS
        },
        "customer_counts": {
            code: sum(row["customer_code"] == code for row in rows)
            for code in EXPECTED_COUNTS
        },
        "customer_items": customer_plan,
        "product_items": product_plan,
    }


def inspect_plan(
    database: Path,
    payload: dict[str, Any],
    *,
    source: str = REHEARSAL_SOURCE,
) -> dict[str, Any]:
    uri = database.resolve().as_uri() + "?mode=ro"
    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            db.connection().exec_driver_sql("PRAGMA query_only = ON")
            return build_plan(db, payload, source=source)
    finally:
        engine.dispose()


def _write_report(path: Path | None, result: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def apply_import(
    *,
    database: Path,
    payload: dict[str, Any],
    manifest_sha256: str,
    actor_username: str,
    expected_database_sha256: str,
    allow_formal_database: bool,
) -> dict[str, Any]:
    is_formal = _assert_database_target(
        database,
        allow_formal_database=allow_formal_database,
    )
    source = FORMAL_SOURCE if is_formal else REHEARSAL_SOURCE
    reason = FORMAL_REASON if is_formal else REHEARSAL_REASON
    before = database_checks(database)
    if before.sha256.lower() != expected_database_sha256.lower():
        raise RuntimeError("目标数据库 SHA-256 已变化，拒绝执行")
    if before.integrity_check.lower() != "ok" or before.foreign_key_violations:
        raise RuntimeError(f"目标数据库写入前检查失败：{asdict(before)}")
    if before.alembic_revision != EXPECTED_SCHEMA_REVISION:
        raise RuntimeError(
            f"数据库版本不匹配：{before.alembic_revision} != {EXPECTED_SCHEMA_REVISION}"
        )
    preflight = inspect_plan(database, payload, source=source)
    if preflight["status"] == "already_applied":
        return {
            "changed": False,
            "status": "already_applied",
            "database": str(database),
            "manifest_sha256": manifest_sha256,
            "checks": asdict(before),
            "plan": preflight,
        }

    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    created_customer_ids: dict[str, int] = {}
    created_product_ids: list[int] = []
    try:
        with factory() as db:
            actor = db.scalar(select(User).where(User.username == actor_username))
            if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
                raise RuntimeError("执行人必须是启用的 admin 或 boss 账号")
            live_plan = build_plan(db, payload, source=source)
            if live_plan["status"] != "ready":
                raise RuntimeError("事务开始前计划已变化，拒绝执行")

            customers_by_code: dict[str, Customer] = {}
            for item in _customer_master(payload):
                customer = db.scalar(
                    select(Customer).where(
                        func.upper(Customer.customer_code) == item["customer_code"]
                    )
                )
                if customer is None:
                    customer = Customer(
                        customer_number=item["customer_number"],
                        customer_code=item["customer_code"],
                        name=item["name"],
                        payment_term_days=0,
                        statement_cycle_start_day=20,
                        credit_limit=Decimal("0"),
                        delivery_method="配送",
                        default_tax_rate=Decimal("0.13"),
                        remark=(
                            f"{source}；老板批准正式统一导入"
                            if is_formal
                            else f"{source}；本轮仅隔离副本演练"
                        ),
                        status="active",
                        is_active=True,
                    )
                    db.add(customer)
                    db.flush()
                    record_versioned_create(
                        db,
                        object_type="customer",
                        entity=customer,
                        user=actor,
                        reason=reason,
                        source=source,
                    )
                    audit_master_change(
                        db,
                        user=actor,
                        action="CREATE",
                        resource="Customer",
                        resource_id=customer.id,
                        details={"source": source, "customer_code": customer.customer_code, "name": customer.name},
                    )
                    created_customer_ids[item["customer_code"]] = int(customer.id)
                customers_by_code[item["customer_code"]] = customer

            for row in payload["rows"]:
                customer = customers_by_code[row["customer_code"]]
                existing = db.scalar(
                    select(Product).where(
                        Product.customer_id == customer.id,
                        Product.product_code == row["product_code"],
                    )
                )
                if existing is not None:
                    continue
                values = _target_product_values(
                    row,
                    customer_id=int(customer.id),
                    source=source,
                )
                product = Product(**values)
                db.add(product)
                db.flush()
                record_versioned_create(
                    db,
                    object_type="product",
                    entity=product,
                    user=actor,
                    reason=reason,
                    source=source,
                )
                audit_master_change(
                    db,
                    user=actor,
                    action="CREATE",
                    resource="Product",
                    resource_id=product.id,
                    details={
                        "source": source,
                        "customer_code": row["customer_code"],
                        "product_code": row["product_code"],
                        "source_sheet": row["source_sheet"],
                        "source_excel_row": row["source_excel_row"],
                        "is_active": row["is_active"],
                    },
                )
                created_product_ids.append(int(product.id))
            db.commit()
    except Exception:
        raise
    finally:
        engine.dispose()

    after = database_checks(database)
    result_plan = inspect_plan(database, payload, source=source)
    if after.integrity_check.lower() != "ok" or after.foreign_key_violations:
        raise RuntimeError(f"演练写入后数据库检查失败：{asdict(after)}")
    for table in PROTECTED_UNCHANGED_TABLES:
        if table in before.counts and before.counts[table] != after.counts.get(table):
            raise RuntimeError(f"保护表意外变化：{table}")
    expected_master_delta = len(created_customer_ids) + len(created_product_ids)
    if after.counts["customers"] - before.counts["customers"] != len(created_customer_ids):
        raise RuntimeError("客户数量增量不符合预期")
    if after.counts["products"] - before.counts["products"] != len(created_product_ids):
        raise RuntimeError("常用箱数量增量不符合预期")
    if after.counts["master_data_object_versions"] - before.counts["master_data_object_versions"] != expected_master_delta:
        raise RuntimeError("主数据版本数量增量不符合预期")
    if after.counts["operation_logs"] - before.counts["operation_logs"] != expected_master_delta * 2:
        raise RuntimeError("审计日志数量增量不符合预期")
    if result_plan["status"] != "already_applied":
        raise RuntimeError("演练写入后回读未达到幂等状态")
    return {
        "changed": True,
        "status": "applied_to_formal_database" if is_formal else "applied_to_isolated_copy",
        "database": str(database),
        "manifest_sha256": manifest_sha256,
        "database_sha256_before": before.sha256,
        "database_sha256_after": after.sha256,
        "created_customer_ids": created_customer_ids,
        "created_product_ids": created_product_ids,
        "checks_before": asdict(before),
        "checks_after": asdict(after),
        "plan_after": result_plan,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YKE/KEW 131条常用箱受控导入")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-database-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--allow-formal-database", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.resolve()
    manifest = args.manifest.resolve()
    if not manifest.is_file():
        raise SystemExit(f"清单不存在：{manifest}")
    payload, manifest_sha256 = load_manifest(manifest, args.expected_manifest_sha256)
    if not args.apply:
        checks = database_checks(database)
        if checks.integrity_check.lower() != "ok" or checks.foreign_key_violations:
            raise SystemExit(f"数据库检查失败：{asdict(checks)}")
        source = (
            FORMAL_SOURCE
            if database == FORMAL_DATABASE and args.allow_formal_database
            else REHEARSAL_SOURCE
        )
        result = {
            "changed": False,
            "status": "dry_run",
            "database": str(database),
            "manifest_sha256": manifest_sha256,
            "checks": asdict(checks),
            "plan": inspect_plan(database, payload, source=source),
        }
        _write_report(args.report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    is_formal = database == FORMAL_DATABASE
    expected_confirmation = FORMAL_CONFIRMATION if is_formal else REHEARSAL_CONFIRMATION
    if args.confirm != expected_confirmation:
        raise SystemExit(f"--apply 必须同时提供 --confirm {expected_confirmation}")
    if args.allow_formal_database != is_formal:
        if is_formal:
            raise SystemExit("正式数据库必须同时提供 --allow-formal-database")
        raise SystemExit("--allow-formal-database 只能用于唯一正式数据库路径")
    if not args.expected_database_sha256:
        raise SystemExit("--apply 必须提供 --expected-database-sha256")
    result = apply_import(
        database=database,
        payload=payload,
        manifest_sha256=manifest_sha256,
        actor_username=args.actor,
        expected_database_sha256=args.expected_database_sha256,
        allow_formal_database=args.allow_formal_database,
    )
    _write_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

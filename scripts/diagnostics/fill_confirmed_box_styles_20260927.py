"""Scoped owner-authorized data task; caller owns backup and transaction gates.

Never run automatically on application startup. No historical order synchronization.
"""
from types import SimpleNamespace

from app.models.product import Product
from app.services.master_data_versioning import apply_versioned_update

TASK = "BOX-STYLE-CONFIRMED-20260927"
TARGETS = (
    (204, 5, "21301464", "异形箱"),
    (2342, 5, "23201130", "A1/0201 普通开槽箱"),
    (3318, 10, "SATL106010249", "A1/0201 普通开槽箱"),
)
ACTOR = SimpleNamespace(id=None, username="codex-owner-authorized-maintenance", role="system")


def preview(db):
    rows = []
    for product_id, customer_id, code, target in TARGETS:
        product = db.get(Product, product_id)
        if (not product or product.customer_id != customer_id or product.product_code != code
                or not product.is_active or product.deleted_at or product.purged_at
                or product.is_composite or product.is_virtual_composite_parent
                or product.supply_mode != "corrugated_production"):
            raise ValueError(f"产品身份或结构已变化：{product_id}")
        if product.box_style not in (None, "", target):
            raise ValueError(f"箱型不再为空或已确认值：{product_id}")
        rows.append(dict(id=product_id, customer_id=customer_id, product_code=code,
                         before=product.box_style, after=target, version=product.version))
    bom = db.get(Product, 3793)
    if not bom or bom.customer_id != 10 or bom.product_code != "SATJ2TP224005" or not bom.is_composite:
        raise ValueError("保留BOM产品身份发生变化")
    return rows


def apply(db, expected_plan, *, authorization):
    if authorization != TASK:
        raise ValueError("缺少本任务明确授权")
    if preview(db) != expected_plan:
        raise ValueError("预览后产品发生变化，请重新核对")
    changed = []
    for row in expected_plan:
        if row["before"] == row["after"]:
            continue
        revision = apply_versioned_update(
            db, object_type="product", entity=db.get(Product, row["id"]),
            updates={"box_style": row["after"]}, expected_version=row["version"],
            user=ACTOR, reason="老板2026-09-27明确确认三款空白箱型；保留BOM组合及全部历史订单",
            source=TASK, action="system_mapping",
        )
        changed.append(dict(**row, new_version=revision.version))
    db.flush()
    return changed

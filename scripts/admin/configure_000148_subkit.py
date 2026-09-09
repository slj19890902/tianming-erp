"""Scoped, audited 000148 activation. Preview by default; never alters stock."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.database import create_sqlite_engine
from app.models.bom_subkit import ProductSubkit, OrderSubkit
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.user import User
from app.services.bom_subkits import read_subkit, save_subkit, freeze_order_subkit


def preview(db):
    parent = db.get(Product, 3765)
    if parent is None or parent.customer_id != 136 or parent.product_code != "Z.001.000148":
        raise RuntimeError("000148正式产品身份不匹配")
    recipe = list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id == parent.id)))
    if {r.component_product_id: r.quantity_per_set for r in recipe} != {3788:2, 3789:6}:
        raise RuntimeError("000148的2长6短配方已变化，请核对")
    items = list(db.scalars(select(OrderItem).join(Order, Order.id == OrderItem.order_id).where(
        OrderItem.product_id == parent.id, OrderItem.is_force_closed.is_(False),
        OrderItem.delivered_quantity < OrderItem.quantity,
        ~Order.status.in_(("cancelled", "closed", "已作废", "已结单"))).order_by(OrderItem.id)))
    # Existing orders are in scope, but received/shipped facts need a separately
    # rehearsed balance conversion. Do not relabel an already posted stock lot.
    blocked = [r.id for r in items if db.get(OrderSubkit, r.id) is None and (
        r.requisition_status != "未报料" or r.material_status != "pending" or r.delivered_quantity)]
    from app.services.production_workflow import has_production_completion_facts
    if items and has_production_completion_facts(db, [r.id for r in items if db.get(OrderSubkit, r.id) is None]):
        blocked = sorted(set(blocked + [r.id for r in items if db.get(OrderSubkit, r.id) is None]))
    facts = {"parent_product_id":parent.id, "parent_version":parent.version,
        "customer_id":parent.customer_id, "recipe":[[r.id,r.component_product_id,str(r.quantity_per_set)] for r in recipe],
        "current_subkit":read_subkit(db, parent.id), "unfinished_order_item_ids":[r.id for r in items],
        "blocked_order_item_ids":blocked,
        "orders":[[r.id,r.quantity,r.delivered_quantity,r.requisition_status,r.material_status] for r in items]}
    facts["fingerprint"] = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return facts


def activate(db, *, expected_fingerprint, actor_id):
    before = preview(db)
    if before["fingerprint"] != expected_fingerprint or before["blocked_order_item_ids"]:
        raise RuntimeError("产品/订单已变化，或存在已进入流程的未完成订单；停止启用，未修改库存")
    actor = db.get(User, actor_id)
    if actor is None or actor.role != "admin" or not actor.is_active:
        raise RuntimeError("必须使用当前有效管理员身份")
    existing = db.get(ProductSubkit, 3765)
    if existing is None:
        result = save_subkit(db, parent_product_id=3765, name="000148内衬", kits_per_parent=1,
            members=[{"product_id":3788,"pieces_per_kit":2},{"product_id":3789,"pieces_per_kit":6}],
            expected_version=0, actor=actor)
    else:
        result = read_subkit(db, 3765)
        if not result["enabled"] or result["kits_per_parent"] != 1 or result["name"] != "000148内衬":
            raise RuntimeError("已有子套件配置不同，禁止覆盖")
    converted = []
    for item_id in before["unfinished_order_item_ids"]:
        if db.get(OrderSubkit, item_id) is None:
            freeze_order_subkit(db, order_item_id=item_id, actor_id=actor.id)
            converted.append(item_id)
    from app.services.audit_log import append_audit_event
    append_audit_event(db, actor=actor, event_category="business", result="success", source="system",
        module_code="products", action_code="activate_000148_subkit", resource="product_subkit",
        entity_type="product", entity_id=3765, customer_id=136,
        details={"task":"BOM-SUBKIT-AUTO-RELEASE-20260909", "before":before,
                 "switched_order_item_ids":converted,"inventory_changed":False})
    return {"subkit":result,"switched_order_item_ids":converted,"inventory_changed":False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--actor-id", type=int)
    parser.add_argument("--backup")
    args = parser.parse_args()
    target = Path(args.database).resolve(strict=True)
    if args.apply:
        if not args.backup or not args.actor_id or not args.expected_fingerprint:
            parser.error("apply requires verified backup, actor-id and expected-fingerprint")
        backup = Path(args.backup).resolve(strict=True)
        if backup == target:
            raise RuntimeError("备份不能是当前数据库")
        with sqlite3.connect(backup.as_uri()+"?mode=ro", uri=True) as check:
            if check.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or check.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("备份校验失败")
    engine = create_sqlite_engine(target)
    with Session(engine) as db:
        if args.apply:
            result = activate(db, expected_fingerprint=args.expected_fingerprint, actor_id=args.actor_id)
            db.commit()
        else:
            result = preview(db)
        print(json.dumps(result, ensure_ascii=False, default=str))
    engine.dispose()


if __name__ == "__main__":
    main()

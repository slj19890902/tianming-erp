"""Create the isolated Mingjunde 200/203 UAT scenario.

This helper is intentionally fail-closed:

- an explicit SQLite path is required;
- the path must contain ``uat``;
- the factory formal database path is always rejected;
- the database must already be migrated to the P0 revision;
- reruns return the existing sample instead of duplicating facts.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXPECTED_REVISION = "ci65v8x9z54"
FORMAL_DATABASE = Path(r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3")
ORDER_NUMBER = "UAT-MJD-200-203-A"
CUSTOMER_NUMBER = 990001
USERNAME = "uat_p0_admin"
PASSWORD = "123456"


def _normalized(path: Path) -> str:
    return str(path.resolve()).casefold()


def _validate_database(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"隔离 UAT 数据库不存在：{path}")
    if _normalized(path) == _normalized(FORMAL_DATABASE):
        raise SystemExit("拒绝连接工厂正式数据库")
    if "uat" not in _normalized(path):
        raise SystemExit("安全门禁：数据库路径必须明确包含 uat")
    with sqlite3.connect(path) as connection:
        revision_rows = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    revision = revision_rows[0][0] if revision_rows else None
    if revision != EXPECTED_REVISION:
        raise SystemExit(
            f"数据库 revision={revision!r}，要求 {EXPECTED_REVISION!r}"
        )
    if integrity != [("ok",)]:
        raise SystemExit(f"数据库完整性检查失败：{integrity!r}")
    if foreign_keys:
        raise SystemExit(f"数据库存在 {len(foreign_keys)} 条外键异常")
    return {
        "revision": revision,
        "integrity_check": "ok",
        "foreign_key_errors": 0,
    }


def seed(path: Path) -> dict:
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models.audit import OperationLog
    from app.models.customer import Customer
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.user import User
    from app.services.production_workflow import (
        create_or_refresh_production_task,
        list_temporary_locations,
    )

    engine = create_sqlite_engine(path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            existing = db.scalar(
                select(Order).where(Order.order_number == ORDER_NUMBER)
            )
            if existing is not None:
                customer = db.get(Customer, existing.customer_id)
                if customer is None:
                    raise RuntimeError("UAT 订单关联客户不存在")
                if customer.customer_number is None:
                    occupied = db.scalar(
                        select(Customer.id).where(
                            Customer.customer_number == CUSTOMER_NUMBER,
                            Customer.id != customer.id,
                        )
                    )
                    if occupied is not None:
                        raise RuntimeError(
                            f"UAT 客户编号 {CUSTOMER_NUMBER} 已被其他客户占用"
                        )
                    customer.customer_number = CUSTOMER_NUMBER
                    db.commit()
                item = db.scalar(
                    select(OrderItem).where(OrderItem.order_id == existing.id)
                )
                task = db.scalar(
                    select(ProductionTask).where(
                        ProductionTask.order_item_id == item.id
                    )
                )
                receipt = db.scalar(
                    select(IncomingReceiptItem).where(
                        IncomingReceiptItem.order_item_id == item.id,
                        IncomingReceiptItem.status == "posted",
                    )
                )
                return {
                    "replayed": True,
                    "order_id": existing.id,
                    "order_number": existing.order_number,
                    "order_item_id": item.id,
                    "production_task_id": task.id if task else None,
                    "incoming_receipt_item_id": receipt.id if receipt else None,
                    "username": USERNAME,
                    "password": PASSWORD,
                }

            user = db.scalar(select(User).where(User.username == USERNAME))
            if user is None:
                user = User(
                    username=USERNAME,
                    password_hash=hash_password(PASSWORD),
                    role="admin",
                    real_name="P0隔离UAT管理员",
                    display_name="P0隔离UAT管理员",
                    must_change_password=False,
                    customer_access_mode="all",
                )
                db.add(user)
                db.flush()
            else:
                user.password_hash = hash_password(PASSWORD)
                user.role = "admin"
                user.must_change_password = False
                user.customer_access_mode = "all"

            customer = db.scalar(
                select(Customer).where(Customer.customer_code == "UAT-MJD-P0")
            )
            if customer is None:
                customer = Customer(
                    customer_number=CUSTOMER_NUMBER,
                    customer_code="UAT-MJD-P0",
                    name="明俊德（P0隔离UAT）",
                    payment_term_days=0,
                    statement_cycle_start_day=1,
                    credit_limit=Decimal("0"),
                    remark="仅用于家庭隔离UAT，不得进入工厂正式库",
                )
                db.add(customer)
                db.flush()

            product = db.scalar(
                select(Product).where(
                    Product.customer_id == customer.id,
                    Product.product_code == "UAT-MJD-OVER-203",
                )
            )
            if product is None:
                product = Product(
                    customer_id=customer.id,
                    product_code="UAT-MJD-OVER-203",
                    customer_material_code="UAT-MJD-OVER-203",
                    product_name="明俊德超收余货验证箱",
                    legacy_material_text="K=A",
                    length_mm=Decimal("400"),
                    width_mm=Decimal("300"),
                    height_mm=Decimal("200"),
                    box_category="normal",
                    box_style="普通箱",
                    flute_type="A",
                    default_cutting_mode="一开一",
                    unit="只",
                    sale_unit_price=Decimal("1.0000"),
                    is_active=True,
                )
                db.add(product)
                db.flush()

            order = Order(
                order_number=ORDER_NUMBER,
                customer_id=customer.id,
                customer_po="明俊德-UAT-200-203",
                order_date=date.today(),
                delivery_date=date.today(),
                status="production",
                payment_status="unpaid",
                total_amount=Decimal("200.00"),
                remark="订单200，实收203，全部投入生产的隔离UAT样本",
                created_by=user.id,
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_order_number=f"{ORDER_NUMBER}-001",
                item_sequence=1,
                quantity=200,
                delivered_quantity=0,
                unit_price=Decimal("1.0000"),
                subtotal=Decimal("200.00"),
                material_status="received",
                material_received_at=datetime.now(),
                material_received_by=user.id,
                snapshot_product_name=product.product_name,
                snapshot_product_code=product.product_code,
                snapshot_spec="400×300×200mm",
                snapshot_material="K=A",
                requisition_qty=200,
                requisition_status="已入库",
                special_process="一开一",
                requisition_date=date.today(),
            )
            db.add(item)
            db.flush()

            incoming = IncomingReceipt(
                receipt_number="UAT-IR-MJD-200-203",
                status="posted",
                received_at=datetime.now(),
                received_by=user.id,
                idempotency_key="uat-ir-mjd-200-203",
                remarks="隔离UAT：报料200，实收203，全部投入生产",
            )
            db.add(incoming)
            db.flush()
            incoming_item = IncomingReceiptItem(
                receipt_id=incoming.id,
                order_id=order.id,
                order_item_id=item.id,
                planned_quantity=200,
                received_quantity=203,
                cumulative_received_quantity=203,
                variance_quantity=3,
                variance_type="over",
                resolution_status="resolved",
                resolution_action="all_to_production",
                resolution_reason="隔离UAT验证超收3张全部投入生产",
                resolved_by=user.id,
                resolved_at=datetime.now(),
                status="posted",
            )
            db.add(incoming_item)
            db.flush()

            task = create_or_refresh_production_task(db, item.id)
            if task is None:
                raise RuntimeError("未能建立生产任务")
            db.add(
                OperationLog(
                    user_id=user.id,
                    action="SEED_P0_OVERRECEIPT_UAT",
                    resource="Order",
                    entity_id=order.id,
                    username=user.username,
                    role=user.role,
                    entity_type="sales_order",
                    details=json.dumps(
                        {
                            "order_quantity": 200,
                            "received_quantity": 203,
                            "resolution_action": "all_to_production",
                            "database": str(path),
                        },
                        ensure_ascii=False,
                    ),
                    description="创建明俊德200/203隔离UAT样本",
                )
            )
            db.commit()

            locations = [
                row
                for row in list_temporary_locations(db)
                if row["is_empty"]
            ]
            return {
                "replayed": False,
                "order_id": order.id,
                "order_number": order.order_number,
                "order_item_id": item.id,
                "incoming_receipt_item_id": incoming_item.id,
                "production_task_id": task.id,
                "ordered_quantity": item.quantity,
                "material_received_quantity": task.material_received_quantity,
                "material_input_quantity": task.material_input_quantity,
                "planned_output_quantity": task.planned_quantity,
                "username": USERNAME,
                "password": PASSWORD,
                "empty_finished_locations": [
                    {
                        "id": row["id"],
                        "code": row["location_code"],
                        "kind": row["location_kind"],
                    }
                    for row in locations[:20]
                ],
            }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    checks = _validate_database(database)
    result = seed(database)
    print(
        json.dumps(
            {
                "database": str(database),
                "checks": checks,
                "sample": result,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()

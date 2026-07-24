"""Create an anonymous, isolated browser-UAT database for BOM requisition checks."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.customer import Customer
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.user import User
from app.services.composite_bom import create_order_item_bom_snapshots
from app.services.composite_bom_workflow import append_component_demand_adjustment


FORMAL_DATABASE = Path(r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3").resolve()
REVISION = "co71v8x9z60"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    database = Path(args.database).resolve()
    if database == FORMAL_DATABASE:
        raise SystemExit("拒绝使用工厂正式数据库")
    if database.exists():
        raise SystemExit(f"目标已存在，拒绝覆盖：{database}")
    database.parent.mkdir(parents=True, exist_ok=True)

    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(
            text("INSERT INTO alembic_version(version_num) VALUES (:revision)"),
            {"revision": REVISION},
        )

    with Session(engine) as session:
        admin = User(
            username="admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="匿名管理员",
            display_name="匿名管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="UAT",
            name="匿名客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        material = Material(
            code="UAT-K=A",
            layer_count=5,
            flute_type="BC",
            supplier_name="匿名纸板供应商",
            is_active=True,
        )
        session.add_all([admin, customer, material])
        session.flush()
        parent = Product(
            customer_id=customer.id,
            product_code="T250-OUTER-UAT",
            customer_material_code="T250-OUTER-UAT",
            product_name="T250 外包装盒（匿名UAT）",
            material_id=material.id,
            box_category="normal",
            box_style="模切内盒",
            default_cutting_mode="一开一",
            report_length_mm=470,
            report_width_mm=600,
            crease_type="净料",
            is_composite=True,
            unit="只",
        )
        component = Product(
            customer_id=customer.id,
            product_code="T250-LINER-UAT",
            customer_material_code="T250-LINER-UAT",
            product_name="T250 内衬（匿名UAT）",
            material_id=material.id,
            box_category="normal",
            box_style="刀卡",
            default_cutting_mode="一开二",
            report_length_mm=575,
            report_width_mm=550,
            crease_type="净料",
            is_internal_component=True,
            unit="只",
        )
        session.add_all([parent, component])
        session.flush()
        session.add(
            ProductBomComponent(
                parent_product_id=parent.id,
                component_product_id=component.id,
                quantity_per_set=Decimal(1),
                display_order=1,
                internal_component_code="T250-OUTER-UAT-S01",
                is_die_cut=False,
                spare_sheet_quantity=0,
                display_mode="internal_only",
                is_required=True,
            )
        )
        order = Order(
            order_number="UAT-BOM-20260724-001",
            customer_id=customer.id,
            customer_po="UAT-PO-001",
            order_date=date(2026, 7, 24),
            delivery_date=date(2026, 7, 30),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("3000"),
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=parent.id,
            item_order_number="UAT-BOM-20260724-001-001",
            item_sequence=1,
            quantity=3000,
            unit_price=Decimal("1"),
            subtotal=Decimal("3000"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=parent.product_code,
            snapshot_product_name=parent.product_name,
            snapshot_spec="470×600",
            snapshot_material=material.code,
            snapshot_original_material_code=material.code,
            snapshot_report_length_mm=470,
            snapshot_report_width_mm=600,
            snapshot_crease_type="净料",
            snapshot_supplier_name=material.supplier_name,
            special_process="一开一",
        )
        session.add(item)
        session.flush()
        snapshots = create_order_item_bom_snapshots(
            session,
            order_item=item,
            parent_product=parent,
        )
        snapshot_id = int(snapshots[0]["id"])
        append_component_demand_adjustment(
            session,
            order_item_id=item.id,
            snapshot_id=snapshot_id,
            required_piece_quantity=2700,
            expected_required_piece_quantity=3000,
            actor_id=admin.id,
            idempotency_key="uat-t250-component-3000-to-2700",
        )
        session.commit()

    print(
        json.dumps(
            {
                "database": str(database),
                "revision": REVISION,
                "username": "admin",
                "password": "123456",
                "order_number": "UAT-BOM-20260724-001",
                "parent_required_pieces": 3000,
                "component_required_pieces": 2700,
                "component_cutting_mode": "一开二",
                "component_theoretical_sheets": 1350,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

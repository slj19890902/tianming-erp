from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models.customer import Customer
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.user import User
from app.services.composite_bom import create_order_item_bom_snapshots
from app.services.production_workflow import create_or_refresh_production_task


EXPECTED_HEAD = "cd60v8x9z49"
UAT_CUSTOMER_CODE = "N039-UAT"
UAT_ORDER_NUMBER = "N039-UAT-001"


def _require_isolated_copy(database: Path) -> Path:
    resolved = database.resolve()
    allowed_root = Path(r"C:\tmp").resolve()
    if allowed_root not in resolved.parents:
        raise SystemExit("N039 UAT database must be under C:\\tmp")
    if not resolved.name.startswith("n039_formal_copy_"):
        raise SystemExit("N039 UAT database name must start with n039_formal_copy_")
    if not resolved.is_file():
        raise SystemExit(f"N039 UAT database does not exist: {resolved}")
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare an isolated N039 UAT account and deterministic test order."
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="123456")
    args = parser.parse_args()
    database = _require_isolated_copy(args.database)

    engine = create_sqlite_engine(database)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        head = db.connection().exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        if head != EXPECTED_HEAD:
            raise SystemExit(f"N039 UAT database head must be {EXPECTED_HEAD}, got {head}")

        user = db.scalar(select(User).where(User.username == args.username))
        if user is None or not user.is_active:
            raise SystemExit("isolated UAT account is missing or inactive")
        user.password_hash = hash_password(args.password)
        user.must_change_password = False
        user.auth_version = int(user.auth_version or 0) + 1

        customer = db.scalar(
            select(Customer).where(Customer.customer_code == UAT_CUSTOMER_CODE)
        )
        if customer is None:
            next_number = int(db.scalar(select(func.max(Customer.customer_number))) or 0) + 1
            customer = Customer(
                customer_number=next_number,
                customer_code=UAT_CUSTOMER_CODE,
                name="N039隔离验收客户",
                payment_term_days=30,
                statement_cycle_start_day=20,
                credit_limit=Decimal("100000"),
                delivery_method="配送",
                status="active",
                is_active=True,
            )
            db.add(customer)
            db.flush()

        material = db.scalar(
            select(Material).where(Material.code == "N039-UAT-K616K")
        )
        if material is None:
            material = Material(
                code="N039-UAT-K616K",
                paper_composition="K616K",
                layer_count=5,
                flute_type="AB",
                supplier_name="N039隔离供应商",
                is_active=True,
            )
            db.add(material)
            db.flush()

        product_specs = (
            ("N039-KIT", "N039组合成品（按套）", False, True, 600, 400, 300, 1200, 700),
            ("N039-COVER", "N039组件盖（每套2片）", True, False, 600, 400, 100, 1100, 650),
            ("N039-BASE", "N039组件底（每套1片）", True, False, 580, 380, 100, 1050, 620),
        )
        products: dict[str, Product] = {}
        for (
            code,
            name,
            is_component,
            is_composite,
            length,
            width,
            height,
            report_length,
            report_width,
        ) in product_specs:
            product = db.scalar(
                select(Product).where(
                    Product.customer_id == customer.id,
                    Product.product_code == code,
                )
            )
            if product is None:
                product = Product(
                    customer_id=customer.id,
                    product_code=code,
                    customer_material_code=code,
                    product_name=name,
                    material_id=material.id,
                    legacy_material_text=material.code,
                    length_mm=Decimal(length),
                    width_mm=Decimal(width),
                    height_mm=Decimal(height),
                    box_category="normal",
                    box_style="组合产品" if is_composite else "组合组件",
                    unit="套" if is_composite else "片",
                    sale_unit_price=Decimal("10") if is_composite else None,
                    report_length_mm=report_length,
                    report_width_mm=report_width,
                    crease_type="净料",
                    flute_type="AB",
                    layer_count=5,
                    is_composite=is_composite,
                    is_internal_component=is_component,
                    is_active=True,
                )
                db.add(product)
                db.flush()
            products[code] = product

        parent = products["N039-KIT"]
        for display_order, code, quantity in (
            (1, "N039-COVER", 2),
            (2, "N039-BASE", 1),
        ):
            component = products[code]
            relation = db.scalar(
                select(ProductBomComponent).where(
                    ProductBomComponent.parent_product_id == parent.id,
                    ProductBomComponent.component_product_id == component.id,
                )
            )
            if relation is None:
                db.add(
                    ProductBomComponent(
                        parent_product_id=parent.id,
                        component_product_id=component.id,
                        quantity_per_set=Decimal(quantity),
                        display_order=display_order,
                        internal_component_code=f"N039-S{display_order:02d}",
                        is_die_cut=False,
                        spare_sheet_quantity=0,
                        display_mode="internal_only",
                        is_required=True,
                    )
                )
        db.flush()

        order = db.scalar(select(Order).where(Order.order_number == UAT_ORDER_NUMBER))
        if order is None:
            order = Order(
                order_number=UAT_ORDER_NUMBER,
                customer_id=customer.id,
                customer_po="N039-UAT-PO-001",
                order_date=date.today(),
                delivery_date=date.today() + timedelta(days=7),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("50"),
                remark="仅用于 N039 隔离验收",
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=parent.id,
                item_order_number=f"{UAT_ORDER_NUMBER}-001",
                item_sequence=1,
                quantity=5,
                unit_price=Decimal("10"),
                subtotal=Decimal("50"),
                material_id=material.id,
                material_status="pending",
                requisition_status="未报料",
                delivered_quantity=0,
                snapshot_product_code=parent.product_code,
                snapshot_product_name=parent.product_name,
                snapshot_spec="600×400×300mm",
                snapshot_material=f"{material.code} / AB",
                snapshot_report_length_mm=parent.report_length_mm,
                snapshot_report_width_mm=parent.report_width_mm,
                snapshot_crease_type=parent.crease_type,
                flute_type="AB",
            )
            db.add(item)
            db.flush()
            create_order_item_bom_snapshots(
                db,
                order_item=item,
                parent_product=parent,
            )
            create_or_refresh_production_task(db, item.id)
        db.commit()
        print(f"database={database}")
        print(f"uat_username={user.username}")
        print(f"uat_password={args.password}")
        print(f"uat_customer={customer.name}")
        print(f"uat_order={UAT_ORDER_NUMBER}")

    engine.dispose()


if __name__ == "__main__":
    main()

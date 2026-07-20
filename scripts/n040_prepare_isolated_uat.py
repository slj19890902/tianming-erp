from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models.customer import Customer
from app.models.customer_material import CustomerMaterialCandidate
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.user import User
from app.services.customer_material_candidates import (
    normalize_material_candidate_key,
    normalize_supplier_candidate_key,
)


EXPECTED_HEAD = "ce61v8x9z50"
UAT_CUSTOMER_CODE = "N040-UAT"
UAT_ORDER_NUMBER = "N040-UAT-001"
ORIGINAL_MATERIAL_CODE = "9CCC9"


def _require_isolated_copy(database: Path) -> Path:
    resolved = database.resolve()
    allowed_root = Path(r"C:\tmp").resolve()
    if allowed_root not in resolved.parents:
        raise SystemExit("N040 UAT database must be under C:\\tmp")
    if not resolved.name.lower().startswith("n040_"):
        raise SystemExit("N040 UAT database name must start with n040_")
    if not resolved.is_file():
        raise SystemExit(f"N040 UAT database does not exist: {resolved}")
    return resolved


def _material(db, *, code: str, supplier_name: str, price: str) -> Material:
    material = db.scalar(
        select(Material).where(
            Material.code == code,
            Material.supplier_name == supplier_name,
        )
    )
    if material is None:
        material = Material(
            code=code,
            paper_composition=code,
            layer_count=5,
            flute_type="AB",
            supplier_name=supplier_name,
            quote_price=Decimal(price),
            is_active=True,
        )
        db.add(material)
        db.flush()
    return material


def _candidate(
    db,
    *,
    customer: Customer,
    material: Material,
    priority: int,
    user: User,
) -> CustomerMaterialCandidate:
    normalized_original = normalize_material_candidate_key(ORIGINAL_MATERIAL_CODE)
    candidate = db.scalar(
        select(CustomerMaterialCandidate).where(
            CustomerMaterialCandidate.customer_id == customer.id,
            CustomerMaterialCandidate.normalized_original_material_code
            == normalized_original,
            CustomerMaterialCandidate.actual_material_id == material.id,
        )
    )
    if candidate is None:
        supplier_name = material.supplier_name or "未设置供应商"
        candidate = CustomerMaterialCandidate(
            customer_id=customer.id,
            original_material_code=ORIGINAL_MATERIAL_CODE,
            normalized_original_material_code=normalized_original,
            supplier_name=supplier_name,
            normalized_supplier_name=normalize_supplier_candidate_key(supplier_name),
            actual_material_id=material.id,
            actual_material_code_snapshot=material.code,
            manual_priority=priority,
            is_active=True,
            source="uat_seed",
            notes="N040 隔离验收候选",
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(candidate)
        db.flush()
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare an isolated N040 candidate-selection UAT database."
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
            raise SystemExit(
                f"N040 UAT database head must be {EXPECTED_HEAD}, got {head}"
            )

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
                name="N040隔离验收客户",
                payment_term_days=30,
                statement_cycle_start_day=20,
                credit_limit=Decimal("100000"),
                delivery_method="配送",
                status="active",
                is_active=True,
            )
            db.add(customer)
            db.flush()

        first_material = _material(
            db,
            code="N040-A416D",
            supplier_name="N040嘉林亿候选",
            price="2.31",
        )
        second_material = _material(
            db,
            code="N040-G719N",
            supplier_name="N040鸣朋候选",
            price="2.28",
        )
        product = db.scalar(
            select(Product).where(
                Product.customer_id == customer.id,
                Product.product_code == "N040-BOX-001",
            )
        )
        if product is None:
            product = Product(
                customer_id=customer.id,
                product_code="N040-BOX-001",
                customer_material_code="N040-BOX-001",
                product_name="N040客户材质候选验收纸箱",
                material_id=first_material.id,
                legacy_material_text=ORIGINAL_MATERIAL_CODE,
                length_mm=Decimal("600"),
                width_mm=Decimal("400"),
                height_mm=Decimal("300"),
                box_category="normal",
                box_style="A1",
                unit="个",
                sale_unit_price=Decimal("10"),
                report_length_mm=1200,
                report_width_mm=700,
                crease_type="净料",
                flute_type="AB",
                layer_count=5,
                is_active=True,
            )
            db.add(product)
            db.flush()

        order = db.scalar(select(Order).where(Order.order_number == UAT_ORDER_NUMBER))
        if order is None:
            order = Order(
                order_number=UAT_ORDER_NUMBER,
                customer_id=customer.id,
                customer_po="N040-UAT-PO-001",
                order_date=date.today(),
                delivery_date=date.today() + timedelta(days=7),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("100"),
                remark="仅用于 N040 隔离验收",
            )
            db.add(order)
            db.flush()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    item_order_number=f"{UAT_ORDER_NUMBER}-001",
                    item_sequence=1,
                    quantity=10,
                    unit_price=Decimal("10"),
                    subtotal=Decimal("100"),
                    material_id=first_material.id,
                    material_status="pending",
                    requisition_status="未报料",
                    delivered_quantity=0,
                    snapshot_product_code=product.product_code,
                    snapshot_product_name=product.product_name,
                    snapshot_spec="600×400×300mm",
                    snapshot_material=ORIGINAL_MATERIAL_CODE,
                    snapshot_original_material_code=ORIGINAL_MATERIAL_CODE,
                    snapshot_supplier_name=first_material.supplier_name,
                    snapshot_report_length_mm=product.report_length_mm,
                    snapshot_report_width_mm=product.report_width_mm,
                    snapshot_crease_type=product.crease_type,
                    layer_count=5,
                    flute_type="AB",
                )
            )
            db.flush()

        order_item = db.scalar(
            select(OrderItem).where(
                OrderItem.order_id == order.id,
                OrderItem.product_id == product.id,
            )
        )
        if order_item is None:
            raise SystemExit("N040 UAT order item is missing")

        confirmed_order = db.scalar(
            select(SupplierRequisitionOrder).where(
                SupplierRequisitionOrder.order_number == "SRO-N040-UAT-0001"
            )
        )
        if confirmed_order is None:
            confirmed_order = SupplierRequisitionOrder(
                order_number="SRO-N040-UAT-0001",
                supplier_name=first_material.supplier_name,
                material_id=first_material.id,
                layer_count=5,
                flute_type="AB",
                total_quantity=10,
                requisition_qty=10,
                status="confirmed",
                created_by=user.id,
            )
            db.add(confirmed_order)
            db.flush()
            db.add(
                SupplierRequisitionOrderItem(
                    supplier_order_id=confirmed_order.id,
                    order_item_id=order_item.id,
                    product_id=product.id,
                    material_id=first_material.id,
                    material_code_snapshot=first_material.code,
                    supplier_name_snapshot=first_material.supplier_name,
                    layer_count_snapshot=5,
                    flute_type_snapshot="AB",
                    order_number=order_item.item_order_number,
                    product_code=product.product_code,
                    product_name=product.product_name,
                    quantity=10,
                    requisition_qty=10,
                    customer_name=customer.name,
                )
            )

        voided_order = db.scalar(
            select(SupplierRequisitionOrder).where(
                SupplierRequisitionOrder.order_number == "SRO-N040-UAT-VOIDED"
            )
        )
        if voided_order is None:
            voided_order = SupplierRequisitionOrder(
                order_number="SRO-N040-UAT-VOIDED",
                supplier_name=second_material.supplier_name,
                material_id=second_material.id,
                layer_count=5,
                flute_type="AB",
                total_quantity=4,
                requisition_qty=4,
                status="voided",
                created_by=user.id,
            )
            db.add(voided_order)
            db.flush()
            db.add(
                SupplierRequisitionOrderItem(
                    supplier_order_id=voided_order.id,
                    order_item_id=order_item.id,
                    product_id=product.id,
                    material_id=second_material.id,
                    material_code_snapshot=second_material.code,
                    supplier_name_snapshot=second_material.supplier_name,
                    layer_count_snapshot=5,
                    flute_type_snapshot="AB",
                    order_number=order_item.item_order_number,
                    product_code=product.product_code,
                    product_name=product.product_name,
                    quantity=4,
                    requisition_qty=4,
                    customer_name=customer.name,
                )
            )

        first_candidate = _candidate(
            db,
            customer=customer,
            material=first_material,
            priority=20,
            user=user,
        )
        second_candidate = _candidate(
            db,
            customer=customer,
            material=second_material,
            priority=10,
            user=user,
        )
        db.commit()
        print(f"database={database}")
        print(f"uat_username={user.username}")
        print(f"uat_password={args.password}")
        print(f"uat_customer={customer.name}")
        print(f"uat_order={UAT_ORDER_NUMBER}")
        print(f"candidate_ids={first_candidate.id},{second_candidate.id}")

    engine.dispose()


if __name__ == "__main__":
    main()

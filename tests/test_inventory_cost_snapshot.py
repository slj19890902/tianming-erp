from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.material import Material
from app.models.product import Product
from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
from app.models.warehouse_inventory import WarehouseLocation
from app.services.inventory_cost_snapshot import estimate_finished_product_cost
from app.services.inventory_insights import build_inventory_insights
from app.services.warehouse_inventory import manual_finished_in, manual_semi_finished_in


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "inventory-cost-snapshot.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def _customer(db: Session) -> Customer:
    row = Customer(
        customer_number=9821,
        customer_code="COST-SNAPSHOT-CUSTOMER",
        name="成本快照测试客户",
        payment_term_days=0,
        credit_limit=0,
    )
    db.add(row)
    db.flush()
    return row


def _material(
    db: Session,
    *,
    code: str,
    price: str,
    supplier: str = "昆山鸣朋",
) -> Material:
    row = Material(
        code=code,
        layer_count=3,
        quote_price=Decimal(price),
        price_unit="元/㎡",
        supplier_name=supplier,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row


def test_finished_entry_snapshots_report_area_times_square_price(db: Session) -> None:
    customer = _customer(db)
    material = _material(db, code="F6A", price="1.8000")
    product = Product(
        customer_id=customer.id,
        product_code="COST-FG-001",
        customer_material_code="COST-FG-001",
        product_name="成品材料估算测试",
        material_id=material.id,
        default_material_code=material.code,
        layer_count=3,
        flute_type="B",
        report_length_mm=1360,
        report_width_mm=1270,
        pieces_per_box=1,
        box_category="normal",
    )
    location = WarehouseLocation(
        location_code="COST-FG-01",
        location_name="成本成品库位",
        warehouse_type="finished",
    )
    db.add_all([product, location])
    db.flush()

    lot = manual_finished_in(
        db,
        customer_id=customer.id,
        product_id=product.id,
        location_id=location.id,
        quantity=20,
        stock_date=date(2026, 7, 13),
        source_type="stocktake",
        remarks=None,
        operator_id=None,
        idempotency_key="cost-finished-entry",
    )

    assert lot.estimated_cost_area_m2_snapshot == Decimal("1.727200")
    assert lot.estimated_square_price_snapshot == Decimal("1.8000")
    assert lot.estimated_unit_cost_snapshot == Decimal("3.1090")
    assert lot.cost_snapshot_source == "material_quote_area"

    material.quote_price = Decimal("9.9000")
    db.flush()
    result = build_inventory_insights(db, as_of=date(2026, 7, 13))
    action = result["action_items"][0]
    assert result["summary"]["estimated_inventory_value"] == "62.18"
    assert action["cost_status"] == "estimated_snapshot"
    assert action["estimated_unit_cost"] == "3.11"


def test_a3_finished_cost_adds_cover_and_base_areas_once(db: Session) -> None:
    customer = _customer(db)
    material = _material(db, code="N7N", price="2.0000")
    product = Product(
        customer_id=customer.id,
        product_code="COST-A3-001",
        customer_material_code="COST-A3-001",
        product_name="天地盖材料估算测试",
        material_id=material.id,
        default_material_code=material.code,
        layer_count=3,
        flute_type="B",
        report_length_mm=2345,
        report_width_mm=1060,
        base_report_length_mm=2320,
        base_report_width_mm=1035,
        pieces_per_box=2,
        box_category="die_cut",
    )
    db.add(product)
    db.flush()

    estimate = estimate_finished_product_cost(db, product=product)

    assert estimate is not None
    assert estimate.area_m2 == Decimal("4.886900")
    assert estimate.unit_cost == Decimal("9.7738")
    assert [row["component"] for row in estimate.detail["components"]] == [
        "cover",
        "base",
    ]


def test_missing_price_unit_remains_cost_pending(db: Session) -> None:
    customer = _customer(db)
    material = _material(db, code="NO-UNIT", price="2.0000")
    material.price_unit = None
    product = Product(
        customer_id=customer.id,
        product_code="COST-NO-UNIT",
        customer_material_code="COST-NO-UNIT",
        product_name="Missing price unit",
        material_id=material.id,
        default_material_code=material.code,
        layer_count=3,
        flute_type="B",
        report_length_mm=1000,
        report_width_mm=1000,
        pieces_per_box=1,
        box_category="normal",
    )
    db.add(product)
    db.flush()

    assert estimate_finished_product_cost(db, product=product) is None


@pytest.mark.parametrize(
    ("base_length", "base_width"),
    [(2320, None), (None, 1035), (0, 1035), (2320, 0)],
)
def test_a3_incomplete_base_dimensions_remain_cost_pending(
    db: Session,
    base_length: int | None,
    base_width: int | None,
) -> None:
    customer = _customer(db)
    material = _material(db, code="A3-PENDING", price="2.0000")
    product = Product(
        customer_id=customer.id,
        product_code="COST-A3-PENDING",
        customer_material_code="COST-A3-PENDING",
        product_name="A3 incomplete base dimensions",
        material_id=material.id,
        default_material_code=material.code,
        layer_count=3,
        flute_type="B",
        box_style="A3",
        report_length_mm=2345,
        report_width_mm=1060,
        base_report_length_mm=base_length,
        base_report_width_mm=base_width,
        pieces_per_box=2,
        box_category="die_cut",
    )
    db.add(product)
    db.flush()

    assert estimate_finished_product_cost(db, product=product) is None


def test_semi_entry_resolves_material_code_and_snapshots_sheet_cost(db: Session) -> None:
    _material(db, code="N7N", price="2.2800")
    location = WarehouseLocation(
        location_code="COST-SI-01",
        location_name="成本半成品库位",
        warehouse_type="semi_finished",
    )
    db.add(location)
    db.flush()

    lot = manual_semi_finished_in(
        db,
        location_id=location.id,
        quantity=30,
        stock_date=date(2026, 7, 13),
        source_type="stocktake",
        material_code="N7N",
        layer_count=3,
        flute_type="B",
        board_length_mm=1200,
        board_width_mm=800,
        sheet_type="raw_board",
        supplier_name="昆山鸣朋",
        customer_id=None,
        crease_type=None,
        crease_left_mm=None,
        crease_middle_mm=None,
        crease_right_mm=None,
        cutting_note=None,
        remarks=None,
        operator_id=None,
        idempotency_key="cost-semi-entry",
    )

    assert lot.estimated_cost_area_m2_snapshot == Decimal("0.960000")
    assert lot.estimated_square_price_snapshot == Decimal("2.2800")
    assert lot.estimated_unit_cost_snapshot == Decimal("2.1888")
    result = build_inventory_insights(db, as_of=date(2026, 7, 13))
    assert result["summary"]["estimated_inventory_value"] == "65.66"
    assert result["data_quality"]["missing_cost_lots"] == 0


def test_missing_dimensions_or_material_price_remains_cost_pending(db: Session) -> None:
    customer = _customer(db)
    material = Material(code="NO-PRICE", layer_count=3, price_unit="元/㎡", is_active=True)
    product = Product(
        customer_id=customer.id,
        product_code="COST-MISSING-001",
        customer_material_code="COST-MISSING-001",
        product_name="成本缺失测试",
        material=material,
        layer_count=3,
        flute_type="B",
        box_category="normal",
    )
    db.add(product)
    db.flush()

    assert estimate_finished_product_cost(db, product=product) is None


def test_estimate_uses_existing_flute_price_adjustment_rule(db: Session) -> None:
    customer = _customer(db)
    material = _material(
        db,
        code="C6C",
        price="1.3100",
        supplier="苏州嘉林亿包装科技有限公司",
    )
    db.add(
        SupplierFlutePriceRule(
            supplier_name=material.supplier_name,
            layer_count=3,
            flute_type="A",
            price_delta=Decimal("0.0400"),
            is_active=True,
        )
    )
    product = Product(
        customer_id=customer.id,
        product_code="COST-FLUTE-001",
        customer_material_code="COST-FLUTE-001",
        product_name="楞型加价成本测试",
        material=material,
        layer_count=3,
        flute_type="A",
        report_length_mm=1000,
        report_width_mm=1000,
        pieces_per_box=1,
        box_category="normal",
    )
    db.add(product)
    db.flush()

    estimate = estimate_finished_product_cost(db, product=product)

    assert estimate is not None
    assert estimate.square_price == Decimal("1.3500")
    assert estimate.unit_cost == Decimal("1.3500")
    assert estimate.detail["flute_delta"] == 0.04

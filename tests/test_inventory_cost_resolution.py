from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.material import Material
from app.models.product import Product
from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
from app.services.inventory_cost_snapshot import (
    apply_cost_snapshot,
    estimate_finished_product_cost,
    estimate_semi_finished_cost,
)


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "inventory-cost-resolution.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    engine.dispose()


def _material(
    db: Session,
    *,
    code: str,
    supplier: str,
    price: str,
) -> Material:
    material = Material(
        code=code,
        supplier_name=supplier,
        quote_price=Decimal(price),
        price_unit="元/㎡",
        layer_count=3,
        is_active=True,
    )
    db.add(material)
    db.flush()
    return material


def _add_flute_delta(db: Session, *, supplier: str, delta: str) -> None:
    db.add(
        SupplierFlutePriceRule(
            supplier_name=supplier,
            layer_count=3,
            flute_type="B",
            price_delta=Decimal(delta),
            is_active=True,
        )
    )
    db.flush()


def test_material_id_with_conflicting_supplier_stays_cost_pending(db: Session) -> None:
    material = _material(db, code="C4C", supplier="Supplier A", price="2.0000")
    _add_flute_delta(db, supplier="Supplier B", delta="0.5000")

    estimate = estimate_semi_finished_cost(
        db,
        material_id=material.id,
        material_code="C4C",
        supplier_name="Supplier B",
        layer_count=3,
        flute_type="B",
        board_length_mm=1000,
        board_width_mm=1000,
    )

    assert estimate is None


def test_same_code_without_supplier_is_ambiguous_but_exact_supplier_resolves(db: Session) -> None:
    _material(db, code="N7N", supplier="Supplier A", price="2.0000")
    supplier_b_material = _material(
        db, code="n7n", supplier="Supplier B", price="3.0000"
    )
    _add_flute_delta(db, supplier="Supplier B", delta="0.5000")

    ambiguous = estimate_semi_finished_cost(
        db,
        material_id=None,
        material_code="N7N",
        supplier_name=None,
        layer_count=3,
        flute_type="B",
        board_length_mm=1000,
        board_width_mm=1000,
    )
    exact = estimate_semi_finished_cost(
        db,
        material_id=None,
        material_code="N7N",
        supplier_name="Supplier B",
        layer_count=3,
        flute_type="B",
        board_length_mm=1000,
        board_width_mm=1000,
    )

    assert ambiguous is None
    assert exact is not None
    assert exact.detail["material_id"] == supplier_b_material.id
    assert exact.square_price == Decimal("3.5000")
    assert exact.unit_cost == Decimal("3.5000")


def test_symbol_material_code_resolves_without_stripping_plus(db: Session) -> None:
    material = _material(db, code="A+A", supplier="Supplier A", price="2.0000")

    estimate = estimate_semi_finished_cost(
        db,
        material_id=None,
        material_code="A+A",
        supplier_name="Supplier A",
        layer_count=3,
        flute_type=None,
        board_length_mm=1000,
        board_width_mm=1000,
    )

    assert estimate is not None
    assert estimate.detail["material_id"] == material.id
    assert estimate.detail["material_code"] == "A+A"
    assert estimate.square_price == Decimal("2.0000")


def test_finished_formula_uses_pieces_and_current_effective_square_price(db: Session) -> None:
    customer = Customer(
        customer_number=9822,
        customer_code="COST-RESOLUTION",
        name="Cost resolution customer",
        payment_term_days=0,
        credit_limit=0,
    )
    material = _material(db, code="F6A", supplier="Supplier A", price="2.0000")
    _add_flute_delta(db, supplier="Supplier A", delta="0.5000")
    product = Product(
        customer=customer,
        product_code="COST-RESOLUTION-FG",
        customer_material_code="COST-RESOLUTION-FG",
        product_name="Finished formula",
        material=material,
        layer_count=3,
        flute_type="B",
        report_length_mm=1000,
        report_width_mm=500,
        pieces_per_box=3,
        box_category="normal",
    )
    db.add(product)
    db.flush()

    estimate = estimate_finished_product_cost(db, product=product)

    assert estimate is not None
    assert estimate.area_m2 == Decimal("1.500000")
    assert estimate.square_price == Decimal("2.5000")
    assert estimate.unit_cost == Decimal("3.7500")
    assert (
        estimate.detail["formula"]
        == "length_mm * width_mm / 1,000,000 * current_effective_material_square_price"
    )
    assert estimate.detail["supplier_name"] == "Supplier A"
    assert estimate.detail["price_unit"] == "元/㎡"
    assert estimate.detail["estimate_basis"] == "current_material_quote_not_actual_cash_cost"

    snapshot = SimpleNamespace()
    apply_cost_snapshot(snapshot, estimate)
    snapshot_detail = json.loads(snapshot.cost_snapshot_detail_json)
    assert snapshot_detail["supplier_name"] == "Supplier A"
    assert snapshot_detail["price_unit"] == "元/㎡"


def test_a3_finished_cost_keeps_cover_and_base_without_piece_multiplier(db: Session) -> None:
    customer = Customer(
        customer_number=9823,
        customer_code="COST-RESOLUTION-A3",
        name="A3 regression customer",
        payment_term_days=0,
        credit_limit=0,
    )
    material = _material(db, code="A3A", supplier="Supplier A", price="2.0000")
    product = Product(
        customer=customer,
        product_code="COST-RESOLUTION-A3",
        customer_material_code="COST-RESOLUTION-A3",
        product_name="A3 cover and base",
        material=material,
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
    assert [component["component"] for component in estimate.detail["components"]] == [
        "cover",
        "base",
    ]
    assert all("pieces_per_box" not in component for component in estimate.detail["components"])

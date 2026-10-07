from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select

from app.core.sheet_dimensions import SheetDimension, sheet_dimension_number, validate_sheet_dimensions
from app.models.dimension_type import SheetDimensionColumn


@pytest.mark.parametrize("value, expected", [(444.5, 444.5), ("298.50", 298.5), ("444.25", 444.25), (540, 540), ("318.00", 318)])
def test_measurement_keeps_decimal_and_integer_identity(value, expected):
    actual = TypeAdapter(SheetDimension).validate_python(value)
    assert actual == expected and type(actual) is type(expected)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "298.555", "bad"])
def test_invalid_precision_never_rounds(value):
    with pytest.raises((ValidationError, ValueError)):
        TypeAdapter(SheetDimension).validate_python(value)


def test_sqlite_integer_affinity_and_orm_roundtrip():
    engine = create_engine("sqlite://")
    table = Table("dimensions", MetaData(), Column("id", Integer, primary_key=True), Column("length", SheetDimensionColumn))
    table.metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(table.insert(), [{"id": 1, "length": 318}, {"id": 2, "length": 298.5}, {"id": 3, "length": Decimal("444.25")}])
        assert db.exec_driver_sql("select length, typeof(length) from dimensions order by id").all() == [(318, "integer"), (298.5, "real"), (444.25, "real")]
        assert db.scalars(select(table.c.length).order_by(table.c.id)).all() == [318, 298.5, 444.25]


def test_card_and_corrugated_classification():
    validate_sheet_dimensions(298.5, 444.5, layer_count=1, flute_type="NONE")
    validate_sheet_dimensions(318, 540, layer_count=3, flute_type="B")
    for layer, flute in [(3, "B"), (5, "AB"), (None, None), (1, "B")]:
        with pytest.raises(ValueError, match="整数"):
            validate_sheet_dimensions(298.5, 444.5, layer_count=layer, flute_type=flute)


def test_product_validation_uses_actual_material_not_client_layer():
    from app.api.products import _validate_product_material_flute
    material = SimpleNamespace(layer_count=3, code="VIK")
    db = SimpleNamespace(get=lambda *args: material)
    payload = SimpleNamespace(material_id=1, layer_count=None, flute_type="B", legacy_material_text=None,
                              report_length_mm=298.5, report_width_mm=444.5, base_report_length_mm=None, base_report_width_mm=None)
    with pytest.raises(HTTPException, match="整数"):
        _validate_product_material_flute(db, payload)
    material.layer_count = 1
    payload.layer_count = 1
    payload.flute_type = "NONE"
    _validate_product_material_flute(db, payload)
    assert payload.report_length_mm == 298.5


def test_sheet_entry_rejects_corrugated_fraction():
    from tests.test_plain_paper_inventory import payload
    assert payload(board_length_mm=298.5, board_width_mm=444.5).board_length_mm == 298.5
    with pytest.raises(ValidationError, match="整数"):
        payload(board_length_mm=298.5, layer_count=3, flute_type="B", supplier_id=None, sheet_unit_cost=None)


def test_fractional_stock_entry_replay_and_persist(stocktake_app):
    from tests.test_plain_paper_inventory import setup
    from app.api.warehouse_goods import create_sheet
    from app.models.warehouse_inventory import InventoryLot
    _, factory, ids, _ = stocktake_app
    with factory() as db:
        user, _, entry = setup(db, ids)
        entry.board_length_mm, entry.board_width_mm = 298.5, 444.5
        result = create_sheet(entry, db, user)
        db.expire_all()
        detail = db.get(InventoryLot, result["lot_id"]).semi_finished_detail
        assert (detail.board_length_mm, detail.board_width_mm) == (298.5, 444.5)
        assert create_sheet(entry, db, user) == result
        entry.board_width_mm = 444.25
        with pytest.raises(HTTPException):
            create_sheet(entry, db, user)


from test_warehouse_goods import stocktake_app
from tests.test_semi_finished_order_reservation import b1_app
from tests.test_p1_32a2_requisition_production_print import production_print_app


def test_new_order_freezes_fractional_card_dimensions(b1_app):
    from fastapi.testclient import TestClient
    from app.models.product import Product
    from app.models.order import OrderItem
    from tests.test_semi_finished_order_reservation import login, post_order, order_item
    from tests.test_t02_order_import_source_identity import _ready_product
    app, factory = b1_app
    _ready_product(factory)
    with factory() as db:
        p = db.get(Product, 1)
        p.layer_count = p.material.layer_count = 1
        p.flute_type = p.material.flute_type = "NONE"
        p.report_length_mm, p.report_width_mm = 298.5, 444.5
        db.commit()
    with TestClient(app) as client:
        login(client, "admin")
        saved = post_order(client, [order_item(1, 10)], "CARD-FRACTION")
        assert saved.status_code == 201, saved.text
    with factory() as db:
        row = db.scalar(select(OrderItem))
        assert (row.snapshot_report_length_mm, row.snapshot_report_width_mm) == (298.5, 444.5)
        p = db.get(Product, 1)
        p.report_length_mm = 333.5
        db.commit()
        db.expire_all()
        assert row.snapshot_report_length_mm == 298.5


def test_supplier_print_retains_fractional_dimensions(production_print_app):
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import build_supplier_requisition_production_package
    with production_print_app["session_factory"]() as db:
        order = db.get(SupplierRequisitionOrder, production_print_app["supplier_order_id"])
        row = sorted(order.items, key=lambda row: row.id)[0]
        row.report_length_mm, row.report_width_mm = 298.5, 444.5
        db.flush()
        package = build_supplier_requisition_production_package(db, order)
        card = next(card for card in package["cards"] if card["supplier_order_item_id"] == row.id)
        component = card["components"][0]
        assert (component["report_length_mm"], component["report_width_mm"]) == (298.5, 444.5)
        db.rollback()


def test_custom_stock_policy_cannot_introduce_corrugated_fraction():
    from app.api.requisition import StockPolicyPayload, _apply_stock_policy_payload
    payload = StockPolicyPayload(policy_name="precision", target_inventory_type="semi_finished",
        layer_count=3, flute_type="B", report_length_mm=298.5, report_width_mm=444.5,
        warning_quantity=10, target_quantity=100)
    row = SimpleNamespace()
    with pytest.raises(HTTPException, match="整数"):
        _apply_stock_policy_payload(row, payload, user_id=1)
    assert not vars(row)
    payload.layer_count, payload.flute_type = 1, "NONE"
    _apply_stock_policy_payload(row, payload, user_id=1)
    assert row.report_length_mm == 298.5

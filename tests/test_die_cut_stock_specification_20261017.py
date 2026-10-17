"""A multi-up die is an identity source, never a finished-piece dimension."""
from datetime import date
import json

from app.core.receipt_price_guard import ReceiptPriceGuardSession
from app.core import inventory_entry_guard  # noqa: F401
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.warehouse_inventory import WarehouseLocation
from app.services.product_specification import product_stock_specification, resolved_product_specification
from app.services.warehouse_inventory import manual_finished_in
from tests.test_inventory_cost_snapshot import db, _customer, _material


def test_registered_multimold_stock_keeps_piece_count_without_inventing_dimensions(db):
    customer = _customer(db)
    material = _material(db, code="DIE-SPEC", price="2")
    mold = MoldTool(mold_code="DIE-4", mold_name="四模", rack_location="R04", is_active=True)
    db.add(mold); db.flush()
    product = Product(customer_id=customer.id, product_code="CUT-4", customer_material_code="CUT-4",
        product_name="已完成模切产品", unit="只", material_id=material.id, layer_count=3, flute_type="B",
        box_category="die_cut", box_style="刀卡", production_process="模切,无需结合", mold_tool_id=mold.id,
        report_length_mm=257, report_width_mm=380, crease_type="净料", pieces_per_box=1,
        sheet_cutting_settings={"schema_version":2,"whole":dict(length_parts=4,width_parts=1,mold_count=4,is_die_cut=True)})
    location = WarehouseLocation(location_code="DIE-SPEC-TEST",location_name="隔离成品位",warehouse_type="finished")
    db.add_all([product,location]); db.commit()
    assert product.length_mm is None and product.width_mm is None
    assert "4模" in product_stock_specification(product)
    assert "单片尺寸未登记" not in product_stock_specification(product)
    assert resolved_product_specification(None,product) == product_stock_specification(product)
    assert resolved_product_specification("旧单冻结规格",product) == "旧单冻结规格"
    args = dict(customer_id=customer.id,product_id=product.id,location_id=location.id,quantity=8,
        stock_date=date(2026,9,22),source_type="stocktake",remarks="隔离验证",operator_id=None,
        idempotency_key="die-spec-eight-pieces")
    with ReceiptPriceGuardSession(bind=db.get_bind()) as session:
        lot = manual_finished_in(session,**args); session.commit()
        assert lot.quantity_available == 8
        assert lot.finished_detail.length_mm is None and lot.finished_detail.width_mm is None
        basis = json.loads(lot.finished_detail.physical_basis_json)
        assert basis["mold_tool_id"] == str(mold.id)
        assert "4模" in basis["spec"]
        assert lot.estimated_unit_cost_snapshot is not None
        replay = manual_finished_in(session,**args); session.commit()
        assert replay.id == lot.id and replay.quantity_available == 8
    mold.is_active = False; db.flush()
    assert product_stock_specification(product) is None
    mold.is_active = True
    product.sheet_cutting_settings = None
    assert product_stock_specification(product) is None

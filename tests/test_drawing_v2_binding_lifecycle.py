"""New source freezing may bind; repairing historical tasks must not infer a version."""
from datetime import date
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.models.customer import Customer
from app.models.drawing_design import DrawingRelease
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.drawing_binding import bound_task_release
from app.services.production_workflow import create_or_refresh_production_task


@pytest.mark.parametrize("component", [False, True], ids=["ordinary-same-dimensions", "bom-same-product-version"])
def test_new_source_binds_once_but_later_repaired_task_keeps_legacy_source(component):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="生命周期隔离客户")
        db.add(customer)
        db.flush()
        product = Product(customer_id=customer.id, product_code="LIFECYCLE", customer_material_code="LIFECYCLE",
                          product_name="异形内盒", length_mm=620, width_mm=470, height_mm=26, version=1)
        parent = Product(customer_id=customer.id, product_code="LIFECYCLE-P", customer_material_code="LIFECYCLE-P",
                         product_name="组合产品", is_composite=True)
        order = Order(order_number="LIFECYCLE-UAT", customer_id=customer.id, order_date=date(2026, 9, 22))
        db.add_all([product, parent, order])
        db.flush()

        def publish(version, wing):
            release = DrawingRelease(product_id=product.id, customer_id=customer.id, product_version=1,
                design_version=version, revision=f"R{version}", external_number="LIFECYCLE", internal_number="LIFECYCLE",
                idempotency_key=f"lifecycle-{version}", template_key="custom_21301634_v1",
                pdf_reference="unused", pdf_sha256="0" * 64,
                manifest_json=json.dumps({"product_dimensions": {"length_mm": "620", "width_mm": "470", "height_mm": "26"},
                                          "parameters": {"left_wing_mm": str(wing), "right_wing_mm": str(wing)}}))
            db.add(release)
            db.flush()
            return release

        def freeze_source():
            item = OrderItem(order_id=order.id, product_id=parent.id if component else product.id,
                             quantity=10, unit_price=0, subtotal=0, snapshot_product_name=product.product_name,
                             snapshot_spec="620×470×26mm", snapshot_material="B", material_status="pending")
            db.add(item)
            db.flush()
            if component:
                db.add(SalesOrderItemBomComponent(sales_order_item_id=item.id, component_product_id=product.id,
                    component_product_version=1, parent_product_version=1, order_set_quantity=10, quantity_per_set=1,
                    required_piece_quantity=10, display_order=1, internal_component_code="LIFECYCLE-P-S01",
                    is_die_cut=False, spare_sheet_quantity=0, display_mode="internal_only", is_required=True,
                    snapshot_component_product_code=product.product_code, snapshot_component_product_name=product.product_name,
                    snapshot_component_spec=item.snapshot_spec, snapshot_component_box_category="normal"))
                db.flush()
            return item

        first = publish(1, 40)
        new_source = freeze_source()
        original_task = create_or_refresh_production_task(db, new_source.id, source_is_new=True)
        assert bound_task_release(db, original_task.id).id == first.id
        historical_source = freeze_source()  # Existing source whose persistent task is missing.
        db.commit()

        second = publish(2, 35)  # Product.version and L×W×H are unchanged; the engineering segments differ.
        db.commit()
        repaired = create_or_refresh_production_task(db, historical_source.id)
        assert repaired is not None and repaired.status == "waiting_material"
        assert repaired.ordered_quantity_snapshot == 10 and repaired.planned_quantity == 0
        assert bound_task_release(db, repaired.id) is None
        assert bound_task_release(db, original_task.id).id == first.id
        assert product.version == 1

        latest_source = freeze_source()
        latest_task = create_or_refresh_production_task(db, latest_source.id, source_is_new=True)
        assert bound_task_release(db, latest_task.id).id == second.id
        # Re-entering a workflow never replaces a task's immutable initial binding.
        create_or_refresh_production_task(db, new_source.id, source_is_new=True)
        assert bound_task_release(db, original_task.id).id == first.id
    engine.dispose()

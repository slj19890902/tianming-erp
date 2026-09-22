"""Automatic binding must fit frozen order evidence, not just product identity."""
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.drawing_design import DrawingRelease
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionTask
from app.services.drawing_binding import bind_new_task_drawing


@pytest.fixture
def binding_env():
    engine = create_engine("sqlite:///:memory:")
    # Only release rows are needed to exercise the real SQL selection. Context
    # objects stay in memory; there is no checkout or production database.
    DrawingRelease.__table__.create(engine)
    with Session(engine) as query_db:
        def release(identifier, version, length, *, customer=7, template="liner_v1"):
            row = DrawingRelease(id=identifier, product_id=30, customer_id=customer, product_version=version,
                design_version=identifier, revision=f"R{identifier}", external_number="UAT", internal_number="UAT",
                idempotency_key=f"release-{identifier}", template_key=template, pdf_reference="unused",
                pdf_sha256="0"*64, manifest_json=json.dumps({"product_dimensions": {
                    "length_mm": str(length), "width_mm": "50.5", "height_mm": "3"}}))
            query_db.add(row)
            query_db.commit()
            return row
        release(1, 1, "100.25")
        release(2, 2, "200.25")
        release(3, 1, "100.25", customer=8)  # A newer foreign customer row is never eligible.
        order = Order(id=10, customer_id=7)
        item = OrderItem(id=20, order_id=10, product_id=30, snapshot_spec="100.25×50.5×3mm")
        part = SalesOrderItemBomComponent(id=50, sales_order_item_id=20, component_product_id=30,
                                         component_product_version=1, snapshot_component_spec="100.25×50.5×3mm")
        task = ProductionTask(id=40, order_item_id=20, sales_order_item_bom_component_id=None)
        product = Product(id=30, customer_id=7, version=2, length_mm=200.25, width_mm=50.5, height_mm=3)
        records = {(Order, 10): order, (OrderItem, 20): item, (SalesOrderItemBomComponent, 50): part}
        db = Mock(spec=Session)
        db.get.side_effect = lambda model, identifier: records.get((model, identifier))
        db.scalar.side_effect = query_db.scalar
        db.scalars.side_effect = query_db.scalars
        yield db, task, product, item, part, release
    engine.dispose()


def test_bom_uses_frozen_component_version_even_when_current_product_has_newer_drawing(binding_env):
    db, task, product, _, _, _ = binding_env
    task.sales_order_item_bom_component_id = 50
    bind_new_task_drawing(db, task, product, source_is_new=True)
    assert db.add.call_args.args[0].release_id == 1


@pytest.mark.parametrize("snapshot_version", [None, 10])
def test_bom_without_matching_version_keeps_existing_source(binding_env, snapshot_version):
    db, task, product, _, part, _ = binding_env
    task.sales_order_item_bom_component_id = 50
    part.component_product_version = snapshot_version
    bind_new_task_drawing(db, task, product, source_is_new=True)
    db.add.assert_not_called()


@pytest.mark.parametrize("spec", ["100.25×50.5×3mm", " 100.250 X 50.50 * 3 MM ", "100.25*50.5"])
def test_ordinary_order_uses_matching_frozen_dimensions_and_preserves_decimals(binding_env, spec):
    db, task, product, item, _, _ = binding_env
    product.version = 1
    product.length_mm = 100.25
    item.snapshot_spec = spec
    bind_new_task_drawing(db, task, product, source_is_new=True)
    assert db.add.call_args.args[0].release_id == 1


@pytest.mark.parametrize("spec", [None, "UAT", "100.25×50.5×3/5", "100.25×50.5×3 cm", "内盒100.25×50.5×3",
                                    "300×50.5×3", "0×50.5×3", "100.25×50.5×None"])
def test_unknown_or_different_ordinary_spec_is_not_guessed(binding_env, spec):
    db, task, product, item, _, _ = binding_env
    item.snapshot_spec = spec
    bind_new_task_drawing(db, task, product, source_is_new=True)
    db.add.assert_not_called()


def test_two_dimensions_cannot_establish_a_box_height(binding_env):
    db, task, product, item, _, add_release = binding_env
    add_release(4, 3, "300", template="custom_21301634_v1")
    product.version = 3
    item.snapshot_spec = "300×50.5mm"
    bind_new_task_drawing(db, task, product, source_is_new=True)
    db.add.assert_not_called()


def test_new_ordinary_source_does_not_take_old_product_release_when_only_dimensions_match(binding_env):
    db, task, product, item, _, _ = binding_env
    # A new master-data version may change material/thickness/mold without
    # changing the dimensions. No corresponding drawing has been published.
    product.version = 4
    product.length_mm = 100.25
    item.snapshot_spec = "100.25×50.5×3mm"
    bind_new_task_drawing(db, task, product, source_is_new=True)
    db.add.assert_not_called()

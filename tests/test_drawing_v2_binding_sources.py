"""Drawing source precedence and customer boundaries, without a database."""
import json
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.drawing_design import DrawingRelease, ProductionTaskDrawingAdoption
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionTask
from app.services.drawing_binding import bind_new_task_drawing, bound_task_release


def context(*, component=False):
    order = Order(id=10, customer_id=7)
    item = OrderItem(id=20, order_id=10, product_id=30, snapshot_spec="100×50×3mm")
    task = ProductionTask(id=40, order_item_id=20,
                          sales_order_item_bom_component_id=50 if component else None)
    part = SalesOrderItemBomComponent(id=50, sales_order_item_id=20, component_product_id=31,
                                     component_product_version=1)
    product = Product(id=31 if component else 30, customer_id=7, version=1)
    release = DrawingRelease(id=60, product_id=product.id, customer_id=7, product_version=1,
                             template_key="liner_v1", manifest_json=json.dumps({"product_dimensions": {
                                 "length_mm": "100", "width_mm": "50", "height_mm": "3"}}))
    records = {(Order, 10): order, (OrderItem, 20): item,
               (ProductionTask, 40): task, (SalesOrderItemBomComponent, 50): part}
    db = Mock(spec=Session)
    db.get.side_effect = lambda model, identifier: records.get((model, identifier))
    db.scalar.side_effect = lambda statement: (None if statement.column_descriptions[0]["entity"]
                                               is ProductionTaskDrawingAdoption else release)
    db.scalars.return_value.all.return_value = [release]
    return db, task, product, item, part, release


@pytest.mark.parametrize("component", [False, True])
def test_order_only_drawing_keeps_precedence_over_product_release(component):
    db, task, product, item, _, _ = context(component=component)
    item.drawing_file = "private:order_drawings/customer-order-only.pdf"
    bind_new_task_drawing(db, task, product, source_is_new=True)
    db.scalar.assert_not_called()
    db.scalars.assert_not_called()
    db.add.assert_not_called()


def test_frozen_component_source_keeps_precedence_over_product_release():
    db, task, product, _, part, _ = context(component=True)
    part.snapshot_die_cut_path = "private:product_drawings/frozen-component.pdf"
    bind_new_task_drawing(db, task, product, source_is_new=True)
    db.scalar.assert_not_called()
    db.scalars.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.parametrize("component", [False, True])
def test_without_order_exception_binds_the_expected_product_and_customer(component):
    db, task, product, _, _, release = context(component=component)
    bind_new_task_drawing(db, task, product, source_is_new=True)
    bound = db.add.call_args.args[0]
    assert (bound.task_id, bound.release_id) == (task.id, release.id)
    query = (db.scalar if component else db.scalars).call_args.args[0]
    expected = {"product_id_1": product.id, "customer_id_1": 7, "product_version_1": 1}
    if component:
        expected.update(param_1=1)
    assert query.compile().params == expected
    assert "drawing_releases.id DESC" in str(query)


@pytest.mark.parametrize("mismatch", ["order_customer", "product", "component_owner", "missing_component"])
def test_invalid_task_scope_cannot_bind_even_when_an_order_exception_exists(mismatch):
    db, task, product, item, part, _ = context(component=True)
    item.drawing_file = "private:order_drawings/do-not-bypass-scope.pdf"
    if mismatch == "order_customer":
        product.customer_id = 8
    elif mismatch == "product":
        product.id = 999
    elif mismatch == "component_owner":
        part.sales_order_item_id = 999
    else:
        task.sales_order_item_bom_component_id = 999
    with pytest.raises(HTTPException) as failure:
        bind_new_task_drawing(db, task, product, source_is_new=True)
    assert failure.value.status_code == 409
    db.scalar.assert_not_called()
    db.add.assert_not_called()


def test_already_bound_task_retains_its_published_version_when_order_artwork_changes():
    db, task, _, item, _, release = context()
    item.drawing_file = "private:order_drawings/later-artwork.pdf"
    assert bound_task_release(db, task.id) is release
    db.add.assert_not_called()


@pytest.mark.parametrize("mismatch", ["customer", "product", "component_owner"])
def test_reading_existing_binding_still_checks_order_and_component_scope(mismatch):
    db, task, _, _, part, release = context(component=True)
    if mismatch == "customer":
        release.customer_id = 8
    elif mismatch == "product":
        release.product_id = 999
    else:
        part.sales_order_item_id = 999
    with pytest.raises(HTTPException) as failure:
        bound_task_release(db, task.id)
    assert failure.value.status_code == 409


def test_explicit_adoption_can_select_a_different_component_version():
    db, task, _, _, part, release = context(component=True)
    task.version = 2
    part.component_product_version = 1
    release.product_version = 2
    event = ProductionTaskDrawingAdoption(id=1, task_id=task.id, new_release_id=release.id,
        expected_task_version=1, resulting_task_version=2, confirmed_not_issued=1)
    existing_get = db.get.side_effect
    db.get.side_effect = lambda model, identifier: release if model is DrawingRelease else existing_get(model, identifier)
    db.scalar.side_effect = lambda statement: event
    assert bound_task_release(db, task.id) is release

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import importlib
import sys
from types import SimpleNamespace

import pytest

from app.services import (
    floor3_locations,
    incoming_receipts,
    inventory_cost_snapshot,
    production_workflow,
    stock_replenishment,
    warehouse_inventory,
)
from app.services.inventory_cost_snapshot import InventoryCostEstimate


class NullObject(SimpleNamespace):
    def __getattr__(self, _name: str):
        return None


def test_inventory_cost_snapshot_uses_shared_utc_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_time = datetime(2026, 7, 17, 8, 30)
    explicit_time = datetime(2026, 7, 16, 1, 2, 3)
    estimate = InventoryCostEstimate(
        unit_cost=Decimal("1.0000"),
        square_price=Decimal("2.0000"),
        area_m2=Decimal("0.500000"),
        source="test",
        detail={},
    )
    monkeypatch.setattr(
        inventory_cost_snapshot, "utc_now_naive", lambda: default_time
    )

    default_snapshot = NullObject()
    inventory_cost_snapshot.apply_cost_snapshot(default_snapshot, estimate)
    assert default_snapshot.cost_snapshot_at == default_time

    explicit_snapshot = NullObject()
    inventory_cost_snapshot.apply_cost_snapshot(
        explicit_snapshot, estimate, captured_at=explicit_time
    )
    assert explicit_snapshot.cost_snapshot_at == explicit_time


@pytest.mark.parametrize(
    ("received_at", "stock_date"),
    [
        (datetime(2026, 7, 16, 16, 30), date(2026, 7, 17)),
        (datetime(2026, 7, 16, 23, 59), date(2026, 7, 17)),
        (datetime(2026, 7, 17, 0, 0), date(2026, 7, 17)),
    ],
)
def test_incoming_surplus_uses_beijing_date_of_utc_receipt(
    monkeypatch: pytest.MonkeyPatch,
    received_at: datetime,
    stock_date: date,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_manual_semi_finished_in(_db, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        incoming_receipts,
        "manual_semi_finished_in",
        fake_manual_semi_finished_in,
    )
    item = NullObject(
        cardboard_len=1200,
        cardboard_width=800,
        snapshot_material="K=A",
        layer_count=5,
        flute_type="BC",
        snapshot_pieces_per_box=1,
        snapshot_supplier_name="supplier",
        material_id=9,
    )
    target = NullObject(
        order=NullObject(customer_id=7),
        order_item=item,
        requisition_item=None,
        component_type="top",
    )
    receipt_item = NullObject(
        id=42,
        receipt=NullObject(received_at=received_at),
    )

    result = incoming_receipts._create_surplus_lot(
        object(),
        target=target,
        receipt_item=receipt_item,
        surplus=3,
        location_id=5,
        user_id=11,
        reason=None,
    )

    assert result is sentinel
    assert captured["stock_date"] == stock_date


@pytest.mark.parametrize(
    ("module", "factory_name", "prefix"),
    [
        (incoming_receipts, "_number", "IR"),
        (warehouse_inventory, "_number", "FG"),
    ],
)
def test_business_numbers_use_beijing_calendar_date(
    monkeypatch: pytest.MonkeyPatch,
    module,
    factory_name: str,
    prefix: str,
) -> None:
    monkeypatch.setattr(
        module,
        "beijing_now_naive",
        lambda: datetime(2026, 7, 18, 0, 30),
    )

    value = getattr(module, factory_name)(prefix)

    assert value.startswith(f"{prefix}-20260718-")


def test_floor3_and_replenishment_numbers_use_beijing_calendar_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 18, 0, 30)
    monkeypatch.setattr(floor3_locations, "beijing_now_naive", lambda: now)
    monkeypatch.setattr(stock_replenishment, "beijing_now_naive", lambda: now)

    assert floor3_locations._generated_pallet_code().startswith(
        "PLT-3F-20260718-"
    )
    assert stock_replenishment.next_replenishment_order_number().startswith(
        "SR-20260718-"
    )


def test_tianhua_manual_utc_datetime_serialization_uses_z(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "app.services.tianhua_pre_delivery"
    services_package = importlib.import_module("app.services")
    marker = object()
    previous_module = sys.modules.get(module_name, marker)
    previous_attribute = getattr(services_package, "tianhua_pre_delivery", marker)
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    sys.modules.pop(module_name, None)
    if previous_attribute is not marker:
        delattr(services_package, "tianhua_pre_delivery")
    tianhua_pre_delivery = importlib.import_module(module_name)
    import_item = NullObject(id=1, status="ok", warning=None, selected=True)
    draft_item = NullObject(
        mobile_pick_status="picked",
        mobile_picked_at=datetime(2026, 7, 17, 16, 0),
    )

    payload = tianhua_pre_delivery.item_dict(import_item, draft_item)

    assert payload["mobile_picked_at"] == "2026-07-17T16:00:00Z"

    sys.modules.pop(module_name, None)
    if previous_module is not marker:
        sys.modules[module_name] = previous_module
    if previous_attribute is marker:
        if hasattr(services_package, "tianhua_pre_delivery"):
            delattr(services_package, "tianhua_pre_delivery")
    else:
        setattr(services_package, "tianhua_pre_delivery", previous_attribute)


def test_incoming_receipt_datetime_is_explicit_and_nullable() -> None:
    receipt = NullObject(
        receipt_number="IR-1",
        status="posted",
        received_at=datetime(2026, 7, 17, 0, 1, 2),
        received_by=1,
    )
    row = NullObject(
        id=2,
        receipt_id=1,
        receipt=receipt,
        planned_quantity=10,
        cumulative_received_quantity=4,
    )

    assert incoming_receipts.receipt_item_dict(row)["received_at"] == (
        "2026-07-17T00:01:02Z"
    )
    receipt.received_at = None
    assert incoming_receipts.receipt_item_dict(row)["received_at"] is None


def test_stock_replenishment_datetimes_are_explicit_and_nullable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2026, 7, 17, 0, 1, 2)
    item = NullObject(
        id=2,
        quantity=10,
        stocked_quantity=4,
        location=None,
        inventory_lot=None,
        historical_row=None,
        stocked_at=timestamp,
    )
    order = NullObject(
        id=1,
        customer=None,
        items=[item],
        created_at=timestamp,
        confirmed_at=None,
        stocked_at=timestamp,
    )
    policy = NullObject(
        target_quantity=10,
        warning_quantity=5,
        default_location=None,
        customer=None,
        product=None,
        active=True,
        updated_at=timestamp,
    )
    monkeypatch.setattr(stock_replenishment, "current_policy_quantity", lambda *_args: 0)

    item_payload = stock_replenishment.replenishment_item_dict(item)
    order_payload = stock_replenishment.replenishment_order_dict(order)
    policy_payload = stock_replenishment.stock_policy_dict(object(), policy)

    assert item_payload["stocked_at"] == "2026-07-17T00:01:02Z"
    assert order_payload["created_at"] == "2026-07-17T00:01:02Z"
    assert order_payload["confirmed_at"] is None
    assert order_payload["stocked_at"] == "2026-07-17T00:01:02Z"
    assert policy_payload["updated_at"] == "2026-07-17T00:01:02Z"
    item.stocked_at = None
    policy.updated_at = None
    assert stock_replenishment.replenishment_item_dict(item)["stocked_at"] is None
    assert stock_replenishment.stock_policy_dict(object(), policy)["updated_at"] is None


def test_production_response_datetimes_are_explicit_and_nullable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Query:
        def where(self, *_args):
            return self

        def order_by(self, *_args):
            return self

    class Rows:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    timestamp = datetime(2026, 7, 17, 0, 1, 2)
    task = NullObject(
        id=1,
        status="ready",
        planned_quantity=10,
        finished_coverage_snapshot=0,
        readiness_basis="material_ready",
        ready_at=timestamp,
        version=1,
    )
    item = NullObject(
        id=2,
        product_id=3,
        quantity=10,
        delivered_quantity=0,
        material_status="received",
        is_force_closed=False,
    )
    order = NullObject(
        id=4,
        order_number="SO-1",
        customer_id=5,
        status="in_production",
    )
    customer = NullObject(name="customer")
    product = NullObject(
        box_category=None,
        product_code="P-1",
        product_name="product",
        version=1,
        production_label_enabled=False,
        production_label_units_per_label=None,
    )
    task_rows = [(task, item, order, customer, product)]
    db = NullObject(
        execute=lambda _query: Rows(task_rows),
        scalars=lambda _query: Rows([]),
        scalar=lambda _query: 0,
        get=lambda _model, _key: None,
    )
    monkeypatch.setattr(production_workflow, "_task_query", lambda *_args: Query())
    monkeypatch.setattr(
        production_workflow,
        "production_ready_quantity",
        lambda *_args: 10,
    )
    monkeypatch.setattr(
        production_workflow,
        "_active_customer_board_preparation_sources",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        production_workflow,
        "_receipt_purpose_summaries_by_order_item_ids",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        production_workflow,
        "preferred_area_summaries_by_customer_ids",
        lambda *_args, **_kwargs: {},
    )

    task_payload = production_workflow.list_production_tasks(
        db,
        allowed_customer_ids=None,
    )[0]
    assert task_payload["ready_at"] == "2026-07-17T00:01:02Z"
    task.ready_at = None
    assert production_workflow.list_production_tasks(
        db,
        allowed_customer_ids=None,
    )[0]["ready_at"] is None

    completion = NullObject(
        id=6,
        batch_id=7,
        quantity=10,
        completion_type="normal",
        material_input_quantity=10,
        planned_output_quantity=10,
        actual_output_quantity=10,
        defective_quantity=0,
        order_reserved_quantity=10,
        direct_delivery_quantity=0,
        stock_quantity=10,
        surplus_finished_quantity=0,
        status="posted",
        initial_disposition="stock",
        warehouse_location_id=None,
        inventory_lot_id=None,
        remarks=None,
        completed_by=None,
        completed_at=timestamp,
    )
    completion_rows = [
        (completion, task, item, order, customer, product, None, None)
    ]
    monkeypatch.setattr(
        production_workflow,
        "_completion_rows",
        lambda *_args, **_kwargs: completion_rows,
    )
    monkeypatch.setattr(
        production_workflow,
        "_dispatched_delivery_order_item_ids",
        lambda *_args: set(),
    )

    completion_payload = production_workflow.list_production_completions(
        db,
        allowed_customer_ids=None,
    )[0]
    assert completion_payload["completed_at"] == "2026-07-17T00:01:02Z"
    completion.completed_at = None
    assert production_workflow.list_production_completions(
        db,
        allowed_customer_ids=None,
    )[0]["completed_at"] is None

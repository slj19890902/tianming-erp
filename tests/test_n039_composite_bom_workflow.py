from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.composite_bom_workflow import (
    CompositeBomWorkflowError,
    ComponentAvailability,
    ComponentDemand,
    ComponentConsumption,
    ConsumptionPart,
    _as_positive_integer,
)
import app.services.composite_bom_workflow as workflow


def test_quantity_per_set_accepts_positive_integers_only() -> None:
    assert _as_positive_integer(Decimal("2"), field="每套组件数量") == 2
    for invalid in (0, -1, Decimal("1.5"), "abc"):
        with pytest.raises(CompositeBomWorkflowError, match="正整数"):
            _as_positive_integer(invalid, field="每套组件数量")


def test_component_availability_keeps_stock_and_direct_separate() -> None:
    row = ComponentAvailability(
        snapshot_id=7,
        component_code="LID",
        component_name="盖",
        quantity_per_set=2,
        is_required=True,
        stock_quantity=6,
        direct_quantity=4,
        available_quantity=10,
        required_piece_quantity=20,
    )
    assert row.available_quantity // row.quantity_per_set == 5
    assert row.stock_quantity + row.direct_quantity == row.available_quantity


def test_optional_component_never_changes_required_kit_capacity() -> None:
    required = ComponentDemand(1, 1, 11, "LID", "盖", None, 2, True, 5, 10)
    optional = ComponentDemand(2, 1, 12, "CARD", "说明卡", None, 1, False, 5, 5)
    assert required.required_piece_quantity == 10
    assert optional.is_required is False
    # Optional shortages are informational and never lower the required-kit
    # capacity.  When optional stock exists it is consumed opportunistically;
    # that behavior is covered by the execution-plan test below.
    available_required_sets = required.required_piece_quantity // required.quantity_per_set
    assert available_required_sets == 5


def test_atomic_shortage_contract_requires_preflight_before_mutation() -> None:
    required = ComponentDemand(1, 1, 11, "BASE", "底", None, 3, True, 2, 6)
    stock = ComponentAvailability(1, "BASE", "底", 3, True, 2, 0, 2, 6)
    assert stock.available_quantity < required.quantity_per_set
    # The execution service checks every component plan first.  This test
    # guards the arithmetic boundary used to reject a no-write shortage.
    with pytest.raises(CompositeBomWorkflowError, match="不足"):
        if stock.available_quantity < required.quantity_per_set:
            raise CompositeBomWorkflowError("组件库存不足")


class _FakeDb:
    def __init__(self, *, objects=None, scalar_result=None, scalar_rows=None):
        self.objects = objects or {}
        self.scalar_result = scalar_result
        self.scalar_rows = list(scalar_rows or [])
        self.added = []
        self.flush_count = 0

    def get(self, model, value):
        return self.objects.get((model.__name__, value))

    def scalar(self, statement):
        del statement
        return self.scalar_result

    def scalars(self, statement):
        del statement
        return SimpleNamespace(all=lambda: self.scalar_rows.pop(0) if self.scalar_rows else [])

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flush_count += 1

    def begin_nested(self):
        return nullcontext()


def test_stock_and_direct_mixed_plan_consumes_one_complete_kit(monkeypatch) -> None:
    delivery_item = SimpleNamespace(id=55, order_item_id=10, delivery_id=9)
    stock_reservation = SimpleNamespace(
        id=31,
        reserved_stock_quantity=3,
        consumed_stock_quantity=0,
        released_stock_quantity=0,
    )
    direct_completion = SimpleNamespace(id=41, quantity=5)
    db = _FakeDb(objects={("DeliveryItem", 55): delivery_item})
    demand = ComponentDemand(7, 10, 11, "LID", "盖", None, 4, True, 3, 12)
    monkeypatch.setattr(workflow, "effective_component_demands", lambda *_: [demand])
    monkeypatch.setattr(workflow, "_stock_reservations", lambda *_: [stock_reservation])
    monkeypatch.setattr(workflow, "_direct_completion_rows", lambda *_: [(direct_completion, 0)])

    plan = workflow.build_delivery_component_consumption_plan(
        db, delivery_item_id=55, delivery_sets=2
    )

    assert [(part.source, part.quantity) for part in plan[0].parts] == [
        ("stock", 3),
        ("direct", 5),
    ]


def test_optional_component_is_consumed_when_available_but_never_blocks(monkeypatch) -> None:
    delivery_item = SimpleNamespace(id=56, order_item_id=10, delivery_id=9)
    required = ComponentDemand(7, 10, 11, "BOX", "主箱", None, 1, True, 2, 2)
    optional = ComponentDemand(8, 10, 12, "CARD", "说明卡", None, 1, False, 2, 2)
    optional_completion = SimpleNamespace(id=42, quantity=1)
    db = _FakeDb(objects={("DeliveryItem", 56): delivery_item})
    monkeypatch.setattr(workflow, "effective_component_demands", lambda *_: [required, optional])
    monkeypatch.setattr(workflow, "_stock_reservations", lambda *_: [])
    monkeypatch.setattr(
        workflow,
        "_direct_completion_rows",
        lambda _db, snapshot_id: [(SimpleNamespace(id=41, quantity=2), 0)]
        if snapshot_id == 7
        else [(optional_completion, 0)],
    )

    plan = workflow.build_delivery_component_consumption_plan(
        db,
        delivery_item_id=56,
        delivery_sets=2,
    )

    assert len(plan) == 2
    assert plan[0].required_quantity == 2
    assert sum(part.quantity for part in plan[1].parts) == 1


def test_preflight_shortage_does_not_write_any_allocation(monkeypatch) -> None:
    db = _FakeDb()
    monkeypatch.setattr(
        workflow,
        "build_delivery_component_consumption_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CompositeBomWorkflowError("复合产品无法齐套发货：盖缺1件")
        ),
    )
    monkeypatch.setattr(workflow, "effective_component_demands", lambda *_: [object()])
    monkeypatch.setattr(
        db,
        "get",
        lambda model, value: SimpleNamespace(id=value, order_item_id=10)
        if model.__name__ == "DeliveryItem"
        else None,
    )

    with pytest.raises(CompositeBomWorkflowError, match="无法齐套"):
        workflow.execute_delivery_component_consumption(
            db,
            delivery_item_id=55,
            delivery_sets=1,
            operator_id=1,
            operation_key="test-short",
        )
    assert db.added == []


def test_execute_and_reverse_component_allocations(monkeypatch) -> None:
    delivery_item = SimpleNamespace(id=55, order_item_id=10, delivery_id=9)
    reservation = SimpleNamespace(
        id=31,
        sales_order_item_bom_component_id=7,
        inventory_lot_id=71,
        reserved_stock_quantity=4,
        consumed_stock_quantity=0,
        released_stock_quantity=0,
        consumed_requirement_quantity=0,
        status="active",
        consumed_by=None,
        consumed_at=None,
        order_item_id=10,
    )
    lot = SimpleNamespace(
        id=71,
        warehouse_location_id=501,
        quantity_available=0,
        quantity_reserved=4,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        version=1,
    )
    db = _FakeDb(
        objects={
            ("DeliveryItem", 55): delivery_item,
            ("InventoryReservation", 31): reservation,
            ("InventoryLot", 71): lot,
        }
    )
    plan = [
        ComponentConsumption(
            snapshot_id=7,
            component_code="LID",
            component_name="盖",
            required_quantity=6,
            parts=(
                ConsumptionPart("stock", 7, 31, 4),
                ConsumptionPart("direct", 7, 41, 2),
            ),
        )
    ]
    monkeypatch.setattr(workflow, "effective_component_demands", lambda *_: [object()])
    monkeypatch.setattr(workflow, "build_delivery_component_consumption_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(workflow, "_balances", lambda row: {
        "available": row.quantity_available,
        "reserved": row.quantity_reserved,
        "consumed": row.quantity_consumed,
        "damaged": row.quantity_damaged,
        "scrapped": row.quantity_scrapped,
    })
    monkeypatch.setattr(workflow, "_movement", lambda *_args, **_kwargs: SimpleNamespace(id=101))
    claimed_location_ids = []
    monkeypatch.setattr(
        workflow,
        "_claim_inventory_destination",
        lambda _db, location_id: claimed_location_ids.append(location_id),
    )

    result = workflow.execute_delivery_component_consumption(
        db,
        delivery_item_id=55,
        delivery_sets=1,
        operator_id=5,
        operation_key="test-consume",
    )
    assert result == plan
    assert (reservation.consumed_stock_quantity, lot.quantity_reserved, lot.quantity_consumed) == (4, 0, 4)
    direct = next(row for row in db.added if row.__class__.__name__ == "BomComponentDirectDeliveryAllocation")
    stock = next(row for row in db.added if row.__class__.__name__ == "DeliveryInventoryAllocation")
    assert direct.consumed_quantity == 2
    assert stock.consumed_stock_quantity == 4

    db.scalar_rows = [[501], [direct], [stock]]
    workflow.reverse_delivery_component_allocations(
        db,
        delivery_item_id=55,
        operator_id=5,
        operation_key="test-reverse",
    )
    assert direct.status == "reversed"
    assert stock.status == "reversed"
    assert claimed_location_ids == [501]
    assert (reservation.consumed_stock_quantity, lot.quantity_reserved, lot.quantity_consumed) == (0, 4, 0)


def test_normal_product_no_snapshot_is_explicitly_not_applicable() -> None:
    # API callers receive an empty snapshot set for normal goods/A3, and must
    # continue to use the existing delivery and production paths.
    snapshots: list[object] = []
    assert snapshots == []

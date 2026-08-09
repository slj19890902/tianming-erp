from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.customer import Customer
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseBatch,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingReceipt,
    ExternalPackagingReceiptItem,
)
from app.models.order import Order, OrderItem
from app.models.order_external_packaging import (
    SalesOrderItemExternalComponent,
    SalesOrderItemExternalComponentCandidate,
)
from app.services import external_packaging_receiving as receiving
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from test_p1_33c5_external_packaging_receiving import _confirm, _login


__all__ = ["purchase_app"]


def _confirmed_seed(app: FastAPI) -> None:
    with TestClient(app) as client:
        _login(client, "purchase-admin")
        _confirm(client, app.state.fixture["order_id"])


def _load_purchases(db: Session) -> list[ExternalPackagingPurchaseOrder]:
    return list(
        db.scalars(
            select(ExternalPackagingPurchaseOrder)
            .options(
                joinedload(ExternalPackagingPurchaseOrder.batch),
                selectinload(ExternalPackagingPurchaseOrder.items),
            )
            .order_by(ExternalPackagingPurchaseOrder.id)
        )
        .unique()
        .all()
    )


def _copied_columns(row: Any, *excluded: str) -> dict[str, Any]:
    omitted = {"id", *excluded}
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
        if column.name not in omitted
    }


def _receipt(
    db: Session,
    purchase: ExternalPackagingPurchaseOrder,
    quantities: list[Decimal],
    *,
    suffix: str,
) -> None:
    nonzero = [
        (item, quantity)
        for item, quantity in zip(purchase.items, quantities, strict=True)
        if quantity > 0
    ]
    if not nonzero:
        return
    receipt = ExternalPackagingReceipt(
        purchase_order_id=purchase.id,
        receipt_number=f"ER-P1-36M-{suffix}",
        idempotency_key=f"p1-36m-receipt-{suffix}",
        request_fingerprint=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        received_by=purchase.confirmed_by,
    )
    db.add(receipt)
    db.flush()
    db.add_all(
        [
            ExternalPackagingReceiptItem(
                receipt_id=receipt.id,
                purchase_item_id=item.id,
                received_quantity=quantity,
                purchase_unit_snapshot=item.purchase_unit,
            )
            for item, quantity in nonzero
        ]
    )
    db.flush()


def _clone_purchase(
    db: Session,
    seed_purchase: ExternalPackagingPurchaseOrder,
    *,
    suffix: str,
    quantities: list[Decimal],
    received: list[Decimal],
    customer_id: int | None = None,
) -> ExternalPackagingPurchaseOrder:
    if len(quantities) != len(received):
        raise AssertionError("quantity and receipt fixtures must have equal lengths")
    if len(quantities) > len(seed_purchase.items):
        raise AssertionError("seed purchase does not have enough distinct source lines")

    seed_order = db.get(Order, seed_purchase.batch.sales_order_id)
    assert seed_order is not None
    order = Order(
        order_number=f"TM-P1-36M-{suffix}",
        customer_id=customer_id or seed_order.customer_id,
        customer_po=f"PO-P1-36M-{suffix}",
        order_date=date(2026, 8, 10),
        total_amount=Decimal("1"),
        created_by=seed_order.created_by,
    )
    db.add(order)
    db.flush()

    batch = ExternalPackagingPurchaseBatch(
        sales_order_id=order.id,
        idempotency_key=f"p1-36m-batch-{suffix}",
        request_fingerprint=hashlib.sha256(
            f"batch-{suffix}".encode("utf-8")
        ).hexdigest(),
        confirmed_by=seed_purchase.confirmed_by,
    )
    db.add(batch)
    db.flush()
    purchase_values = _copied_columns(
        seed_purchase,
        "batch_id",
        "purchase_number",
        "confirmed_at",
    )
    purchase_values.update(
        batch_id=batch.id,
        purchase_number=f"EP-P1-36M-{suffix}",
    )
    purchase = ExternalPackagingPurchaseOrder(**purchase_values)
    db.add(purchase)
    db.flush()

    created_items: list[ExternalPackagingPurchaseItem] = []
    for index, (seed_item, quantity) in enumerate(
        zip(seed_purchase.items, quantities, strict=False), start=1
    ):
        seed_order_item = db.get(OrderItem, seed_item.sales_order_item_id)
        seed_component = db.get(
            SalesOrderItemExternalComponent, seed_item.order_component_id
        )
        seed_candidate = db.get(
            SalesOrderItemExternalComponentCandidate,
            seed_item.order_candidate_id,
        )
        assert seed_order_item is not None
        assert seed_component is not None
        assert seed_candidate is not None

        order_item = OrderItem(
            order_id=order.id,
            product_id=seed_order_item.product_id,
            item_order_number=f"TM-P1-36M-{suffix}-{index:03d}",
            item_sequence=index,
            quantity=1,
            unit_price=Decimal("1"),
            subtotal=Decimal("1"),
            snapshot_product_name=seed_order_item.snapshot_product_name,
            snapshot_product_code=seed_order_item.snapshot_product_code,
        )
        db.add(order_item)
        db.flush()

        component_values = _copied_columns(
            seed_component,
            "sales_order_item_id",
            "created_at",
        )
        component_values["sales_order_item_id"] = order_item.id
        component = SalesOrderItemExternalComponent(**component_values)
        db.add(component)
        db.flush()

        candidate_values = _copied_columns(seed_candidate, "order_component_id")
        candidate_values["order_component_id"] = component.id
        candidate = SalesOrderItemExternalComponentCandidate(**candidate_values)
        db.add(candidate)
        db.flush()

        item_values = _copied_columns(
            seed_item,
            "purchase_order_id",
            "sales_order_id",
            "sales_order_item_id",
            "order_component_id",
            "order_candidate_id",
            "purchase_quantity",
            "confirmed_at",
        )
        item_values.update(
            purchase_order_id=purchase.id,
            sales_order_id=order.id,
            sales_order_item_id=order_item.id,
            order_component_id=component.id,
            order_candidate_id=candidate.id,
            purchase_quantity=quantity,
        )
        purchase_item = ExternalPackagingPurchaseItem(**item_values)
        db.add(purchase_item)
        db.flush()
        created_items.append(purchase_item)

    purchase.items = created_items
    _receipt(db, purchase, received, suffix=suffix)
    return purchase


def _clone_empty_purchase(
    db: Session,
    seed_purchase: ExternalPackagingPurchaseOrder,
    *,
    suffix: str,
) -> ExternalPackagingPurchaseOrder:
    return _clone_purchase(
        db,
        seed_purchase,
        suffix=suffix,
        quantities=[],
        received=[],
    )


def _legacy_overview(
    db: Session,
    *,
    visible_customer_ids: set[int] | None,
    include_completed: bool = False,
) -> dict[str, Any]:
    query = (
        select(ExternalPackagingPurchaseOrder)
        .join(
            ExternalPackagingPurchaseBatch,
            ExternalPackagingPurchaseBatch.id
            == ExternalPackagingPurchaseOrder.batch_id,
        )
        .join(Order, Order.id == ExternalPackagingPurchaseBatch.sales_order_id)
        .options(
            joinedload(ExternalPackagingPurchaseOrder.batch),
            selectinload(ExternalPackagingPurchaseOrder.items),
        )
        .order_by(
            ExternalPackagingPurchaseOrder.confirmed_at,
            ExternalPackagingPurchaseOrder.id,
        )
    )
    if visible_customer_ids is not None:
        query = query.where(Order.customer_id.in_(visible_customer_ids))
    purchases = list(db.scalars(query).unique().all())
    order_ids = {purchase.batch.sales_order_id for purchase in purchases}
    sales_orders = {
        row.id: row
        for row in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    }
    customer_ids = {row.customer_id for row in sales_orders.values()}
    customers = {
        row.id: row
        for row in db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
    }
    item_ids = {item.id for purchase in purchases for item in purchase.items}
    totals = receiving._received_totals(db, item_ids)
    rows = []
    for purchase in purchases:
        sales_order = sales_orders.get(purchase.batch.sales_order_id)
        if sales_order is None:
            continue
        payload = receiving._purchase_payload(
            purchase,
            sales_order=sales_order,
            customer=customers.get(sales_order.customer_id),
            totals=totals,
        )
        if not include_completed:
            payload["items"] = [
                item
                for item in payload["items"]
                if Decimal(item["remaining_quantity"]) > 0
            ]
        if include_completed or payload["status"] != "received":
            rows.append(payload)
    return {
        "purchase_orders": rows,
        "summary": {
            "purchase_order_count": len(rows),
            "pending_line_count": sum(
                1
                for row in rows
                for item in row["items"]
                if Decimal(item["remaining_quantity"]) > 0
            ),
        },
    }


def _overview(factory, scope, *, include_completed: bool = False) -> dict[str, Any]:
    with factory() as db:
        return receiving.build_external_receiving_overview(
            db,
            visible_customer_ids=scope,
            include_completed=include_completed,
        )


def _legacy(factory, scope, *, include_completed: bool = False) -> dict[str, Any]:
    with factory() as db:
        return _legacy_overview(
            db,
            visible_customer_ids=scope,
            include_completed=include_completed,
        )


def test_prefilter_is_deeply_equal_for_all_receiving_states_and_scope(
    purchase_app: FastAPI,
) -> None:
    _confirmed_seed(purchase_app)
    factory = purchase_app.state.session_factory
    with factory() as db:
        one_line = next(row for row in _load_purchases(db) if len(row.items) == 1)
        two_line = next(row for row in _load_purchases(db) if len(row.items) == 2)
        customer_id = db.get(Order, one_line.batch.sales_order_id).customer_id
        other_customer = Customer(
            name="匿名范围外客户",
            customer_code="P1-36M-OTHER",
        )
        db.add(other_customer)
        db.flush()

        partial = _clone_purchase(
            db,
            one_line,
            suffix="PARTIAL",
            quantities=[Decimal("10")],
            received=[Decimal("4")],
        )
        mixed = _clone_purchase(
            db,
            two_line,
            suffix="MIXED",
            quantities=[Decimal("10"), Decimal("20")],
            received=[Decimal("10"), Decimal("5")],
        )
        completed = _clone_purchase(
            db,
            one_line,
            suffix="COMPLETE",
            quantities=[Decimal("7")],
            received=[Decimal("7")],
        )
        fractional_complete = _clone_purchase(
            db,
            one_line,
            suffix="FRACTIONAL",
            quantities=[Decimal("0.9")],
            received=[Decimal("0")],
        )
        for index in range(1, 4):
            _receipt(
                db,
                fractional_complete,
                [Decimal("0.3")],
                suffix=f"FRACTIONAL-{index}",
            )
        micro_complete = _clone_purchase(
            db,
            one_line,
            suffix="MICRO-COMPLETE",
            quantities=[Decimal("0.000001")],
            received=[Decimal("0.000001")],
        )
        micro_partial = _clone_purchase(
            db,
            one_line,
            suffix="MICRO-PARTIAL",
            quantities=[Decimal("0.000002")],
            received=[Decimal("0.000001")],
        )
        empty = _clone_empty_purchase(db, one_line, suffix="EMPTY")
        hidden = _clone_purchase(
            db,
            one_line,
            suffix="OTHER",
            quantities=[Decimal("6")],
            received=[Decimal("0")],
            customer_id=other_customer.id,
        )
        db.commit()
        expected = {
            "partial": partial.id,
            "mixed": mixed.id,
            "completed": completed.id,
            "fractional_complete": fractional_complete.id,
            "micro_complete": micro_complete.id,
            "micro_partial": micro_partial.id,
            "empty": empty.id,
            "hidden": hidden.id,
        }

    scope = {customer_id}
    pending = _overview(factory, scope)
    assert pending == _legacy(factory, scope)
    pending_ids = {row["id"] for row in pending["purchase_orders"]}
    assert expected["partial"] in pending_ids
    assert expected["mixed"] in pending_ids
    assert expected["empty"] in pending_ids
    assert expected["micro_partial"] in pending_ids
    assert expected["completed"] not in pending_ids
    assert expected["fractional_complete"] not in pending_ids
    assert expected["micro_complete"] not in pending_ids
    assert expected["hidden"] not in pending_ids

    partial_row = next(
        row for row in pending["purchase_orders"] if row["id"] == expected["partial"]
    )
    assert partial_row["status"] == "partially_received"
    assert partial_row["items"][0]["remaining_quantity"] == "6"
    mixed_row = next(
        row for row in pending["purchase_orders"] if row["id"] == expected["mixed"]
    )
    assert mixed_row["status"] == "partially_received"
    assert len(mixed_row["items"]) == 1
    assert mixed_row["items"][0]["remaining_quantity"] == "15"
    empty_row = next(
        row for row in pending["purchase_orders"] if row["id"] == expected["empty"]
    )
    assert empty_row["status"] == "pending_receipt"
    assert empty_row["items"] == []
    micro_partial_row = next(
        row
        for row in pending["purchase_orders"]
        if row["id"] == expected["micro_partial"]
    )
    assert micro_partial_row["status"] == "partially_received"
    assert micro_partial_row["items"][0]["received_quantity"] == "0.000001"
    assert micro_partial_row["items"][0]["remaining_quantity"] == "0.000001"

    completed_history = _overview(factory, scope, include_completed=True)
    assert completed_history == _legacy(factory, scope, include_completed=True)
    completed_ids = {row["id"] for row in completed_history["purchase_orders"]}
    assert expected["completed"] in completed_ids
    assert expected["fractional_complete"] in completed_ids
    assert expected["micro_complete"] in completed_ids
    assert expected["hidden"] not in completed_ids

    fractional_row = next(
        row
        for row in completed_history["purchase_orders"]
        if row["id"] == expected["fractional_complete"]
    )
    assert fractional_row["status"] == "received"
    assert fractional_row["items"][0]["received_quantity"] == "0.9"
    assert fractional_row["items"][0]["remaining_quantity"] == "0"

    unrestricted = _overview(factory, None)
    assert unrestricted == _legacy(factory, None)
    assert expected["hidden"] in {
        row["id"] for row in unrestricted["purchase_orders"]
    }


def _measure(factory, customer_id: int) -> dict[str, Any]:
    engine = factory.kw["bind"]
    statements: list[str] = []
    loaded_purchase_ids: set[int] = set()
    loaded_item_ids: set[int] = set()

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with factory() as db:
            def record_object(_session, instance):
                if isinstance(instance, ExternalPackagingPurchaseOrder):
                    loaded_purchase_ids.add(instance.id)
                elif isinstance(instance, ExternalPackagingPurchaseItem):
                    loaded_item_ids.add(instance.id)

            event.listen(db, "loaded_as_persistent", record_object)
            try:
                payload = receiving.build_external_receiving_overview(
                    db,
                    visible_customer_ids={customer_id},
                )
            finally:
                event.remove(db, "loaded_as_persistent", record_object)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    verbs = [statement.split(None, 1)[0] for statement in statements if statement]
    return {
        "payload": payload,
        "bytes": len(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "selects": sum(verb == "select" for verb in verbs),
        "dml": [verb for verb in verbs if verb in {"insert", "update", "delete"}],
        "purchase_ids": loaded_purchase_ids,
        "item_ids": loaded_item_ids,
    }


def _add_completed_histories(
    factory,
    seed_purchase_id: int,
    *,
    start: int,
    count: int,
) -> None:
    with factory() as db:
        seed = db.scalar(
            select(ExternalPackagingPurchaseOrder)
            .options(
                joinedload(ExternalPackagingPurchaseOrder.batch),
                selectinload(ExternalPackagingPurchaseOrder.items),
            )
            .where(ExternalPackagingPurchaseOrder.id == seed_purchase_id)
        )
        assert seed is not None
        for offset in range(start, start + count):
            _clone_purchase(
                db,
                seed,
                suffix=f"H{offset:03d}",
                quantities=[Decimal("5")],
                received=[Decimal("5")],
            )
        db.commit()


def test_formal_six_place_precision_filters_only_exactly_completed_rows(
    purchase_app: FastAPI,
) -> None:
    _confirmed_seed(purchase_app)
    factory = purchase_app.state.session_factory
    with factory() as db:
        purchases = _load_purchases(db)
        seed = next(row for row in purchases if len(row.items) == 1)
        removed = next(row for row in purchases if len(row.items) == 2)
        customer_id = db.get(Order, seed.batch.sales_order_id).customer_id

        fractional_complete = _clone_purchase(
            db,
            seed,
            suffix="MEASURE-FRACTIONAL",
            quantities=[Decimal("0.9")],
            received=[Decimal("0")],
        )
        for index in range(1, 4):
            _receipt(
                db,
                fractional_complete,
                [Decimal("0.3")],
                suffix=f"MEASURE-FRACTIONAL-{index}",
            )
        micro_complete = _clone_purchase(
            db,
            seed,
            suffix="MEASURE-MICRO-COMPLETE",
            quantities=[Decimal("0.000001")],
            received=[Decimal("0.000001")],
        )
        micro_partial = _clone_purchase(
            db,
            seed,
            suffix="MEASURE-MICRO-PARTIAL",
            quantities=[Decimal("0.000002")],
            received=[Decimal("0.000001")],
        )
        db.delete(removed)
        db.commit()

        completed_purchase_ids = {
            fractional_complete.id,
            micro_complete.id,
        }
        completed_item_ids = {
            fractional_complete.items[0].id,
            micro_complete.items[0].id,
        }
        micro_partial_id = micro_partial.id

    measured = _measure(factory, customer_id)
    payload_by_id = {
        row["id"]: row for row in measured["payload"]["purchase_orders"]
    }
    assert completed_purchase_ids.isdisjoint(measured["purchase_ids"])
    assert completed_item_ids.isdisjoint(measured["item_ids"])
    assert completed_purchase_ids.isdisjoint(payload_by_id)
    assert micro_partial_id in measured["purchase_ids"]
    assert payload_by_id[micro_partial_id]["status"] == "partially_received"
    assert payload_by_id[micro_partial_id]["items"][0]["remaining_quantity"] == "0.000001"
    assert measured["dml"] == []


def test_completed_history_does_not_grow_queries_objects_or_response(
    purchase_app: FastAPI,
) -> None:
    _confirmed_seed(purchase_app)
    factory = purchase_app.state.session_factory
    with factory() as db:
        purchases = _load_purchases(db)
        pending = next(row for row in purchases if len(row.items) == 1)
        removed = next(row for row in purchases if len(row.items) == 2)
        customer_id = db.get(Order, pending.batch.sales_order_id).customer_id
        pending_purchase_id = pending.id
        pending_item_ids = {row.id for row in pending.items}
        db.delete(removed)
        db.commit()

    measurements = {0: _measure(factory, customer_id)}
    _add_completed_histories(
        factory,
        pending_purchase_id,
        start=1,
        count=20,
    )
    measurements[20] = _measure(factory, customer_id)
    _add_completed_histories(
        factory,
        pending_purchase_id,
        start=21,
        count=80,
    )
    measurements[100] = _measure(factory, customer_id)

    baseline = measurements[0]
    for history_count, measured in measurements.items():
        assert measured["payload"] == baseline["payload"], history_count
        assert measured["bytes"] == baseline["bytes"], history_count
        assert measured["selects"] == baseline["selects"], history_count
        assert measured["dml"] == [], history_count
        assert measured["purchase_ids"] == {pending_purchase_id}, history_count
        assert measured["item_ids"] == pending_item_ids, history_count
    assert len(baseline["payload"]["purchase_orders"]) == 1
    assert baseline["bytes"] < 2_000
    assert baseline["selects"] <= 6

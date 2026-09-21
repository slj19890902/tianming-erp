"""P04: isolated representative list-scale measurements.

This fixture writes synthetic historical rows directly so that a 100/1000/5000
row list can be measured without creating business facts.  It is deliberately
separate from the order-flow tests: it demonstrates read-path scaling only,
not that those records could have been created by the normal order API.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sqlite3
from statistics import median
from threading import Event, Lock, RLock, Thread
from time import monotonic, sleep

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

import app.api.orders as orders_api
from app.api.deps import get_db
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryReservation,
    WarehouseLocation,
)
from app.services.requisition_quantities import DEFAULT_CUTTING_MODE


TIERS = (100, 1000, 5000)
SAMPLES_PER_MODE = 30
WARM_REQUESTS = 5
PAGE_SIZE = 50
LOCK_POSITIVE_CONTROL_HOLD_SECONDS = 0.2


class _TimedRLock:
    """Test-only wrapper that records waits for the projection-cache mutex."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.wait_samples_ms: list[float] = []

    def acquire(self, *args, **kwargs) -> bool:
        started = monotonic()
        acquired = self._lock.acquire(*args, **kwargs)
        self.wait_samples_ms.append(round((monotonic() - started) * 1000, 3))
        return acquired

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _admin() -> User:
    return User(
        id=-1,
        username="phase3-scale-admin",
        password_hash="unused",
        role="admin",
        real_name="Phase3 scale admin",
        is_active=True,
        must_change_password=False,
        customer_access_mode="all",
    )


def _phase3_scale_app(
    tmp_path: Path, tiers: tuple[int, ...]
) -> Generator[tuple[FastAPI, object, dict[int, int]], None, None]:
    """Build a disposable database containing only the supplied total tiers.

    Each tier has its own customer and product.  Rows cycle through one to
    three item snapshots and three pre-delivery statuses.  They are direct
    performance-fixture rows rather than claimed procurement, stock, delivery,
    or financial source facts.
    """

    engine = create_sqlite_engine(tmp_path / "phase3-scale.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    customer_ids: dict[int, int] = {}
    orders_by_tier: dict[int, list[Order]] = {}
    locations_by_tier: dict[int, int] = {}
    with factory() as db:
        material = Material(
            code="P04-SCALE-MATERIAL",
            quote_price=Decimal("1.2500"),
            supplier_name="P04 synthetic supplier",
            layer_count=3,
            flute_type="A",
            is_active=True,
        )
        db.add(material)
        db.flush()
        for tier in tiers:
            customer = Customer(name=f"P04 synthetic scale {tier}")
            db.add(customer)
            db.flush()
            customer_ids[tier] = int(customer.id)
            location = WarehouseLocation(
                location_code=f"P04-{tier}-LOC",
                location_name=f"P04 synthetic {tier} location",
                warehouse_type="finished",
                address_kind="legacy",
                address_version=1,
            )
            db.add(location)
            db.flush()
            locations_by_tier[tier] = int(location.id)
            product = Product(
                customer_id=customer.id,
                product_code=f"P04-SCALE-{tier}",
                customer_material_code=f"P04-CUST-{tier}",
                product_name=f"P04 scale carton {tier}",
                material_id=material.id,
                box_style="0201",
                layer_count=3,
                flute_type="A",
                report_length_mm=500,
                report_width_mm=300,
            )
            db.add(product)
            db.flush()
            orders_by_tier[tier] = []
            for index in range(tier):
                item_count = index % 3 + 1
                order = Order(
                    order_number=f"P04-{tier}-{index:05d}",
                    customer_id=customer.id,
                    customer_po=f"P04-PO-{index % 17:02d}",
                    order_date=date(2026, 9, 1),
                    delivery_date=date(2026, 9, 20),
                    status=(
                        "pending_confirmation"
                        if index % 3 == 0
                        else "pending_production"
                        if index % 3 == 1
                        else "production"
                    ),
                    payment_status="unpaid",
                    total_amount=Decimal(str(item_count * 10)),
                )
                for item_index in range(item_count):
                    order.items.append(
                        OrderItem(
                            product_id=product.id,
                            item_sequence=item_index + 1,
                            item_order_number=(
                                f"P04-{tier}-{index:05d}-{item_index + 1}"
                            ),
                            quantity=10,
                            delivered_quantity=0,
                            unit_price=Decimal("1.0000"),
                            subtotal=Decimal("10.00"),
                            material_status="pending",
                            snapshot_product_code=product.product_code,
                            snapshot_product_name=product.product_name,
                            snapshot_spec="500x300",
                            snapshot_material=material.code,
                            material_id=material.id,
                            snapshot_supplier_name=material.supplier_name,
                            layer_count=3,
                            flute_type="A",
                            snapshot_report_length_mm=500,
                            snapshot_report_width_mm=300,
                            snapshot_pieces_per_box=1,
                            special_process=DEFAULT_CUTTING_MODE,
                        )
                    )
                db.add(order)
                orders_by_tier[tier].append(order)
            db.flush()
        # Direct fixture rows deliberately cover four read-projection states:
        # no procurement, confirmed procurement, posted incoming + production,
        # and dispatched fulfillment.  They are legal synthetic relations only;
        # no normal write API is being claimed or exercised here.
        for tier, orders in orders_by_tier.items():
            for index, order in enumerate(orders):
                item = order.items[0]
                phase = index % 4
                if phase == 0:
                    continue
                supplier_order = SupplierRequisitionOrder(
                    order_number=f"P04-SRO-{tier}-{index:05d}",
                    total_quantity=10,
                    requisition_qty=10,
                    status="confirmed",
                )
                db.add(supplier_order)
                db.flush()
                db.add(
                    SupplierRequisitionOrderItem(
                        supplier_order_id=supplier_order.id,
                        order_item_id=item.id,
                        product_id=item.product_id,
                        product_name=item.snapshot_product_name,
                        quantity=10,
                        requisition_qty=10,
                    )
                )
                if phase == 1:
                    continue
                receipt = IncomingReceipt(
                    receipt_number=f"P04-IR-{tier}-{index:05d}",
                    status="posted",
                    received_at=date(2026, 9, 2),
                    idempotency_key=f"p04-ir-{tier}-{index:05d}",
                )
                db.add(receipt)
                db.flush()
                db.add(
                    IncomingReceiptItem(
                        receipt_id=receipt.id,
                        order_id=order.id,
                        order_item_id=item.id,
                        planned_quantity=10,
                        received_quantity=10,
                        cumulative_received_quantity=10,
                        variance_quantity=0,
                        variance_type="matched",
                        resolution_status="not_required",
                        status="posted",
                    )
                )
                if phase == 2:
                    continue
                db.add(
                    ProductionTask(
                        order_item_id=item.id,
                        status="completed",
                        planned_quantity=10,
                        finished_coverage_snapshot=0,
                        ordered_quantity_snapshot=10,
                        material_received_quantity=10,
                        material_input_quantity=10,
                        output_factor=1,
                        version=1,
                    )
                )
                delivery = Delivery(
                    delivery_number=f"P04-DN-{tier}-{index:05d}",
                    customer_id=order.customer_id,
                    delivery_date=date(2026, 9, 3),
                    status="dispatched",
                    total_quantity=5,
                )
                db.add(delivery)
                db.flush()
                db.add(
                    DeliveryItem(
                        delivery_id=delivery.id,
                        order_item_id=item.id,
                        delivered_quantity=5,
                    )
                )
                lot = InventoryLot(
                    lot_number=f"P04-LOT-{tier}-{index:05d}",
                    inventory_type="finished",
                    warehouse_location_id=locations_by_tier[tier],
                    quantity_available=5,
                    quantity_reserved=5,
                    quantity_consumed=0,
                    quantity_damaged=0,
                    quantity_scrapped=0,
                    unit="boxes",
                    status="active",
                    source_type="manual",
                    stock_date=date(2026, 9, 3),
                    stock_date_accuracy="exact",
                    last_movement_at=datetime(2026, 9, 3, 8, 0),
                    version=1,
                )
                db.add(lot)
                db.flush()
                db.add(
                    InventoryReservation(
                        reservation_number=f"P04-RSV-{tier}-{index:05d}",
                        inventory_lot_id=lot.id,
                        reservation_type="finished_order",
                        order_id=order.id,
                        order_item_id=item.id,
                        reserved_stock_quantity=5,
                        credited_requirement_quantity=5,
                        status="active",
                        reservation_group_key=f"p04-rsv-{tier}-{index:05d}",
                        idempotency_key=f"p04-rsv-{tier}-{index:05d}",
                    )
                )
        db.commit()

    app = FastAPI()
    app.include_router(orders_api.router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[orders_api.can_read] = _admin
    try:
        yield app, engine, customer_ids
    finally:
        engine.dispose()


@pytest.fixture()
def phase3_scale_app(tmp_path: Path):
    """Historical P04 fixture: 100+1000+5000 rows in one database."""

    yield from _phase3_scale_app(tmp_path, TIERS)


def _p95(values: list[float]) -> float:
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def _request_payload(
    app: FastAPI,
    customer_id: int,
    *,
    expected_unfinished_total: int | None = sum(TIERS),
) -> tuple[dict, float]:
    with TestClient(app) as client:
        started = monotonic()
        response = client.get(
            "/api/orders",
            params={
                "customer_id": customer_id,
                # This is the orders workspace's current default, rather
                # than an unfiltered maintenance view.
                "scope": "active",
                # The current orders page explicitly requests the summary
                # projection.  Measuring ``full`` here would exercise an
                # unused, much larger payload and would not represent the
                # desktop list path this card is intended to protect.
                "detail_level": "summary",
                "page": 1,
                "page_size": PAGE_SIZE,
            },
        )
        elapsed_ms = (monotonic() - started) * 1000
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["items"]) == min(PAGE_SIZE, int(payload["total"]))
    assert "items" not in payload["items"][0]
    if expected_unfinished_total is not None:
        assert payload["unfinished_total"] == expected_unfinished_total
    return payload, elapsed_ms


def _request(app: FastAPI, customer_id: int) -> tuple[int, float]:
    payload, elapsed_ms = _request_payload(app, customer_id)
    return int(payload["total"]), elapsed_ms


def _run_mode(app: FastAPI, customer_id: int, concurrency: int) -> dict[str, object]:
    latencies: list[float] = []
    wall_times: list[float] = []
    errors: list[str] = []
    for _ in range(SAMPLES_PER_MODE // concurrency):
        started = monotonic()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_request, app, customer_id) for _ in range(concurrency)]
            for future in futures:
                try:
                    total, elapsed_ms = future.result()
                    assert total == next(tier for tier, value in CUSTOMER_IDS.items() if value == customer_id)
                    latencies.append(round(elapsed_ms, 3))
                except Exception as exc:  # keep the full batch evidence before failing
                    errors.append(f"{type(exc).__name__}: {exc}")
        wall_times.append(round((monotonic() - started) * 1000, 3))
    assert not errors, errors
    assert len(latencies) == SAMPLES_PER_MODE
    return {
        "concurrency": concurrency,
        "request_samples_ms": latencies,
        "batch_wall_samples_ms": wall_times,
        "p50_ms": round(median(latencies), 3),
        "p95_ms": round(_p95(latencies), 3),
        "max_ms": round(max(latencies), 3),
        "errors": errors,
        # The original card wrote a constant false here.  Ordinary application
        # reads have no SQLite busy-handler callback exposed to Python, so a
        # request/SQL duration alone cannot truthfully be called DB lock wait.
        "database_lock_wait_ms": None,
        "database_lock_wait_observable": False,
    }


# Kept module-local so _run_mode can validate a response without adding hidden
# state to the FastAPI app under measurement.
CUSTOMER_IDS: dict[int, int] = {}


def test_order_list_scale_100_1000_5000_has_bounded_queries_and_no_errors(
    phase3_scale_app,
) -> None:
    """Measure fixed-page reads after five warm requests at each requested tier."""

    app, engine, customer_ids = phase3_scale_app
    CUSTOMER_IDS.clear()
    CUSTOMER_IDS.update(customer_ids)
    reports: list[dict[str, object]] = []
    for tier in TIERS:
        for _ in range(WARM_REQUESTS):
            total, _elapsed = _request(app, customer_ids[tier])
            assert total == tier

        selects = 0
        count_lock = Lock()

        def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
            nonlocal selects
            if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                with count_lock:
                    selects += 1

        event.listen(engine, "before_cursor_execute", count_sql)
        try:
            modes = [_run_mode(app, customer_ids[tier], concurrency) for concurrency in (1, 3, 5)]
        finally:
            event.remove(engine, "before_cursor_execute", count_sql)
        reports.append(
            {
                "tier_orders": tier,
                "page_size": PAGE_SIZE,
                "warm_requests": WARM_REQUESTS,
                "total_complete_requests": SAMPLES_PER_MODE * 3,
                "selects_across_measured_requests": selects,
                "selects_per_request": round(selects / (SAMPLES_PER_MODE * 3), 3),
                "modes": modes,
            }
        )

    payload = {
        "fixture": {
            "database": "fresh disposable SQLite file",
            "tiers": list(TIERS),
            "items_per_order": "cycles 1, 2, 3",
            "statuses": ["pending_confirmation", "pending_production", "production"],
            "source": "direct performance fixture, not an order-flow proof",
        },
        "measurements": reports,
    }
    print("P04_SCALE_METRICS=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    result_path = os.environ.get("TM_PHASE3_SCALE_RESULT_PATH")
    if result_path:
        output = Path(result_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # T16's one-second value is a historical comparison reference, not an
    # invented service-level contract for this fresh synthetic fixture.  Keep
    # the raw percentiles for review while enforcing the actual invariants:
    # fixed query shape and no failed request.  SQLite lock waiting is measured
    # separately with a controlled positive control below.
    assert {report["selects_per_request"] for report in reports} == {6.0}
    assert all(
        not mode["errors"]
        for report in reports
        for mode in report["modes"]
    )


def test_committed_order_change_invalidates_active_badge_projection_cache(
    phase3_scale_app,
) -> None:
    """A cached display count must never survive a committed order mutation."""

    app, engine, customer_ids = phase3_scale_app
    CUSTOMER_IDS.clear()
    CUSTOMER_IDS.update(customer_ids)
    _request(app, customer_ids[100])
    with Session(engine) as db:
        order = db.scalar(select(Order).limit(1))
        assert order is not None
        order.remark = "P04 cache invalidation synthetic marker"
        db.commit()

    selects = 0

    def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            selects += 1

    event.listen(engine, "before_cursor_execute", count_sql)
    try:
        total, _elapsed = _request(app, customer_ids[100])
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)
    assert total == 100
    # A cache hit is a small page read.  A commit has to rebuild the global
    # projection, which is intentionally much larger and therefore visible.
    assert selects > 100


def _write_astra_measurement(name: str, payload: dict) -> None:
    """Persist machine-readable F1/F2 evidence only when the wrapper requests it."""

    result_dir = os.environ.get("TM_PHASE3_ASTRA_RESULT_DIR")
    if not result_dir:
        return
    output = Path(result_dir) / name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_sqlite_delete_journal_lock_wait_positive_control(tmp_path: Path) -> None:
    """Show that the measurement sees a real SQLite lock wait before normal reads.

    This uses two independent DB-API connections to a new file and preserves the
    production DELETE journal and 5-second busy timeout.  It intentionally does
    not change global SQLite settings or call application write endpoints.
    """

    database_path = tmp_path / "p07-lock-positive-control.sqlite3"
    engine = create_sqlite_engine(database_path)
    engine.dispose()
    holder = sqlite3.connect(database_path, timeout=5, check_same_thread=False)
    waiter = sqlite3.connect(database_path, timeout=5, check_same_thread=False)
    for connection in (holder, waiter):
        connection.execute("PRAGMA busy_timeout = 5000")
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    holder.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    holder.execute("INSERT INTO probe(id, value) VALUES (1, 'before')")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("UPDATE probe SET value = 'holder' WHERE id = 1")

    waiter_started = Event()
    waiter_finished = Event()
    observation: dict[str, object] = {}

    def contend_for_write() -> None:
        waiter_started.set()
        started = monotonic()
        try:
            waiter.execute("UPDATE probe SET value = 'waiter' WHERE id = 1")
            waiter.commit()
            observation["result"] = "committed"
        except sqlite3.OperationalError as exc:
            observation["result"] = "sqlite_operational_error"
            observation["error"] = str(exc)
        finally:
            observation["sql_and_transaction_elapsed_ms"] = round(
                (monotonic() - started) * 1000, 3
            )
            waiter_finished.set()

    worker = Thread(target=contend_for_write, name="p07-sqlite-lock-waiter")
    worker.start()
    assert waiter_started.wait(timeout=2)
    sleep(LOCK_POSITIVE_CONTROL_HOLD_SECONDS)
    # The second connection must still be pending before the holder releases;
    # otherwise the test would not be a lock-wait positive control.
    assert not waiter_finished.is_set()
    released_at = monotonic()
    holder.commit()
    assert waiter_finished.wait(timeout=5)
    worker.join(timeout=1)
    holder.close()
    waiter.close()

    waited_ms = float(observation["sql_and_transaction_elapsed_ms"])
    payload = {
        "database": str(database_path),
        "connections": "two independent sqlite3 connections",
        "journal_mode": "delete",
        "busy_timeout_ms": 5000,
        "holder_transaction": "BEGIN EXCLUSIVE + UPDATE",
        "waiter_sql": "UPDATE probe SET value = 'waiter' WHERE id = 1",
        "controlled_hold_ms": LOCK_POSITIVE_CONTROL_HOLD_SECONDS * 1000,
        "release_monotonic": released_at,
        "waiter": observation,
        "database_lock_wait_observed": waited_ms >= LOCK_POSITIVE_CONTROL_HOLD_SECONDS * 500,
        "application_mutex_wait_ms": None,
        "request_total_elapsed_ms": None,
    }
    _write_astra_measurement("f1-lock-positive-control.json", payload)
    print("P07_F1_LOCK_POSITIVE_CONTROL=" + json.dumps(payload, sort_keys=True))
    assert observation.get("result") == "committed", observation
    assert waited_ms >= LOCK_POSITIVE_CONTROL_HOLD_SECONDS * 500


def _measure_request(
    app: FastAPI,
    engine,
    customer_id: int,
    timed_lock: _TimedRLock,
    *,
    expected_unfinished_total: int,
) -> dict[str, object]:
    sql_samples_ms: list[float] = []

    def sql_started(_connection, _cursor, _statement, _parameters, context, _many):
        context._p07_started = monotonic()

    def sql_finished(_connection, _cursor, _statement, _parameters, context, _many):
        started = getattr(context, "_p07_started", None)
        if started is not None:
            sql_samples_ms.append(round((monotonic() - started) * 1000, 3))

    lock_count_before = len(timed_lock.wait_samples_ms)
    event.listen(engine, "before_cursor_execute", sql_started)
    event.listen(engine, "after_cursor_execute", sql_finished)
    try:
        payload, request_elapsed_ms = _request_payload(
            app,
            customer_id,
            expected_unfinished_total=expected_unfinished_total,
        )
    finally:
        event.remove(engine, "before_cursor_execute", sql_started)
        event.remove(engine, "after_cursor_execute", sql_finished)
    lock_samples = timed_lock.wait_samples_ms[lock_count_before:]
    return {
        "request_total_elapsed_ms": round(request_elapsed_ms, 3),
        "sql_execution_total_ms": round(sum(sql_samples_ms), 3),
        "sql_statement_count": len(sql_samples_ms),
        "sql_statement_max_ms": round(max(sql_samples_ms, default=0), 3),
        "application_mutex_wait_ms": round(sum(lock_samples), 3),
        "application_mutex_wait_max_ms": round(max(lock_samples, default=0), 3),
        # Python's sqlite DB-API exposes no busy-handler duration.  These
        # normal, uncontended samples therefore record it as unobservable;
        # F1's independent-connection positive control proves the detector.
        "database_lock_wait_ms": None,
        "database_lock_wait_observable": False,
        "status": int(payload["total"]),
        "unfinished_total": int(payload["unfinished_total"]),
        "item_ids": [int(item["id"]) for item in payload["items"]],
    }


def _measurement_summary(samples: list[dict[str, object]]) -> dict[str, float]:
    request_times = [float(sample["request_total_elapsed_ms"]) for sample in samples]
    sql_counts = [float(sample["sql_statement_count"]) for sample in samples]
    return {
        "request_p50_ms": round(median(request_times), 3),
        "request_p95_ms": round(_p95(request_times), 3),
        "request_max_ms": round(max(request_times), 3),
        "sql_count_p50": round(median(sql_counts), 3),
        "sql_count_max": round(max(sql_counts), 3),
    }


@pytest.mark.parametrize("tier", TIERS)
def test_order_projection_cold_hot_post_commit_and_status_change_are_distinguished(
    tmp_path: Path, monkeypatch, tier: int
) -> None:
    """Measure a single total-size DB, rather than mislabelling filters as tiers.

    Each parameter creates its own fresh database whose total data size, filter
    size, and global active-order projection size are all the same ``tier``.
    The rows are a direct synthetic fixture with 1/2/3 line distribution and
    pending-confirmation/pending-production/production states.  It remains a
    read-path measurement, not a proof that the normal purchase, inventory or
    fulfillment APIs created those facts.
    """

    import app.services.order_business_status as status_service

    fixture = _phase3_scale_app(tmp_path / f"tier-{tier}", (tier,))
    app, engine, customer_ids = next(fixture)
    timed_lock = _TimedRLock()
    monkeypatch.setattr(status_service, "_ACTIVE_STATUS_BADGE_CACHE_LOCK", timed_lock)
    status_service._clear_active_status_badge_cache()
    customer_id = customer_ids[tier]
    expected_total = tier

    def sample(mode: str, *, commit_before_read: bool = False) -> dict[str, object]:
        if mode == "cold":
            status_service._clear_active_status_badge_cache()
        if commit_before_read:
            with Session(engine) as session:
                order = session.scalar(select(Order).where(Order.customer_id == customer_id).limit(1))
                assert order is not None
                order.remark = f"P07 synthetic cache invalidation {tier}-{mode}"
                session.commit()
        return _measure_request(
            app,
            engine,
            customer_id,
            timed_lock,
            expected_unfinished_total=expected_total,
        )

    try:
        metrics = {
            mode: [
                sample(mode, commit_before_read=(mode == "post_commit"))
                for _ in range(5)
            ]
            for mode in ("cold", "hot", "post_commit")
        }

        # Hold a real transaction open after its write is flushed, then start
        # the reader.  This records overlapping independent sessions without
        # injecting a production lock failure or changing the busy timeout.
        status_service._clear_active_status_badge_cache()
        writer_flushed = Event()
        reader_started = Event()
        overlap: dict[str, object] = {}

        def overlapping_writer() -> None:
            started = monotonic()
            with Session(engine) as session:
                order = session.scalar(
                    select(Order).where(Order.customer_id == customer_id).limit(1)
                )
                assert order is not None
                order.remark = f"P07 controlled read-write overlap {tier}"
                session.flush()
                writer_flushed.set()
                assert reader_started.wait(timeout=2)
                sleep(0.05)
                session.commit()
            overlap["writer_transaction_elapsed_ms"] = round(
                (monotonic() - started) * 1000, 3
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(overlapping_writer)
            assert writer_flushed.wait(timeout=2)
            reader_started.set()
            overlap["reader"] = _measure_request(
                app,
                engine,
                customer_id,
                timed_lock,
                expected_unfinished_total=expected_total,
            )
            writer.result(timeout=5)
        overlap["post_commit_reader"] = _measure_request(
            app,
            engine,
            customer_id,
            timed_lock,
            expected_unfinished_total=expected_total,
        )

        # A committed status change must invalidate the projection.  The active
        # list and global badge must both stop counting the changed order.
        status_service._clear_active_status_badge_cache()
        before = _measure_request(
            app, engine, customer_id, timed_lock, expected_unfinished_total=expected_total
        )
        target_id = max(before["item_ids"])
        with Session(engine) as session:
            target = session.get(Order, target_id)
            assert target is not None
            # ``completed`` is a finance-lifecycle label, not a terminal
            # management state.  Use the actual terminal policy value so this
            # synthetic projection test verifies a genuine active-list change.
            target.status = "cancelled"
            session.commit()
        after = _measure_request(
            app,
            engine,
            customer_id,
            timed_lock,
            expected_unfinished_total=expected_total - 1,
        )
        assert after["status"] == expected_total - 1
        assert after["unfinished_total"] == expected_total - 1
        assert target_id not in after["item_ids"]

        payload = {
            "fixture": {
                "database": "fresh per-tier disposable SQLite file",
                "total_orders": tier,
                "filtered_orders": tier,
                "global_projection_orders_before_status_change": tier,
                "line_distribution": "cycles 1, 2, 3",
                "state_distribution": [
                    "pending_confirmation",
                    "pending_production",
                    "production",
                ],
                "related_state_distribution": {
                    "no_procurement": "one quarter",
                    "confirmed_procurement": "one quarter",
                    "posted_incoming": "one quarter",
                    "completed_production_and_partial_dispatch": "one quarter",
                },
                "inventory_distribution": "one quarter has finished lots and active finished-order reservations",
            },
            "samples_per_mode": 5,
            "measurements": metrics,
            "summaries": {
                mode: _measurement_summary(samples)
                for mode, samples in metrics.items()
            },
            "controlled_read_write_overlap": overlap,
            "status_change": {"before": before, "after": after},
        }
        _write_astra_measurement(f"f2-cache-and-status-tier-{tier}.json", payload)
        print("P07_F2_CACHE_METRICS=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
        # Cold and post-commit reads rebuild the global projection.  A warm read
        # must retain the existing low-query path, but no latency threshold is
        # treated as a production SLA.
        hot_max = max(
            int(item["sql_statement_count"]) for item in metrics["hot"]
        )
        assert all(
            int(item["sql_statement_count"]) > hot_max for item in metrics["cold"]
        )
        assert all(
            int(item["sql_statement_count"]) > hot_max
            for item in metrics["post_commit"]
        )
    finally:
        fixture.close()

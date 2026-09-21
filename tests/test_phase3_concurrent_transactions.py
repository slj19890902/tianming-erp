"""P02: actual dual-connection transaction races on a disposable SQLite file."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Lock
from time import monotonic

from fastapi import Request
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select

from tests.test_p1_81_receipt_purpose_flow import (
    _business_counts,
    _create_frozen_sources,
    _freeze_receipt_fact,
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from tests.test_phase11_requisition import _login, requisition_app


def _receive_payload(source, fact: dict, *, idempotency_key: str) -> dict:
    return {
        "received_quantity": 450,
        "idempotency_key": idempotency_key,
        "expected_receipt_fact_version": fact["receipt_fact_version"],
        "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
        "expected_purpose_snapshot_version": source.purpose_snapshot_version,
        "receipt_plan_fingerprint": fact["receipt_plan_fingerprint"],
        "expected_actual_material_version": fact["actual_material_version"],
        "actual_material_fingerprint": fact["actual_material_fingerprint"],
    }


@pytest.mark.parametrize("concurrency", [2, 5])
def test_same_key_receipt_race_uses_independent_connections_and_one_fact(
    requisition_app, monkeypatch, concurrency: int
) -> None:
    """Concurrent real request transactions cross a DB barrier, then replay one fact."""

    from app.api.deps import get_db
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem

    app, session_factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    _seed_material_and_staging(session_factory)
    original_get_db = app.dependency_overrides[get_db]
    request_barrier = Barrier(concurrency, timeout=10)
    start_barrier = Barrier(concurrency + 1, timeout=10)
    trace_lock = Lock()
    trace: list[dict[str, object]] = []

    def traced_get_db(request: Request):
        generator = original_get_db()
        session = next(generator)
        attempt = request.headers.get("x-phase3-attempt")
        if attempt:
            session.info["phase3_attempt"] = attempt

            @event.listens_for(session, "after_begin")
            def record_real_transaction(_session, transaction, connection) -> None:
                if transaction.nested or _session.info.get("phase3_begun"):
                    return
                _session.info["phase3_begun"] = True
                with trace_lock:
                    trace.append(
                        {
                            "attempt": attempt,
                            "connection_id": id(connection.connection.driver_connection),
                            "transaction_started": monotonic(),
                        }
                    )
                request_barrier.wait()

        try:
            yield session
        finally:
            generator.close()

    app.dependency_overrides[get_db] = traced_get_db
    with TestClient(app) as setup_client:
        _login(setup_client, "admin")
        source = _create_frozen_sources(
            setup_client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            setup_client, source, idempotency_key="p02-race-price-fact"
        )
        assert frozen.status_code == 200, frozen.text
        fact = frozen.json()
    before = _business_counts(session_factory)
    payload = _receive_payload(
        source, fact, idempotency_key="p02-same-key-race"
    )

    def submit(attempt: str):
        with TestClient(app) as client:
            _login(client, "admin")
            start_barrier.wait()
            started = monotonic()
            response = client.put(
                f"/api/incoming/receive/{source.route_key}",
                json=payload,
                headers={"x-phase3-attempt": attempt},
            )
            ended = monotonic()
        return {"attempt": attempt, "started": started, "ended": ended, "response": response}

    attempts = [f"attempt-{index}" for index in range(1, concurrency + 1)]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(submit, attempt) for attempt in attempts]
        start_barrier.wait()
        results = [future.result() for future in futures]

    assert {entry["attempt"] for entry in trace} == set(attempts)
    assert len({entry["connection_id"] for entry in trace}) == concurrency
    assert max(result["started"] for result in results) - min(
        result["started"] for result in results
    ) < 1
    assert all(result["ended"] >= result["started"] for result in results)
    responses = [result["response"] for result in results]
    assert [response.status_code for response in responses] == [200] * concurrency, [
        response.text for response in responses
    ]
    assert len({response.json()["receipt_item_id"] for response in responses}) == 1
    transaction_zero = min(entry["transaction_started"] for entry in trace)
    trace_by_attempt = {str(entry["attempt"]): entry for entry in trace}
    print(
        "P02_CONCURRENCY_TRACE="
        + json.dumps(
            [
                {
                    "attempt": result["attempt"],
                    "connection_id": trace_by_attempt[result["attempt"]]["connection_id"],
                    "transaction_start_ms": round(
                        (trace_by_attempt[result["attempt"]]["transaction_started"] - transaction_zero)
                        * 1000,
                        3,
                    ),
                    "request_start_ms": round(
                        (result["started"] - transaction_zero) * 1000, 3
                    ),
                    "request_end_ms": round(
                        (result["ended"] - transaction_zero) * 1000, 3
                    ),
                    "status": result["response"].status_code,
                    "receipt_item_id": result["response"].json()["receipt_item_id"],
                }
                for result in sorted(results, key=lambda item: item["attempt"])
            ],
            sort_keys=True,
        )
    )

    after = _business_counts(session_factory)
    assert {key: value for key, value in after.items() if key != "audit"} == {
        "receipt": before["receipt"] + 1,
        "receipt_item": before["receipt_item"] + 1,
        "settlement_price": before["settlement_price"] + 1,
        "task": before["task"] + 1,
        "completion": before["completion"] + 1,
        "lot": before["lot"] + 1,
        "movement": before["movement"] + 2,
    }
    with session_factory() as session:
        receipt = session.scalar(
            select(IncomingReceipt).where(
                IncomingReceipt.idempotency_key == "p02-same-key-race"
            )
        )
        assert receipt is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(IncomingReceiptItem)
                .where(IncomingReceiptItem.receipt_id == receipt.id)
            )
            == 1
        )

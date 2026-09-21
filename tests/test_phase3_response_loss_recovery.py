"""P01: prove actual write APIs recover after the client loses a committed response.

The ASGI wrapper below deliberately buffers the selected HTTP response and
raises only *after* the real endpoint returns.  It never changes the request,
database, route, or transaction.  The order and receipt endpoints explicitly
commit before returning, so a normal client retry demonstrates the exact
post-commit / pre-receipt recovery branch without a direct business-fact insert.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_semi_finished_order_reservation import (
    add_finished_lot,
    b1_app,
    finished_plan,
    login,
    order_item,
)
from tests.test_p1_81_receipt_purpose_flow import (
    _business_counts,
    _create_frozen_sources,
    _freeze_receipt_fact,
    _receive,
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from tests.test_phase11_requisition import _login, requisition_app


class ResponseLostAfterCommit(RuntimeError):
    """Test-only client transport error after the downstream ASGI app returned."""


class DropSelectedCommittedResponse:
    """Run the real app, hide one completed response, then lose the transport."""

    def __init__(self, app, should_drop: Callable[[dict], bool]) -> None:
        self.app = app
        self.should_drop = should_drop
        self.dropped_paths: list[str] = []
        self.dropped_responses: list[tuple[int, bytes]] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.should_drop(scope):
            await self.app(scope, receive, send)
            return

        buffered: list[dict] = []

        async def retain_response(message: dict) -> None:
            buffered.append(message)

        await self.app(scope, receive, retain_response)
        assert buffered and buffered[0]["type"] == "http.response.start"
        status_code = int(buffered[0]["status"])
        body = b"".join(
            bytes(message.get("body", b""))
            for message in buffered
            if message["type"] == "http.response.body"
        )
        self.dropped_paths.append(str(scope["path"]))
        self.dropped_responses.append((status_code, body))
        raise ResponseLostAfterCommit("test transport lost the completed API response")


def _is_order_create(scope: dict) -> bool:
    return scope["method"] == "POST" and scope["path"] == "/api/orders"


def _is_incoming_receive(scope: dict) -> bool:
    return scope["method"] == "PUT" and str(scope["path"]).startswith(
        "/api/incoming/receive/"
    )


def test_order_create_replays_one_committed_fact_after_response_loss(b1_app) -> None:
    """The order was committed once even though its first client never got bytes."""

    from app.models.material import Material
    from app.models.order import Order
    from app.models.product import Product
    from app.models.supplier import Supplier
    from app.models.warehouse_inventory import InventoryReservation
    from tests.test_phase16_pdf_order_import import _signed_pdf_preview_token

    app, session_factory = b1_app
    with session_factory() as session:
        supplier = session.scalar(select(Supplier).where(Supplier.is_active.is_(True)))
        assert supplier is not None
        material = Material(
            code="P01-LOSS-A416D",
            supplier_name=supplier.standard_name,
            is_active=True,
            layer_count=3,
            flute_type="B",
        )
        session.add(material)
        session.flush()
        product = session.get(Product, 1)
        assert product is not None
        product.material_id = material.id
        session.commit()

    lot, version = add_finished_lot(
        session_factory, product_id=1, quantity=10, key="p01-response-loss-stock"
    )
    payload = {
        "customer_id": 1,
        "customer_po": "P01-LOSS-ORDER",
        "idempotency_key": "p01-order-response-loss",
        "import_draft": True,
        "import_integrity_status": "passed",
        "import_integrity_errors": [],
        "pdf_import_confirmation": {
            "preview_safety_token": _signed_pdf_preview_token(app),
            "confirmed": True,
        },
        "items": [
            order_item(
                1,
                10,
                {"finished": [finished_plan(lot, version, 10)], "semi": []},
                line="p01-response-loss-row",
            )
        ],
    }
    lost_transport = DropSelectedCommittedResponse(app, _is_order_create)
    with TestClient(lost_transport) as lost_client:
        login(lost_client, "sales")
        with pytest.raises(ResponseLostAfterCommit):
            lost_client.post("/api/orders", json=payload)
    assert lost_transport.dropped_paths == ["/api/orders"]
    assert [status for status, _body in lost_transport.dropped_responses] == [201]

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(InventoryReservation)) == 1

    with TestClient(app) as client:
        login(client, "sales")
        replay = client.post("/api/orders", json=payload)
        assert replay.status_code == 201, replay.text
        created_id = int(replay.json()["id"])
        recovery = client.get("/api/orders/create-attempts/p01-order-response-loss")
        assert recovery.status_code == 200, recovery.text
        assert recovery.json() == {
            "status": "completed",
            "order": {"id": created_id, "customer_id": 1},
        }
        changed = deepcopy(payload)
        changed["items"][0]["quantity"] = 9
        conflict = client.post("/api/orders", json=changed)
        assert conflict.status_code == 409, conflict.text

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(InventoryReservation)) == 1


def test_receipt_replays_one_committed_fact_after_response_loss(
    requisition_app, monkeypatch
) -> None:
    """The receipt chain remains singular when the first response is lost."""

    app, session_factory = requisition_app
    # The endpoint requires a published current location identity.  This is
    # synthetic map metadata only; receipt facts still enter through the API.
    _use_p181_published_map_identity(monkeypatch)
    _seed_material_and_staging(session_factory)
    lost_transport = DropSelectedCommittedResponse(app, _is_incoming_receive)
    with TestClient(lost_transport) as lost_client:
        _login(lost_client, "admin")
        source = _create_frozen_sources(
            lost_client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            lost_client, source, idempotency_key="p01-receipt-price-fact"
        )
        assert frozen.status_code == 200, frozen.text
        receipt_fact = frozen.json()
        before = _business_counts(session_factory)
        with pytest.raises(ResponseLostAfterCommit):
            _receive(
                lost_client,
                source,
                receipt_fact,
                quantity=450,
                idempotency_key="p01-receipt-response-loss",
            )
    assert lost_transport.dropped_paths == [f"/api/incoming/receive/{source.route_key}"]
    assert [status for status, _body in lost_transport.dropped_responses] == [200]
    after_lost_response = _business_counts(session_factory)
    assert after_lost_response != before
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem

    with session_factory() as session:
        committed_item_id = session.scalar(
            select(IncomingReceiptItem.id)
            .join(IncomingReceipt, IncomingReceiptItem.receipt_id == IncomingReceipt.id)
            .where(IncomingReceipt.idempotency_key == "p01-receipt-response-loss")
        )
    assert committed_item_id is not None

    with TestClient(app) as client:
        _login(client, "admin")
        after_relogin = _business_counts(session_factory)
        replay = _receive(
            client,
            source,
            receipt_fact,
            quantity=450,
            idempotency_key="p01-receipt-response-loss",
        )
        assert replay.status_code == 200, replay.text
        replay_body = replay.json()
        assert int(replay_body["receipt_item_id"]) == int(committed_item_id)
        assert _business_counts(session_factory) == after_relogin

        changed = _receive(
            client,
            source,
            receipt_fact,
            quantity=451,
            idempotency_key="p01-receipt-response-loss",
        )
        assert changed.status_code == 409, changed.text
        assert _business_counts(session_factory) == after_relogin

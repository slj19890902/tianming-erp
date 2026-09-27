from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.audit import OperationLog
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.order_import_source import OrderImportSource, OrderImportSourceLine
from app.models.product import Product
from app.models.supplier import Supplier
from tests.test_phase16_pdf_order_import import _signed_pdf_preview_token
from tests.test_semi_finished_order_reservation import b1_app, login, order_item


def _ready_product(factory) -> None:
    with factory() as db:
        supplier = db.scalar(select(Supplier).where(Supplier.is_active.is_(True)))
        material = Material(
            code="A416D",
            supplier_name=supplier.standard_name,
            is_active=True,
            layer_count=3,
            flute_type="B",
        )
        db.add(material)
        db.flush()
        for product_id in (1, 2):
            db.get(Product, product_id).material_id = material.id
        db.commit()


def _payload(
    app,
    source_hash: str,
    key: str,
    *,
    quantity: int = 10,
    customer_po: str | None = "T02-SAME-PO",
) -> dict:
    preview_item = {
        "line_no": 17,
        "source_page": 2,
        "raw_lines": ["17 B1-P1 共享规格印刷1 10 1.00"],
        "product_code": "B1-P1",
        "product_name": "共享规格印刷1",
        "quantity": quantity,
        "unit_price": "1.00",
    }
    line = order_item(1, quantity, line="pdf-line-17")
    line.update(product_code="B1-P1", product_name="共享规格印刷1")
    return {
        "customer_id": 1,
        "customer_po": customer_po,
        "idempotency_key": key,
        "import_draft": True,
        "import_integrity_status": "passed",
        "import_integrity_errors": [],
        "pdf_import_confirmation": {
            "preview_safety_token": _signed_pdf_preview_token(
                app,
                source_hash=source_hash,
                source_name=f"{source_hash[:4]}.pdf",
                items=[preview_item],
            ),
            "confirmed": True,
        },
        "items": [line],
    }


def test_imported_order_without_customer_po_uses_stable_source_identity(b1_app) -> None:
    app, factory = b1_app
    _ready_product(factory)
    payload = _payload(
        app,
        "e" * 64,
        "t02-missing-customer-po",
        customer_po=None,
    )

    with TestClient(app) as client:
        login(client, "sales")
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    with factory() as db:
        order = db.scalar(select(Order))
        source = db.scalar(select(OrderImportSource))
        assert order is not None and order.customer_po is None
        assert source is not None and source.order_id == order.id
        assert source.source_hash == "e" * 64


def test_different_trusted_sources_with_same_po_create_independent_orders_and_lines(
    b1_app,
) -> None:
    app, factory = b1_app
    _ready_product(factory)
    first_payload = _payload(app, "a" * 64, "t02-first-source")
    second_payload = _payload(app, "b" * 64, "t02-second-source")

    with TestClient(app) as client:
        login(client, "sales")
        first = client.post("/api/orders", json=first_payload)
        second = client.post("/api/orders", json=second_payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    assert second.json()["related_existing_orders"] == [
        {
            "id": first.json()["id"],
            "order_number": first.json()["order_number"],
            "status": first.json()["status"],
        }
    ]
    with factory() as db:
        orders = list(
            db.scalars(
                select(Order)
                .where(Order.customer_po == "T02-SAME-PO")
                .order_by(Order.id)
            )
        )
        sources = list(db.scalars(select(OrderImportSource).order_by(OrderImportSource.id)))
        lines = list(db.scalars(select(OrderImportSourceLine).order_by(OrderImportSourceLine.id)))
        assert len(orders) == len(sources) == len(lines) == 2
        assert {source.source_hash for source in sources} == {"a" * 64, "b" * 64}
        assert {source.order_id for source in sources} == {order.id for order in orders}
        assert all(line.source_page == 2 and line.source_line_label == "17" for line in lines)
        assert all(line.order_item_id is not None for line in lines)
        assert all(
            json.loads(line.confirmation_summary_json)["manual_revision"] is False
            for line in lines
        )
        assert [item.quantity for item in db.scalars(select(OrderItem).order_by(OrderItem.id))] == [10, 10]
        audits = list(
            db.scalars(
                select(OperationLog)
                .where(OperationLog.action_code == "order.pdf_create")
                .order_by(OperationLog.id)
            )
        )
        assert len(audits) == 2
        audit_details = json.loads(audits[-1].details or "{}")
        source_summary = audit_details["import_source_summary"]
        assert source_summary == {
            "source_id": sources[-1].id,
            "source_kind": "pdf_upload",
            "line_count": 1,
            "manual_revision_count": 0,
            "line_mappings": [
                {
                    "source_position": 1,
                    "source_page": 2,
                    "source_sheet": None,
                    "source_row": None,
                    "source_line_label": "17",
                    "order_item_id": lines[-1].order_item_id,
                    "manual_revision": False,
                }
            ],
        }
        assert "raw_lines" not in (audits[-1].details or "")


def test_same_source_replays_original_and_rejects_changed_business_payload(b1_app) -> None:
    app, factory = b1_app
    _ready_product(factory)
    original = _payload(app, "c" * 64, "t02-original-key")
    with TestClient(app) as client:
        login(client, "sales")
        created = client.post("/api/orders", json=original)
        replay_payload = deepcopy(original)
        replay_payload["idempotency_key"] = "t02-rescan-key"
        replay = client.post("/api/orders", json=replay_payload)
        changed = deepcopy(replay_payload)
        changed["idempotency_key"] = "t02-changed-key"
        changed["items"][0]["quantity"] = 11
        conflict = client.post("/api/orders", json=changed)

    assert created.status_code == 201, created.text
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == created.json()["id"]
    assert replay.json()["source_replay"] is True
    assert conflict.status_code == 409
    assert "本次明细或处理方式与原保存不一致" in conflict.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 1
        assert db.scalar(select(func.count()).select_from(Order)) == 1


def test_different_trusted_sources_without_po_remain_independent(b1_app) -> None:
    app, factory = b1_app
    _ready_product(factory)
    with TestClient(app) as client:
        login(client, "sales")
        first = client.post(
            "/api/orders",
            json=_payload(
                app, "1" * 64, "t02-no-po-first", customer_po=None
            ),
        )
        second = client.post(
            "/api/orders",
            json=_payload(
                app, "2" * 64, "t02-no-po-second", customer_po=None
            ),
        )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 2


def test_cancelled_source_replays_original_and_new_source_does_not_reopen_it(
    b1_app,
) -> None:
    app, factory = b1_app
    _ready_product(factory)
    original = _payload(app, "3" * 64, "t02-cancelled-original")
    with TestClient(app) as client:
        login(client, "sales")
        created = client.post("/api/orders", json=original)
        assert created.status_code == 201, created.text
        with factory() as db:
            order = db.get(Order, created.json()["id"])
            order.status = "cancelled"
            db.commit()
        replay_payload = deepcopy(original)
        replay_payload["idempotency_key"] = "t02-cancelled-replay"
        replay = client.post("/api/orders", json=replay_payload)
        replacement = client.post(
            "/api/orders",
            json=_payload(app, "4" * 64, "t02-cancelled-new-source"),
        )

    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]
    assert replay.json()["status"] == "cancelled"
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["id"] != created.json()["id"]
    with factory() as db:
        original_order = db.get(Order, created.json()["id"])
        assert original_order.status == "cancelled"
        assert original_order.items[0].quantity == 10


def test_source_identity_is_shared_across_operators_and_rejects_other_customer(
    b1_app,
) -> None:
    from app.api.orders import _encode_pdf_preview_safety_token
    from app.models.user import User

    app, factory = b1_app
    _ready_product(factory)
    source_hash = "5" * 64
    sales_payload = _payload(app, source_hash, "t02-cross-actor-sales")
    with TestClient(app) as client:
        login(client, "sales")
        created = client.post("/api/orders", json=sales_payload)
        assert created.status_code == 201, created.text

        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            admin_token = _encode_pdf_preview_safety_token(
                {
                    "source_name": "cross-actor.pdf",
                    "file_hash": source_hash,
                    "items": [
                        {
                            "line_no": 17,
                            "product_code": "B1-P1",
                            "product_name": "共享规格印刷1",
                            "quantity": 10,
                            "unit_price": "1.00",
                        }
                    ],
                    "recognition_status": "recognized",
                    "customer_route": {"status": "locked"},
                    "customer_match_status": "matched",
                    "integrity_check": {"integrity_status": "passed"},
                    "matched_customer_id": 1,
                },
                admin,
            )
        admin_payload = deepcopy(sales_payload)
        admin_payload["idempotency_key"] = "t02-cross-actor-admin"
        admin_payload["pdf_import_confirmation"]["preview_safety_token"] = admin_token
        login(client, "admin")
        replay = client.post("/api/orders", json=admin_payload)

        with factory() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            other_customer_token = _encode_pdf_preview_safety_token(
                {
                    "source_name": "cross-customer.pdf",
                    "file_hash": source_hash,
                    "items": [{"line_no": 1, "quantity": 10, "unit_price": "1.00"}],
                    "recognition_status": "recognized",
                    "customer_route": {"status": "locked"},
                    "customer_match_status": "matched",
                    "integrity_check": {"integrity_status": "passed"},
                    "matched_customer_id": 2,
                },
                admin,
            )
        other_customer = deepcopy(admin_payload)
        other_customer["idempotency_key"] = "t02-cross-customer"
        other_customer["customer_id"] = 2
        other_customer["pdf_import_confirmation"][
            "preview_safety_token"
        ] = other_customer_token
        rejected = client.post("/api/orders", json=other_customer)

    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]
    assert rejected.status_code == 409
    assert "其他客户" in rejected.text


def test_same_po_without_a_new_trusted_source_keeps_duplicate_guard(b1_app) -> None:
    app, factory = b1_app
    _ready_product(factory)
    with TestClient(app) as client:
        login(client, "sales")
        imported = client.post(
            "/api/orders", json=_payload(app, "d" * 64, "t02-imported-key")
        )
        untrusted = {
            "customer_id": 1,
            "customer_po": "T02-SAME-PO",
            "idempotency_key": "t02-no-source-key",
            "items": [
                {
                    "client_line_id": "plain-line",
                    "product_id": 1,
                    "quantity": 10,
                    "unit_price": "1.00",
                    "product_code": "B1-P1",
                    "product_name": "共享规格印刷1",
                }
            ],
        }
        rejected = client.post("/api/orders", json=untrusted)

    assert imported.status_code == 201, imported.text
    assert rejected.status_code == 409
    assert "相同客户、客户单号和明细" in rejected.text


def test_two_source_lines_persist_and_second_line_failure_is_atomic(b1_app) -> None:
    app, factory = b1_app
    _ready_product(factory)
    preview_items = [
        {
            "line_no": line_no,
            "source_page": page,
            "product_code": f"B1-P{product_id}",
            "product_name": f"共享规格印刷{product_id}",
            "quantity": 10,
            "unit_price": "1.00",
        }
        for product_id, line_no, page in ((1, 10, 1), (2, 20, 3))
    ]

    def two_line_payload(source_hash: str, key: str, second_product_id: int) -> dict:
        payload = _payload(app, source_hash, key)
        payload["items"] = [
            {
                **order_item(product_id, 10, line=f"pdf-line-{index}"),
                "product_code": f"B1-P{index}",
                "product_name": f"共享规格印刷{index}",
            }
            for index, product_id in enumerate((1, second_product_id), start=1)
        ]
        payload["pdf_import_confirmation"][
            "preview_safety_token"
        ] = _signed_pdf_preview_token(
            app,
            source_hash=source_hash,
            source_name=f"{source_hash[:4]}.pdf",
            items=preview_items,
        )
        return payload

    with TestClient(app) as client:
        login(client, "sales")
        failed = client.post(
            "/api/orders",
            json=two_line_payload("6" * 64, "t02-second-line-failure", 999999),
        )
        assert failed.status_code == 400
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order)) == 0
            assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 0
        saved = client.post(
            "/api/orders",
            json=two_line_payload("7" * 64, "t02-two-lines", 2),
        )

    assert saved.status_code == 201, saved.text
    with factory() as db:
        lines = list(
            db.scalars(
                select(OrderImportSourceLine).order_by(
                    OrderImportSourceLine.source_position
                )
            )
        )
        assert [(line.source_line_label, line.source_page) for line in lines] == [
            ("10", 1),
            ("20", 3),
        ]


def test_import_source_and_order_roll_back_together_when_audit_fails(
    b1_app, monkeypatch
) -> None:
    app, factory = b1_app
    _ready_product(factory)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.api.orders._append_order_audit", fail_audit)
    with TestClient(app) as client:
        login(client, "sales")
        response = client.post(
            "/api/orders", json=_payload(app, "e" * 64, "t02-audit-failure")
        )

    assert response.status_code == 500
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 0
        assert db.scalar(select(func.count()).select_from(OrderImportSourceLine)) == 0
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.scalar(
            select(func.count()).select_from(OperationLog).where(
                OperationLog.action == "order_create_replay"
            )
        ) == 0


def test_concurrent_same_source_creates_one_order_and_returns_one_identity(b1_app) -> None:
    app, factory = b1_app
    _ready_product(factory)
    first = _payload(app, "f" * 64, "t02-concurrent-a")
    second = deepcopy(first)
    second["idempotency_key"] = "t02-concurrent-b"

    def submit(payload: dict) -> tuple[int, dict]:
        with TestClient(app) as client:
            login(client, "sales")
            response = client.post("/api/orders", json=payload)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, (first, second)))

    assert [status for status, _body in responses] == [201, 201]
    assert len({body["id"] for _status, body in responses}) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 1
        assert db.scalar(select(func.count()).select_from(Order)) == 1

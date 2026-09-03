from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _freeze_receipt_fact,
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from tests.test_phase11_requisition import _login, requisition_app


@pytest.fixture(autouse=True)
def _published_map_identity(monkeypatch) -> None:
    _use_p181_published_map_identity(monkeypatch)


def test_batch_replay_rejects_response_after_cost_permission_is_revoked(
    requisition_app,
) -> None:
    from app.models.access_control import UserPermissionOverride
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.purchase_receipt import IncomingReceiptBatchFact
    from app.models.user import User

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="batch-replay-permission-price",
        )
        assert frozen.status_code == 200, frozen.text
        receipt_fact = frozen.json()
        with session_factory() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            workshop = session.scalar(
                select(User).where(User.username == "workshop")
            )
            assert admin is not None and workshop is not None
            session.add(
                UserPermissionOverride(
                    user_id=workshop.id,
                    permission_code="cost.view",
                    is_allowed=True,
                    granted_by=admin.id,
                )
            )
            session.commit()

        _login(client, "workshop")
        current = client.get("/api/auth/me")
        assert current.status_code == 200, current.text
        assert "cost.view" in current.json()["permissions"]
        payload = {
            "idempotency_key": "batch-replay-permission-receive",
            "items": [
                {
                    "item_id": source.route_key,
                    "received_quantity": 10,
                    "idempotency_key": "batch-replay-permission-receive:line-1",
                    "expected_receipt_fact_version": receipt_fact[
                        "receipt_fact_version"
                    ],
                    "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
                    "expected_purpose_snapshot_version": (
                        source.purpose_snapshot_version
                    ),
                    "receipt_plan_fingerprint": receipt_fact[
                        "receipt_plan_fingerprint"
                    ],
                    "expected_actual_material_version": receipt_fact[
                        "actual_material_version"
                    ],
                    "actual_material_fingerprint": receipt_fact[
                        "actual_material_fingerprint"
                    ],
                }
            ],
        }

        first = client.put("/api/incoming/batch-receive", json=payload)
        assert first.status_code == 200, first.text
        allocation = first.json()["results"][0]["item"]["purpose_allocation"]
        assert {
            "sheet_cost",
            "order_cost",
            "reserve_cost",
            "receipt_total_cost",
        }.issubset(allocation)

        same_access_replay = client.put("/api/incoming/batch-receive", json=payload)
        assert same_access_replay.status_code == 200, same_access_replay.text
        assert same_access_replay.json() == first.json()

        with session_factory() as session:
            workshop = session.scalar(
                select(User).where(User.username == "workshop")
            )
            assert workshop is not None
            override = session.scalar(
                select(UserPermissionOverride).where(
                    UserPermissionOverride.user_id == workshop.id,
                    UserPermissionOverride.permission_code == "cost.view",
                )
            )
            assert override is not None
            override.is_allowed = False
            workshop.auth_version += 1
            session.commit()

        _login(client, "workshop")
        current = client.get("/api/auth/me")
        assert current.status_code == 200, current.text
        assert "cost.view" not in current.json()["permissions"]

        revoked_replay = client.put("/api/incoming/batch-receive", json=payload)
        assert revoked_replay.status_code == 409, revoked_replay.text
        assert revoked_replay.json()["detail"]["code"] == (
            "INCOMING_BATCH_IDEMPOTENCY_CONFLICT"
        )
        serialized_error = json.dumps(revoked_replay.json(), ensure_ascii=False)
        for sensitive_key in (
            "sheet_cost",
            "order_cost",
            "reserve_cost",
            "receipt_total_cost",
        ):
            assert sensitive_key not in serialized_error

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IncomingReceiptItem)) == 1
        assert session.scalar(
            select(func.count()).select_from(IncomingReceiptBatchFact)
        ) == 1

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_p1_81_receipt_purpose_flow import (
    FrozenSource,
    _add_changed_material,
    _create_frozen_sources,
    _error_code,
    _login,
    _receive,
    _seed_material_and_staging,
)
from tests.test_phase11_requisition import requisition_app


def _auto_freeze(
    client: TestClient,
    source: FrozenSource,
    *,
    expected_latest_version: int,
    idempotency_key: str,
    actual_material_id: int | None = None,
    material_variance_approval_id: int | None = None,
):
    payload = {
        "actual_material_id": actual_material_id or source.material_id,
        "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
        "purpose_snapshot_version": source.purpose_snapshot_version,
        "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
        "expected_source_version": source.source_version,
        "expected_latest_receipt_fact_version": expected_latest_version,
        "idempotency_key": idempotency_key,
    }
    if material_variance_approval_id is not None:
        payload["material_variance_approval_id"] = material_variance_approval_id
    return client.put(
        "/api/requisition/purchase-sources/"
        f"{quote(source.source_key, safe='')}/receipt-facts/auto",
        json=payload,
    )


def _update_material_contract(
    session_factory,
    material_id: int,
    *,
    unit_price: str,
    currency: str = "CNY",
    tax_included: bool = True,
    tax_rate: str = "0.13",
) -> None:
    from app.models.material import Material

    with session_factory() as session:
        material = session.get(Material, material_id)
        assert material is not None
        material.quote_price = Decimal(unit_price)
        material.purchase_currency = currency
        material.purchase_tax_included = tax_included
        material.purchase_tax_rate = Decimal(tax_rate)
        material.version += 1
        session.commit()


def test_auto_freeze_rolls_current_master_contract_forward_without_rewriting_history(
    requisition_app,
) -> None:
    from app.models.purchase_receipt import (
        IncomingReceiptPurposeAllocation,
        PurchaseReceiptFact,
    )

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

        _login(client, "workshop")
        first_response = _auto_freeze(
            client,
            source,
            expected_latest_version=0,
            idempotency_key="p022-auto-first-contract",
        )
        assert first_response.status_code == 200, first_response.text
        first_fact = first_response.json()
        assert first_fact["receipt_fact_version"] == 1
        assert first_fact["unit_price"] == "99.9900"
        first_receipt = _receive(
            client,
            source,
            first_fact,
            quantity=4,
            idempotency_key="p022-receive-first-contract",
        )
        assert first_receipt.status_code == 200, first_receipt.text

        _update_material_contract(
            session_factory,
            source.material_id,
            unit_price="108.8800",
            currency="USD",
            tax_included=False,
            tax_rate="0.06",
        )
        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        pending_row = next(
            row
            for row in pending.json()["items"]
            if str(row["item_id"]) == source.route_key
        )
        assert pending_row["receipt_fact_ready"] is False
        assert pending_row["latest_receipt_fact_version"] == 1
        assert pending_row["expected_receipt_fact_version"] is None
        assert pending_row["receipt_execution_ready"] is True
        second_response = _auto_freeze(
            client,
            source,
            expected_latest_version=1,
            idempotency_key="p022-auto-second-contract",
        )
        assert second_response.status_code == 200, second_response.text
        second_fact = second_response.json()
        assert second_fact["receipt_fact_version"] == 2
        assert second_fact["unit_price"] == "108.8800"
        assert second_fact["currency"] == "USD"
        assert second_fact["tax_included"] is False
        assert second_fact["tax_rate"] == "0.0600"
        second_receipt = _receive(
            client,
            source,
            second_fact,
            quantity=6,
            idempotency_key="p022-receive-second-contract",
        )
        assert second_receipt.status_code == 200, second_receipt.text

    with session_factory() as session:
        facts = list(
            session.scalars(
                select(PurchaseReceiptFact)
                .where(
                    PurchaseReceiptFact.purchase_purpose_source_snapshot_id
                    == source.purpose_snapshot_id
                )
                .order_by(PurchaseReceiptFact.receipt_fact_version)
            )
        )
        assert len(facts) == 2
        assert Decimal(facts[0].unit_price) == Decimal("99.990000")
        assert facts[0].currency == "CNY"
        assert Decimal(facts[1].unit_price) == Decimal("108.880000")
        assert facts[1].currency == "USD"
        allocations = list(
            session.scalars(
                select(IncomingReceiptPurposeAllocation)
                .where(
                    IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id
                    == source.purpose_snapshot_id
                )
                .order_by(IncomingReceiptPurposeAllocation.id)
            )
        )
        assert [row.receipt_total_sheet_qty for row in allocations] == [4, 6]
        assert [row.purchase_receipt_fact_id for row in allocations] == [
            facts[0].id,
            facts[1].id,
        ]


def test_auto_freeze_rejects_stale_latest_version_after_concurrent_rollforward(
    requisition_app,
) -> None:
    from app.models.purchase_receipt import PurchaseReceiptFact

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
        _login(client, "workshop")
        first = _auto_freeze(
            client,
            source,
            expected_latest_version=0,
            idempotency_key="p022-concurrent-first",
        )
        assert first.status_code == 200, first.text

        _update_material_contract(
            session_factory,
            source.material_id,
            unit_price="101.0100",
        )
        winner = _auto_freeze(
            client,
            source,
            expected_latest_version=1,
            idempotency_key="p022-concurrent-winner",
        )
        assert winner.status_code == 200, winner.text
        assert winner.json()["receipt_fact_version"] == 2

        replay = _auto_freeze(
            client,
            source,
            expected_latest_version=1,
            idempotency_key="p022-concurrent-winner",
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt_fact_id"] == winner.json()["receipt_fact_id"]
        assert replay.json()["receipt_fact_version"] == 2

        stale = _auto_freeze(
            client,
            source,
            expected_latest_version=1,
            idempotency_key="p022-concurrent-stale",
        )
        assert stale.status_code == 409, stale.text
        assert _error_code(stale) == "PURCHASE_RECEIPT_FACT_STALE"

    with session_factory() as session:
        fact_count = session.scalar(
            select(func.count(PurchaseReceiptFact.id)).where(
                PurchaseReceiptFact.purchase_purpose_source_snapshot_id
                == source.purpose_snapshot_id
            )
        )
        assert fact_count == 2


def test_auto_freeze_rollforward_still_rejects_unapproved_material_change(
    requisition_app,
) -> None:
    from app.models.purchase_receipt import PurchaseReceiptFact

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    changed_material_id = _add_changed_material(session_factory)
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
        _login(client, "workshop")
        first = _auto_freeze(
            client,
            source,
            expected_latest_version=0,
            idempotency_key="p022-material-change-first",
        )
        assert first.status_code == 200, first.text

        rejected = _auto_freeze(
            client,
            source,
            expected_latest_version=1,
            idempotency_key="p022-material-change-unapproved",
            actual_material_id=changed_material_id,
        )
        assert rejected.status_code == 409, rejected.text
        assert _error_code(rejected) == "ACTUAL_MATERIAL_CONFIRMATION_REQUIRED"

    with session_factory() as session:
        fact_count = session.scalar(
            select(func.count(PurchaseReceiptFact.id)).where(
                PurchaseReceiptFact.purchase_purpose_source_snapshot_id
                == source.purpose_snapshot_id
            )
        )
        assert fact_count == 1


def test_auto_freeze_missing_active_material_master_returns_chinese_without_writing(
    requisition_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.material import Material
    from app.models.purchase_receipt import PurchaseReceiptFact

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
        with session_factory() as session:
            material = session.get(Material, source.material_id)
            assert material is not None
            material.is_active = False
            session.commit()

        _login(client, "workshop")
        rejected = _auto_freeze(
            client,
            source,
            expected_latest_version=0,
            idempotency_key="p022-missing-active-material-master",
        )

        assert rejected.status_code == 409, rejected.text
        detail = rejected.json()["detail"]
        assert detail == {
            "code": "MATERIAL_MASTER_PRICE_REQUIRED",
            "message": "正常收料前必须匹配启用中的材质主档，请先核对报料材质。",
        }
        assert "Material master match is required" not in rejected.text

    with session_factory() as session:
        fact_count = session.scalar(
            select(func.count(PurchaseReceiptFact.id)).where(
                PurchaseReceiptFact.purchase_purpose_source_snapshot_id
                == source.purpose_snapshot_id
            )
        )
        audit_count = session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action
                == "AUTO_CONFIRM_PURCHASE_RECEIPT_FACT_FROM_MATERIAL_MASTER"
            )
        )
        assert fact_count == 0
        assert audit_count == 0

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_p1_81_receipt_purpose_flow import (
    _add_changed_material,
    _create_frozen_source_batch,
    _create_frozen_sources,
    _freeze_receipt_fact,
    _login,
    _receive,
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from tests.test_phase11_requisition import requisition_app


@pytest.fixture(autouse=True)
def _p191_published_map_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_p181_published_map_identity(monkeypatch)


def _approve_material_change(
    client: TestClient,
    source,
    *,
    changed_material_id: int,
    key: str,
) -> dict:
    _login(client, "workshop")
    requested = client.put(
        "/api/requisition/purchase-sources/"
        f"{quote(source.source_key, safe='')}/material-variances",
        json={
            "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
            "purpose_snapshot_version": source.purpose_snapshot_version,
            "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
            "expected_source_version": source.source_version,
            "actual_material_id": changed_material_id,
            "reason": "匿名供应商本次实际到货材质不同",
            "idempotency_key": f"{key}-request",
        },
    )
    assert requested.status_code == 200, requested.text
    _login(client, "admin")
    approved = client.put(
        "/api/requisition/purchase-material-variances/"
        f"{requested.json()['material_variance_id']}/confirm",
        json={"idempotency_key": f"{key}-approve"},
    )
    assert approved.status_code == 200, approved.text
    _login(client, "workshop")
    frozen = client.put(
        "/api/requisition/purchase-sources/"
        f"{quote(source.source_key, safe='')}/receipt-facts/auto",
        json={
            "actual_material_id": changed_material_id,
            "material_variance_approval_id": approved.json()[
                "material_variance_approval_id"
            ],
            "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
            "purpose_snapshot_version": source.purpose_snapshot_version,
            "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
            "expected_source_version": source.source_version,
            "expected_latest_receipt_fact_version": 0,
            "idempotency_key": f"{key}-freeze",
        },
    )
    assert frozen.status_code == 200, frozen.text
    return frozen.json()


def test_actual_material_projects_to_receipt_history_and_card_without_overwriting_purchase(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    app, session_factory = requisition_app
    original_material_id = _seed_material_and_staging(session_factory)
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
        frozen = _approve_material_change(
            client,
            source,
            changed_material_id=changed_material_id,
            key="p191-actual-material",
        )
        received = _receive(
            client,
            source,
            frozen,
            quantity=10,
            idempotency_key="p191-actual-material-receive",
        )
        assert received.status_code == 200, received.text
        receipt_item_id = int(received.json()["receipt_item_id"])

        recent = client.get("/api/incoming/received", params={"page": 1, "page_size": 1})
        assert recent.status_code == 200, recent.text
        assert recent.json()["total"] == 1
        row = recent.json()["items"][0]
        assert row["receipt_item_id"] == receipt_item_id
        assert row["reported_material_code"] == "KAKAK"
        assert row["actual_material_code"] == "ABABA"
        assert row["actual_material_display"] == "ABABA / AB"
        assert row["material_changed"] is True

        history = client.get("/api/incoming/history", params={"page": 1, "page_size": 1})
        assert history.status_code == 200, history.text
        history_row = history.json()["items"][0]
        assert history_row["actual_material_code"] == "ABABA"
        assert history_row["reported_material_code"] == "KAKAK"

        card = client.get(
            f"/api/incoming/receipt-items/{receipt_item_id}/production-card"
        )
        assert card.status_code == 200, card.text
        component = card.json()["cards"][0]["components"][0]
        assert component["material_code"] == "ABABA"
        assert component["flute_type"] == "AB"
        assert component["reported_material_code"] == "KAKAK"
        assert card.json()["actual_material_code"] == "ABABA"

    with session_factory() as session:
        supplier_item = session.get(SupplierRequisitionOrderItem, source.supplier_item_id)
        order_item = session.get(OrderItem, 1)
        assert supplier_item is not None and order_item is not None
        assert supplier_item.material_id == original_material_id
        assert supplier_item.material_code_snapshot == "KAKAK"
        assert order_item.snapshot_material == "KAKAK"


def test_received_material_projection_rejects_null_and_combined_flute_display(
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
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p191-normalized-material-fact",
        )
        assert frozen.status_code == 200, frozen.text
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=10,
            idempotency_key="p191-normalized-material-receive",
        )
        assert received.status_code == 200, received.text

        with session_factory() as session:
            fact = session.scalar(
                select(PurchaseReceiptFact).where(
                    PurchaseReceiptFact.supplier_requisition_order_item_id
                    == source.supplier_item_id
                )
            )
            assert fact is not None
            fact.expected_material_code_snapshot = "N717N-AB/EB"
            fact.actual_material_code_snapshot = "N717N-AB/EB"
            fact.actual_material_layer_count_snapshot = 5
            fact.actual_material_flute_type_snapshot = "AB/BE"
            session.commit()

        recent = client.get("/api/incoming/received", params={"page": 1, "page_size": 1})
        assert recent.status_code == 200, recent.text
        recent_row = recent.json()["items"][0]
        assert recent_row["reported_material_code"] == "N717N"
        assert recent_row["actual_material_code"] == "N717N"
        assert recent_row["actual_material_flute_type"] == "AB"
        assert recent_row["actual_material_display"] == "N717N / AB"
        assert "AB/BE" not in recent_row["actual_material_display"]
        assert "None" not in recent_row["actual_material_display"]

        with session_factory() as session:
            fact = session.scalar(
                select(PurchaseReceiptFact).where(
                    PurchaseReceiptFact.supplier_requisition_order_item_id
                    == source.supplier_item_id
                )
            )
            assert fact is not None
            fact.actual_material_flute_type_snapshot = None
            session.commit()

        history = client.get("/api/incoming/history", params={"page": 1, "page_size": 1})
        assert history.status_code == 200, history.text
        history_row = history.json()["items"][0]
        assert history_row["actual_material_flute_type"] == "AB"
        assert history_row["actual_material_display"] == "N717N / AB"
        assert "None" not in history_row["actual_material_display"]


def test_pending_purchase_order_is_projected_as_one_row_per_product_detail(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_source_batch(client, session_factory, count=2)
        response = client.get("/api/incoming/pending", params={"page": 1, "page_size": 12})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == len(sources) == 2
    rows = payload["items"]
    assert len(rows) == 2
    assert len({row["item_id"] for row in rows}) == 2
    assert {row["supplier_order_item_id"] for row in rows} == {
        source.supplier_item_id for source in sources
    }
    assert all(str(row.get("product_code") or "").strip() for row in rows)
    assert all(str(row.get("product_name") or "").strip() for row in rows)
    assert all("requisition_qty" in row for row in rows)


def test_selected_receipt_cards_are_one_fail_closed_half_a4_batch_with_plan_warning(
    requisition_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_source_batch(client, session_factory, count=2)
        with session_factory() as session:
            supplier_item = session.get(
                SupplierRequisitionOrderItem, sources[0].supplier_item_id
            )
            assert supplier_item is not None
            supplier_order_id = int(supplier_item.supplier_order_id)

        plan = client.get(
            f"/api/requisition/supplier-orders/{supplier_order_id}/production-print-package"
        )
        assert plan.status_code == 200, plan.text
        first_plan_card = next(
            row
            for row in plan.json()["cards"]
            if row["supplier_order_item_id"] == sources[0].supplier_item_id
        )
        with session_factory() as session:
            session.add(
                OperationLog(
                    action="PREPARE_PRODUCTION_PRINT",
                    resource="ProductionPrintBatch",
                    details=json.dumps(
                        {
                            "items": [
                                {
                                    "supplier_order_id": supplier_order_id,
                                    "source_identity": first_plan_card[
                                        "source_identity"
                                    ],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    event_category="business",
                    result="success",
                    source="web",
                    module_code="requisition",
                    action_code="requisition.production_print_batch.prepared",
                    schema_version=1,
                )
            )
            session.commit()

        receipt_ids: list[int] = []
        for index, source in enumerate(sources, start=1):
            frozen = _freeze_receipt_fact(
                client,
                source,
                idempotency_key=f"p191-batch-fact-{index}",
            )
            assert frozen.status_code == 200, frozen.text
            received = _receive(
                client,
                source,
                frozen.json(),
                quantity=10,
                idempotency_key=f"p191-batch-receive-{index}",
            )
            assert received.status_code == 200, received.text
            receipt_ids.append(int(received.json()["receipt_item_id"]))

        batch = client.get(
            "/api/incoming/production-card-batch",
            params=[("receipt_item_id", value) for value in receipt_ids],
        )
        assert batch.status_code == 200, batch.text
        payload = batch.json()
        assert payload["receipt_item_ids"] == receipt_ids
        assert payload["card_count"] == 2
        assert payload["page_count"] == 1
        assert payload["pages"][0]["top"] is not None
        assert payload["pages"][0]["bottom"] is not None
        assert len(payload["planned_print_warnings"]) == 1
        assert payload["planned_print_warnings"][0]["receipt_item_id"] == receipt_ids[0]

        duplicate = client.get(
            "/api/incoming/production-card-batch",
            params=[("receipt_item_id", receipt_ids[0]), ("receipt_item_id", receipt_ids[0])],
        )
        assert duplicate.status_code == 422, duplicate.text

        invalid = client.get(
            "/api/incoming/production-card-batch",
            params=[("receipt_item_id", receipt_ids[0]), ("receipt_item_id", 999999)],
        )
        assert invalid.status_code == 409, invalid.text
        assert invalid.json()["detail"]["code"] == "incoming_production_card_batch_invalid"
        assert invalid.json()["detail"]["invalid_items"][0]["receipt_item_id"] == 999999


def test_compact_incoming_frontend_uses_top_selection_and_no_row_print_buttons() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    index = (root / "static" / "index.html").read_text(encoding="utf-8")
    print_page = (root / "static" / "requisition-production-print.html").read_text(
        encoding="utf-8"
    )

    incoming = index[index.index('<template v-else-if="activePage === \'incoming\'">') :]
    incoming = incoming[: incoming.index('<template v-else-if="activePage === \'production\'">')]
    assert "incoming-compact-head" in incoming
    assert 'desc="老板端与车间共用' not in incoming
    assert "打印产品标签（{{ incomingProductionCardSelectedCount }}）" in incoming
    assert '@click="openSelectedIncomingProductLabels"' in incoming
    assert '@click="openIncomingProductionCard(row)"' not in incoming
    assert "报料长" in incoming and "报料宽" in incoming and "压线尺寸" in incoming
    assert "客户简称 / 存货编码 / 产品名称" in incoming
    assert "incomingDimensionMm(row.cardboard_len)" in incoming
    assert "incomingDimensionMm(row.cardboard_width)" in incoming
    assert 'class="incoming-product-identity-line"' in incoming
    assert '<div class="incoming-product-name">{{ row.product_name || \'-\' }}</div>' in incoming
    assert 'class="incoming-history-action-line"' in incoming
    assert "incomingTab==='pending' ? '本次实收' : '实收数量'" in incoming
    assert "撤销收料，回到已报料" not in incoming
    assert ".table-wrap:has(> .incoming-compact-table)" in index
    assert "max-height:none; overflow-x:hidden; overflow-y:visible" in index
    assert "receipt_item_ids" in print_page
    assert "/api/incoming/production-card-batch" in print_page
    assert 'return this.loadIncomingReceived({force:true})' in index
    assert "return this.loadIncomingHistory()" in index
    assert "incomingCleanDisplayToken(value)" in index
    assert "incomingDisplayFlute(value, fallbackValue)" in index
    assert ".incoming-history-action-line" in index

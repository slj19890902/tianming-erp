"""The first homepage read must not wait for eligibility or imply zero work."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.test_erp3_backend_read_paths import erp3_read_app, _read
from tests.test_n028_dashboard_permissions import _seed_dashboard_data
from tests.test_phase3_scale_reliability import _admin


@pytest.mark.parametrize("role", ["admin", "boss"])
def test_summary_does_not_read_heavy_sources_or_return_false_zeroes(erp3_read_app, monkeypatch, role):
    from app.api import dashboard, incoming, requisition, deliveries, finance
    from app.services import production_workflow

    app, engine, customer_id, _, _ = erp3_read_app
    actor = _admin()
    actor.role = role
    app.dependency_overrides[dashboard.can_read] = lambda: actor

    def forbidden(*args, **kwargs):
        pytest.fail("summary must not compute module eligibility or inventory")

    for module, names in (
        (dashboard, ["_workflow_projection_rows", "_dashboard_kpi_payload", "build_inventory_insights", "_common_box_low_stock_warnings"]),
        (incoming, ["dashboard_pending_incoming_rows"]),
        (requisition, ["dashboard_pending_requisition_rows"]),
        (deliveries, ["pending_delivery_customer_summaries"]),
        (finance, ["pending_statement_customer_summaries", "project_customer_months"]),
        (production_workflow, ["list_production_task_dashboard_rows"]),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    with TestClient(app) as client:
        payload, statements, _ = _read(client, engine, "/api/dashboard/overview", mode="summary")
    assert payload["mode"] == "summary"
    assert payload["available_groups"] == ["stock", "production", "delivery", "finance", "approval"]
    assert payload["workbench"]["tasks"] == []
    assert customer_id in {c["id"] for c in payload["workbench"]["customers"]}
    assert "kpi" not in payload and "summary" not in payload
    assert payload["cards"] and all(c["count"] is None for c in payload["cards"])
    assert all(c[field] is None for c in payload["cards"] for field in ("amount", "waiting_count", "due_waiting_count") if field in c)
    assert len(statements) <= 2
    assert all("from customers" in statement for statement, _ in statements)


def test_sections_union_preserves_full_cards_tasks_and_attention(erp3_read_app, monkeypatch):
    from app.api import dashboard
    from app.models.business_approval import BusinessApproval
    from app.models.home_task import HomeTaskPreference

    app, engine, customer_id, _, _ = erp3_read_app
    monkeypatch.setattr(dashboard, "_common_box_low_stock_warnings", lambda _db, _scope: [
        {"customer_id": customer_id, "customer_name": "Synthetic", "policy_id": 8},
    ])
    with Session(engine) as db:
        db.add(_admin())
        db.flush()
        db.add(BusinessApproval(applicant_id=-1, customer_id=customer_id, action="stock_replenishment",
            payload_json='{"items":[{"stock_policy_id":8}]}', basis_hash="a" * 64, request_hash="b" * 64,
            idempotency_key="erp3-section-approval", created_at=datetime(2026, 10, 1)))
        db.commit()
    with TestClient(app) as client:
        first, _, _ = _read(client, engine, "/api/dashboard/overview")
        task = next(t for t in first["workbench"]["tasks"] if t["key"] != "approval")
        with Session(engine) as db:
            db.add(HomeTaskPreference(scope_key="user:-1", task_key=task["id"],
                source_hash=task["source_hash"], state="hidden", reason="", version=1,
                customer_ids_json=f"[{customer_id}]", actor_id=-1, updated_at=datetime(2026, 10, 22)))
            db.commit()
        full, _, _ = _read(client, engine, "/api/dashboard/overview")
        explicit, _, _ = _read(client, engine, "/api/dashboard/overview", mode="full")
        assert full == explicit
        results = [_read(client, engine, "/api/dashboard/sections", group=group)[0]
                   for group in ("stock", "production", "delivery", "finance", "approval")]
    assert sorted((c for r in results for c in r["cards"]), key=lambda c: c["key"]) == sorted(full["cards"], key=lambda c: c["key"])
    assert sorted((t for r in results for t in r["workbench"]["tasks"]), key=lambda t: t["id"]) == sorted(full["workbench"]["tasks"], key=lambda t: t["id"])
    assert results[0]["low_stock_warnings"] == full.get("low_stock_warnings", [])
    assert results[0]["low_stock_warnings"][0]["pending_request_ids"]
    assert results[0]["workbench"]["tasks"] == []
    assert all(t["key"] != "approval" for r in results[:-1] for t in r["workbench"]["tasks"])
    assert [t["attention_state"] for r in results for t in r["workbench"]["tasks"] if t["id"] == task["id"]] == ["hidden"]


def test_production_section_never_reads_other_groups_and_failure_stays_failure(erp3_read_app, monkeypatch):
    from app.api import dashboard, deliveries, finance
    from app.services import dashboard_workbench

    app, engine, _, _, _ = erp3_read_app

    def forbidden(*args, **kwargs):
        pytest.fail("section must not query unrelated sources")

    for module, names in (
        (dashboard, ["_workflow_projection_rows", "_dashboard_kpi_payload", "build_inventory_insights", "_common_box_low_stock_warnings"]),
        (deliveries, ["pending_delivery_customer_summaries"]),
        (finance, ["pending_statement_customer_summaries", "project_customer_months"]),
        (dashboard_workbench, ["_pending_requests"]),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    with TestClient(app) as client:
        payload, statements, _ = _read(client, engine, "/api/dashboard/sections", group="production")
    assert {c["key"] for c in payload["cards"]} == {"pending_material", "pending_incoming", "pending_production"}
    assert "low_stock_warnings" not in payload
    assert not any("from finance_" in statement for statement, _ in statements)

    def failed(*args, **kwargs):
        raise RuntimeError("synthetic projection failure")

    monkeypatch.setattr(dashboard, "_authoritative_dashboard_data", failed)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/dashboard/sections", params={"group": "production"}).status_code == 500


def test_sections_and_summary_obey_real_permission_overrides_and_scope(tmp_path):
    from app.api.dashboard import dashboard_overview, dashboard_section
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User
    from fastapi import HTTPException

    engine = create_sqlite_engine(tmp_path / "section-scope.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    ids = _seed_dashboard_data(factory)
    try:
        with factory() as db:
            finance = db.get(User, ids["finance"])
            summary = dashboard_overview(db, finance, mode="summary")
            assert {c["id"] for c in summary["workbench"]["customers"]} == {ids["allowed_customer"]}
            full = dashboard_overview(db, finance)
            detail = dashboard_section(group="finance", db=db, user=finance)
            assert detail["cards"] == [c for c in full["cards"] if c["key"].startswith(("pending_reconciliation", "pending_invoice", "pending_payment"))]
            assert all(t["customer_id"] == ids["allowed_customer"] for t in detail["workbench"]["tasks"])
            sales = db.get(User, ids["sales"])
            with pytest.raises(HTTPException) as denied:
                dashboard_section(group="finance", db=db, user=sales)
            assert denied.value.status_code == 403
            empty = db.get(User, ids["empty_sales"])
            assert dashboard_overview(db, empty, mode="summary")["workbench"]["customers"] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("path,params", [
    ("/api/dashboard/overview", {"mode": "invalid"}),
    ("/api/dashboard/sections", {"group": "priority"}),
    ("/api/dashboard/sections", {}),
])
def test_invalid_summary_contract_is_rejected(erp3_read_app, path, params):
    app, _, _, _, _ = erp3_read_app
    with TestClient(app) as client:
        assert client.get(path, params=params).status_code == 422


@pytest.mark.parametrize("path,params", [
    ("pending", {}), ("pending", {"page": 1}),
    ("received", {}), ("received", {"page": 1}),
    ("history", {}),
])
def test_empty_incoming_scope_does_no_business_reads(erp3_read_app, monkeypatch, path, params):
    from app.api import incoming

    app, engine, _, _, _ = erp3_read_app
    monkeypatch.setattr(incoming, "_visible_customer_ids", lambda _user, _db: set())
    with TestClient(app) as client:
        payload, statements, _ = _read(client, engine, f"/api/incoming/{path}", **params)
    assert payload["items"] == []
    assert statements == []


def test_empty_incoming_scope_keeps_invalid_date_and_denied_customer_errors(erp3_read_app, monkeypatch):
    from app.api import incoming

    app, _, customer_id, _, _ = erp3_read_app
    monkeypatch.setattr(incoming, "_visible_customer_ids", lambda _user, _db: set())
    with TestClient(app) as client:
        assert client.get("/api/incoming/history", params={"customer_id": customer_id}).status_code == 403
        assert client.get("/api/incoming/history", params={"date_from": "invalid"}).status_code == 422

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import warehouse
from app.api.deps import get_db
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.user import User


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _insights_fixture() -> dict:
    return {
        "generated_at": "2026-07-17T08:00:00Z",
        "as_of": "2026-07-17",
        "top_secret": "222.22 supplier-secret",
        "summary": {
            "recorded_lots": 2,
            "available_lots": 2,
            "finished_available": 12,
            "semi_finished_available": 8,
            "total_reserved": 1,
            "total_damaged": 0,
            "total_scrapped": 0,
            "estimated_inventory_value": "234.56",
            "actual_inventory_value": "999.99",
            "future_cost_total": "444.44 supplier-secret",
        },
        "data_quality": {
            "active_location_lots": 2,
            "cost_ready_lots": 1,
            "missing_cost_lots": 1,
            "cost_coverage_percent": 50.0,
            "snapshot_estimate_coverage": 50.0,
            "current_quote_coverage": 0.0,
            "product_reference_coverage": 0.0,
            "actual_cost_supported": False,
            "actual_cost_message": "成本来源：快照估算",
            "supplier_cost_source": "supplier-secret estimated_snapshot 444.44",
        },
        "by_type": {
            "finished": {
                "lots": 1,
                "available": 12,
                "reserved": 1,
                "damaged": 0,
                "scrapped": 0,
                "cost_total": "222.22",
            },
            "semi_finished": {
                "lots": 1,
                "available": 8,
                "reserved": 0,
                "damaged": 0,
                "scrapped": 0,
            },
            "future_cost_type": {"supplier": "supplier-secret"},
        },
        "action_item_count": 2,
        "high_priority_action_item_count": 2,
        "action_items": [
            {
                "priority": 0,
                "lot_number": "COST-ONLY-LOT",
                "estimated_unit_cost": "23.45",
                "estimated_value": "234.56",
                "cost_status": "pending",
                "message": "supplier-secret",
                "reasons": [{"code": "cost_pending", "text": "成本待补"}],
            },
            {
                "priority": 1,
                "lot_id": 2,
                "lot_number": "OPERATIONAL-LOT",
                "inventory_type": "finished",
                "status": "active",
                "location_code": "A1-R01",
                "quantity_available": 12,
                "unit": "boxes",
                "age_days": 200,
                "age_basis": "stock_date",
                "last_movement_at": "2026-07-01T08:00:00Z",
                "movement_stagnant_days": 16,
                "covered_demand_quantity": 10,
                "uncovered_demand_quantity": 2,
                "coverage_percent": 83.3,
                "coverage_basis": "finished_available_vs_open_order_demand",
                "estimated_unit_cost": "10.00",
                "estimated_value": "120.00",
                "cost_status": "estimated_snapshot",
                "message": "supplier-secret 222.22",
                "sort_key": "estimated_snapshot 444.44",
                "detail": {
                    "customer_name": "运营客户",
                    "inventory_code": "OP-001",
                    "name": "运营产品",
                    "message": "supplier-secret",
                    "sort_key": "444.44",
                    "assigned_products": [
                        {
                            "product_id": 9,
                            "inventory_code": "OP-009",
                            "name": "运营候选",
                            "supplier_secret": "supplier-secret",
                        }
                    ],
                },
                "demand": {
                    "demand_30": 2,
                    "demand_90": 4,
                    "demand_180": 6,
                    "open_demand": 12,
                    "estimated_value": "222.22",
                },
                "reasons": [
                    {
                        "code": "age_slow",
                        "text": "库龄超过 180 天",
                        "amount": "222.22",
                    },
                    {"code": "cost_pending", "text": "成本待补"},
                    {
                        "code": "future_supplier_cost",
                        "text": "supplier-secret estimated_snapshot 444.44",
                    },
                ],
            },
        ],
        "age_buckets": [
            {
                "key": "181_365",
                "label": "181-365 天",
                "lots": 2,
                "finished_available": 12,
                "semi_finished_available": 8,
                "estimated_value": "444.44",
            }
        ],
        "scope_notice": "仅显示授权库存",
        "recommendation_notice": "只读运营建议",
    }


@pytest.fixture()
def cost_permission_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_sqlite_engine(tmp_path / "warehouse-cost-permissions.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="warehouse-no-cost",
                    password_hash="test",
                    role="workshop",
                    real_name="Warehouse no cost",
                    must_change_password=False,
                ),
                User(
                    username="warehouse-cost",
                    password_hash="test",
                    role="finance",
                    real_name="Warehouse cost",
                    must_change_password=False,
                ),
            ]
        )
        db.commit()
        no_cost_id = db.scalar(
            select(User.id).where(User.username == "warehouse-no-cost")
        )
        cost_id = db.scalar(
            select(User.id).where(User.username == "warehouse-cost")
        )

    app = FastAPI()
    app.include_router(warehouse.router, prefix="/api/warehouse")

    def override_get_db():
        with factory() as db:
            yield db

    current_user_id = {"value": no_cost_id}

    def override_can_read():
        with factory() as db:
            # Keep the ORM user attached through endpoint execution because
            # effective_permissions reads its override relationship.
            yield db.get(User, current_user_id["value"])

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[warehouse.can_read] = override_can_read
    monkeypatch.setattr(warehouse, "build_inventory_insights", lambda *_args, **_kwargs: _insights_fixture())
    return app, current_user_id, cost_id


def test_insights_api_removes_cost_fields_and_cost_only_actions_without_cost_view(
    cost_permission_api,
) -> None:
    app, current_user_id, _cost_id = cost_permission_api
    with TestClient(app) as client:
        response = client.get("/api/warehouse/insights")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "generated_at",
        "as_of",
        "scope_notice",
        "recommendation_notice",
        "summary",
        "data_quality",
        "by_type",
        "age_buckets",
        "action_item_count",
        "high_priority_action_item_count",
        "action_items",
    }
    assert set(payload["summary"]) == {
        "recorded_lots",
        "available_lots",
        "finished_available",
        "semi_finished_available",
        "total_reserved",
        "total_damaged",
        "total_scrapped",
    }
    assert payload["data_quality"] == {"active_location_lots": 2}
    assert set(payload["by_type"]) == {"finished", "semi_finished"}
    assert set(payload["by_type"]["finished"]) == {
        "lots",
        "available",
        "reserved",
        "damaged",
        "scrapped",
    }
    assert set(payload["age_buckets"][0]) == {
        "key",
        "label",
        "lots",
        "finished_available",
        "semi_finished_available",
    }
    assert [item["lot_number"] for item in payload["action_items"]] == [
        "OPERATIONAL-LOT"
    ]
    action = payload["action_items"][0]
    assert set(action).isdisjoint(
        {
            "estimated_unit_cost",
            "estimated_value",
            "cost_status",
            "message",
            "sort_key",
        }
    )
    assert [reason["code"] for reason in action["reasons"]] == ["age_slow"]
    assert action["reasons"] == [
        {"code": "age_slow", "text": "库龄超过 180 天"}
    ]
    assert set(action["detail"]) == {
        "customer_name",
        "inventory_code",
        "name",
        "assigned_products",
    }
    assert action["detail"]["assigned_products"] == [
        {"product_id": 9, "inventory_code": "OP-009", "name": "运营候选"}
    ]
    assert set(action["demand"]) == {
        "demand_30",
        "demand_90",
        "demand_180",
        "open_demand",
    }
    assert payload["action_item_count"] == 1
    assert payload["high_priority_action_item_count"] == 1
    for secret in (
        "222.22",
        "234.56",
        "444.44",
        "999.99",
        "23.45",
        "supplier-secret",
        "成本来源",
        "estimated_snapshot",
        "future_cost_total",
        "supplier_cost_source",
        "sort_key",
    ):
        assert secret not in response.text


def test_insights_api_keeps_full_response_for_cost_view_users(
    cost_permission_api,
) -> None:
    app, current_user_id, cost_id = cost_permission_api
    current_user_id["value"] = cost_id
    with TestClient(app) as client:
        response = client.get("/api/warehouse/insights")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["estimated_inventory_value"] == "234.56"
    assert payload["summary"]["actual_inventory_value"] == "999.99"
    assert payload["data_quality"]["actual_cost_message"] == "成本来源：快照估算"
    assert payload["action_items"][0]["estimated_unit_cost"] == "23.45"
    assert payload["action_items"][0]["cost_status"] == "pending"
    assert payload["top_secret"] == "222.22 supplier-secret"
    assert payload["action_items"][1]["sort_key"] == "estimated_snapshot 444.44"


def test_warehouse_insight_frontend_uses_permission_gate_before_cost_rendering() -> None:
    assert 'id="insightCostNotice" class="notice hidden"' in WAREHOUSE_HTML
    assert 'const showCosts=hasPermission("cost.view");' in WAREHOUSE_HTML
    assert 'if(showCosts){\n        cards.push(' in WAREHOUSE_HTML
    assert '$("insightCostNotice").classList.toggle("hidden",!showCosts);' in WAREHOUSE_HTML
    assert '...(showCosts?["估算来源"]:[])' in WAREHOUSE_HTML
    assert '${showCosts?`<td>${cost}</td>`:""}' in WAREHOUSE_HTML


def test_warehouse_insight_renderer_executes_cost_gate_with_minimal_dom() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the warehouse permission render test"
    match = re.search(
        r"^\s*function renderInsights\(\)\{.*?(?=^\s*async function loadLocations)",
        WAREHOUSE_HTML,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "renderInsights JavaScript function not found"
    safe_payload = warehouse._redact_inventory_insight_costs(_insights_fixture())
    raw_payload = _insights_fixture()
    script = f"""
const elements={{}};
function element(id){{
  if(!elements[id]){{
    const classes=new Set(id==="insightCostNotice"?["hidden"]:[]);
    elements[id]={{
      innerHTML:"",textContent:"",classes,
      classList:{{
        toggle(name,force){{if(force)classes.add(name);else classes.delete(name)}},
        contains(name){{return classes.has(name)}}
      }}
    }};
  }}
  return elements[id];
}}
const $=element;
const labels={{boxes:"个",sheets:"张/片"}};
const h=value=>String(value??"").replace(/[&<>"']/g,char=>({{"&":"&amp;","<":"&lt;",">":"&gt;","\\\"":"&quot;","'":"&#39;"}})[char]);
const state={{user:{{role:"workshop"}},permissions:[],insights:null}};
const hasPermission=code=>state.user?.role==="admin"||state.permissions.includes(code);
{match.group(0)}
function snapshot(){{
  return {{
    summary:$("insightSummary").innerHTML,
    quality:$("insightQuality").innerHTML,
    head:$("insightActionHead").innerHTML,
    actions:$("insightActionBody").innerHTML,
    noticeHidden:$("insightCostNotice").classList.contains("hidden")
  }};
}}
const authFailure={{noticeHidden:$("insightCostNotice").classList.contains("hidden"),summary:$("insightSummary").innerHTML}};
state.insights={json.dumps(safe_payload, ensure_ascii=False)};
renderInsights();
const withoutCost=snapshot();
state.permissions=["cost.view"];
state.insights={json.dumps(raw_payload, ensure_ascii=False)};
renderInsights();
const withCost=snapshot();
console.log(JSON.stringify({{authFailure,withoutCost,withCost}}));
"""
    result = subprocess.run(
        [node, "-e", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered["authFailure"] == {"noticeHidden": True, "summary": ""}
    assert rendered["withoutCost"]["noticeHidden"] is True
    without_cost_text = json.dumps(rendered["withoutCost"], ensure_ascii=False)
    for secret in ("234.56", "999.99", "estimated_snapshot", "supplier-secret"):
        assert secret not in without_cost_text
    assert "估算来源" not in rendered["withoutCost"]["head"]
    assert rendered["withCost"]["noticeHidden"] is False
    assert "234.56" in rendered["withCost"]["summary"]
    assert "估算来源" in rendered["withCost"]["head"]
    assert "120.00" in rendered["withCost"]["actions"]

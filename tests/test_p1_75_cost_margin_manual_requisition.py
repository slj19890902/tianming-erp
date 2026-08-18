from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.models.customer_quote_preference import CustomerQuotePreference
from app.models.order import OrderItem
from app.models.product import Product
from tests.test_p1_03_manual_size_orders import (
    _make_fixture_material_valid_for_manual_a1,
    _manual_payload,
)
from tests.test_phase5_orders import _login, order_api_app
from tests.test_p1_28a_order_material_cost import _FakeSession, _item, _material
from app.services.order_material_cost import estimate_order_item_material_cost


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
ORDERS = (ROOT / "app" / "api" / "orders.py").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    match = re.search(rf"\n\s+(?:async\s+)?{re.escape(name)}\s*\([^\n]*\)\s*\{{", INDEX)
    assert match, name
    start = match.end()
    depth = 1
    quote = None
    escaped = False
    for position in range(start, len(INDEX)):
        char = INDEX[position]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return INDEX[start:position]
    raise AssertionError(f"unterminated method {name}")


def _fixed_material_price(monkeypatch) -> None:
    from app.services import order_material_cost

    def fixed_price(_db, *, material, supplier_name, layer_count, flute_type):
        return {
            "base_price": "2.0000",
            "flute_delta": "0",
            "effective_price": "2.0000",
            "rule_id": None,
            "supplier_name": supplier_name,
            "layer_count": layer_count,
            "flute_type": flute_type,
        }

    monkeypatch.setattr(order_material_cost, "get_effective_material_price", fixed_price)


@pytest.mark.parametrize(
    "box_style",
    [
        "A1/0201 普通开槽箱",
        "满摇盖纸箱",
        "半开槽箱",
        "围板",
        "衬板",
        "隔板",
        "刀卡",
        "模切内盒",
        "异形箱",
    ],
)
def test_all_corrugated_styles_use_frozen_report_dimensions_for_material_cost(
    box_style: str, monkeypatch
) -> None:
    _fixed_material_price(monkeypatch)
    item = _item(quantity=10, report_length=990, report_width=610)
    item.product.box_style = box_style
    # Deliberately unrelated finished dimensions prove that cost never uses
    # product L/W/H as a substitute for the frozen board dimensions.
    item.product.length_mm = Decimal("1")
    item.product.width_mm = Decimal("2")
    item.product.height_mm = Decimal("3")
    result = estimate_order_item_material_cost(_FakeSession(_material(1)), item)
    assert result["material_cost_status"] == "calculated"
    assert Decimal(result["estimated_material_total_cost"]) == Decimal("12.08")
    assert result["material_cost_components"][0]["report_length_mm"] == "990"
    assert result["material_cost_components"][0]["report_width_mm"] == "610"


def test_manual_a1_override_freezes_common_box_order_cost_and_material_margin(
    order_api_app, monkeypatch
) -> None:
    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    _fixed_material_price(monkeypatch)
    payload = _manual_payload(client_line_id="p1-75-manual-a1")
    payload["customer_po"] = "P1-75-A1"
    payload["items"][0].update(
        {
            "report_length_mm": 1800,
            "report_width_mm": 660,
            "crease_type": "压线",
            "crease_left_mm": 180,
            "crease_middle_mm": 300,
            "crease_right_mm": 180,
            "splice_mode": "single",
            "pieces_per_box": 1,
            "flap_mm": 30,
            "default_cutting_mode": "一开一",
        }
    )
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)
        pending = client.get("/api/requisition/pending")
    assert response.status_code == 201, response.text
    assert pending.status_code == 200, pending.text
    row = response.json()["items"][0]
    assert row["snapshot_report_length_mm"] == 1800
    assert row["snapshot_report_width_mm"] == 660
    assert row["snapshot_crease_left_mm"] == row["snapshot_crease_right_mm"] == 180
    assert row["material_cost_is_current_estimate"] is False
    assert Decimal(row["estimated_material_total_cost"]) == Decimal("475.20")
    assert Decimal(row["material_sales_amount"]) == Decimal("736.00")
    assert Decimal(row["material_gross_profit"]) == Decimal("260.80")
    assert Decimal(row["material_gross_margin"]) == Decimal("0.3543")
    component = row["material_cost_components"][0]
    assert Decimal(component["area_per_sheet_m2"]) == Decimal("1.188000")
    assert Decimal(component["estimated_sheet_cost"]) == Decimal("2.3760")
    assert component["purchase_sheet_quantity"] == 200
    pending_row = next(
        pending_row
        for pending_row in pending.json()["items"]
        if pending_row["item_id"] == response.json()["items"][0]["id"]
    )
    assert (
        pending_row["snapshot_report_length_mm"],
        pending_row["snapshot_report_width_mm"],
    ) == (1800, 660)
    assert (
        pending_row["snapshot_crease_left_mm"],
        pending_row["snapshot_crease_middle_mm"],
        pending_row["snapshot_crease_right_mm"],
    ) == (180, 300, 180)

    with session_factory() as session:
        item = session.scalar(select(OrderItem))
        product = session.get(Product, item.product_id)
        assert (product.report_length_mm, product.report_width_mm) == (1800, 660)
        assert (product.crease_left_mm, product.crease_middle_mm, product.crease_right_mm) == (180, 300, 180)
        assert (item.snapshot_report_length_mm, item.snapshot_report_width_mm) == (1800, 660)


def test_manual_a3_uses_same_rule_and_freezes_cover_and_base(order_api_app, monkeypatch) -> None:
    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    _fixed_material_price(monkeypatch)
    with session_factory() as session:
        session.add(
            CustomerQuotePreference(
                customer_id=1,
                box_type="A3",
                material_id=1,
                flute_type="AB",
                tax_included_square_price=Decimal("3.2500"),
                is_active=True,
            )
        )
        session.commit()
    payload = _manual_payload(client_line_id="p1-75-manual-a3")
    payload["customer_po"] = "P1-75-A3"
    payload["items"][0]["box_type"] = "A3"
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)
    assert response.status_code == 201, response.text
    row = response.json()["items"][0]
    assert row["snapshot_report_length_mm"] == 1120
    assert row["snapshot_report_width_mm"] == 950
    assert row["snapshot_base_report_length_mm"] == 1095
    assert row["snapshot_base_report_width_mm"] == 925
    assert [part["source_type"] for part in row["material_cost_components"]] == [
        "order_cover",
        "order_base",
    ]


def test_irregular_manual_box_fails_closed_until_report_dimensions_are_explicit(
    order_api_app, monkeypatch
) -> None:
    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    _fixed_material_price(monkeypatch)
    with session_factory() as session:
        session.add(
            CustomerQuotePreference(
                customer_id=1,
                box_type="异形箱",
                material_id=1,
                flute_type="AB",
                tax_included_square_price=Decimal("3.2500"),
                is_active=True,
            )
        )
        session.commit()
    payload = _manual_payload(client_line_id="p1-75-irregular")
    payload["customer_po"] = "P1-75-IRREGULAR"
    payload["items"][0].update({"box_type": "异形箱", "unit_price": "5.00"})
    with TestClient(app) as client:
        _login(client)
        missing = client.post("/api/orders", json=payload)
        assert missing.status_code == 400, missing.text
        assert "必须人工填写正数的报料长和报料宽" in missing.json()["detail"]
        payload["items"][0].update(
            {
                "report_length_mm": 990,
                "report_width_mm": 610,
                "crease_type": "净料",
            }
        )
        created = client.post("/api/orders", json=payload)
    assert created.status_code == 201, created.text
    row = created.json()["items"][0]
    assert (row["snapshot_report_length_mm"], row["snapshot_report_width_mm"]) == (990, 610)
    assert row["snapshot_crease_type"] == "净料"
    assert Decimal(row["estimated_material_total_cost"]) == Decimal("241.56")


def test_two_dimensional_liner_manual_order_does_not_invent_a_height(
    order_api_app, monkeypatch
) -> None:
    app, session_factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(session_factory)
    _fixed_material_price(monkeypatch)
    with session_factory() as session:
        session.add(
            CustomerQuotePreference(
                customer_id=1,
                box_type="衬板",
                material_id=1,
                flute_type="AB",
                tax_included_square_price=Decimal("3.2500"),
                is_active=True,
            )
        )
        session.commit()
    payload = _manual_payload(client_line_id="p1-75-liner")
    payload["customer_po"] = "P1-75-LINER"
    payload["items"][0].update(
        {
            "box_type": "衬板",
            "height_mm": None,
            "crease_type": "净料",
            "unit_price": "1.50",
        }
    )
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)
    assert response.status_code == 201, response.text
    row = response.json()["items"][0]
    assert row["snapshot_spec"].replace("×", "x") == "520x350mm"
    assert (row["snapshot_report_length_mm"], row["snapshot_report_width_mm"]) == (520, 350)
    assert row["snapshot_crease_type"] == "净料"


def test_manual_report_contract_and_material_margin_are_internal_only_markers() -> None:
    assert "manual-size-report-row" in INDEX
    assert "refreshManualSizeRecommendation(item,{force:true})" in INDEX
    assert "item._report_override && !force" in INDEX
    assert "Number(item._recommendation_generation) !== generation" in INDEX
    assert "onManualSizeCreaseInput(item,'left')" in INDEX
    assert "item.report_width_mm = values.reduce" in INDEX
    assert "material_gross_profit" in INDEX
    assert "材料毛利（未扣加工、人工、运输和损耗）" in ORDERS
    for field in (
        "report_length_mm",
        "report_width_mm",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
        "base_report_length_mm",
        "base_report_width_mm",
        "default_cutting_mode",
    ):
        assert f"{field}: item.manual_size_entry" in INDEX


def test_manual_recommendation_latest_wins_and_a1_crease_sync_run_in_real_js(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the P1-75 frontend behavior test"
    recommendation = _method_body("refreshManualSizeRecommendation")
    crease = _method_body("onManualSizeCreaseInput")
    script = f"""
const pending=[];
const vm={{
  manualSizeRequiredDimensionsComplete(){{return true;}},
  requestBoxTypeRecommendation(){{return new Promise(resolve=>pending.push(resolve));}},
  errorMessage(error){{return error.message||String(error);}},
  isA1BoxStyle(){{return true;}},
  normalizeMmInteger(value){{const n=Number(value);return Number.isInteger(n)&&n>=0?n:null;}},
  markManualSizeReportOverride(item){{item._report_override=true;item._recommendation_generation=Number(item._recommendation_generation||0)+1;}},
}};
vm.refreshManualSizeRecommendation=new Function('return async function(item,{{force=false}}={{}}){{'+{json.dumps(recommendation, ensure_ascii=False)}+'}}')().bind(vm);
vm.onManualSizeCreaseInput=new Function('return function(item,part){{'+{json.dumps(crease, ensure_ascii=False)}+'}}')().bind(vm);
const item={{manual_size_entry:true,box_type:'A1',length_mm:520,width_mm:350,height_mm:300,splice_mode:'single',flap_mm:30,_report_override:false,_recommendation_generation:0}};
(async()=>{{
  const first=vm.refreshManualSizeRecommendation(item,{{force:true}});
  item.length_mm=530;
  const second=vm.refreshManualSizeRecommendation(item,{{force:true}});
  pending[1]({{report_length_mm:1800,report_width_mm:660,crease_type:'压线',crease_left_mm:180,crease_middle_mm:300,crease_right_mm:180,splice_mode:'single',pieces_per_box:1,flap_mm:30,message:'new'}});
  await second;
  pending[0]({{report_length_mm:1700,report_width_mm:650,crease_type:'压线',crease_left_mm:175,crease_middle_mm:300,crease_right_mm:175,message:'old'}});
  await first;
  if(item.report_length_mm!==1800||item.report_width_mm!==660||item._recommendation_message!=='new') throw new Error('stale recommendation overwrote the newer dimensions');
  const forced=vm.refreshManualSizeRecommendation(item,{{force:true}});
  item.crease_left_mm=195;item.crease_middle_mm=300;item.crease_right_mm=190;
  vm.onManualSizeCreaseInput(item,'left');
  pending[2]({{report_length_mm:1900,report_width_mm:700,crease_type:'压线',crease_left_mm:200,crease_middle_mm:300,crease_right_mm:200,message:'forced-old'}});
  await forced;
  if(item.crease_left_mm!==195||item.crease_right_mm!==195||item.report_width_mm!==690) throw new Error('in-flight forced recommendation overwrote a later manual edit');
  item.crease_left_mm=190;item.crease_middle_mm=300;item.crease_right_mm=180;
  vm.onManualSizeCreaseInput(item,'left');
  if(item.crease_right_mm!==190||item.report_width_mm!==680||!item._report_override) throw new Error('A1 crease edit did not synchronize both sides and board width');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    target = tmp_path / "p1-75-manual-behavior.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

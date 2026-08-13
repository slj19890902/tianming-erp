import json
import subprocess
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_phase11_requisition import (
    _add_pending_candidate,
    _login,
    requisition_app,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_source(start: str, end: str) -> str:
    start_index = INDEX_HTML.index(start)
    end_index = INDEX_HTML.index(end, start_index)
    return INDEX_HTML[start_index:end_index]


def _run_cutting_helper(script: str) -> dict:
    helper_source = (
        _method_source("cuttingModeFactor(mode) {", "requisitionDimensionWarnings(line) {")
        + _method_source(
            "calculateMergedRequisitionCuttingPlan(line, rawMode = null) {",
            "isMergedRequisitionCuttingLine(line) {",
        )
    )
    node_script = f"""
const helpers = ({{{helper_source}}});
const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};
{script}
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def test_cutting_helper_recomputes_from_immutable_base_without_accumulation() -> None:
    result = _run_cutting_helper(
        """
const row = {
  original_report_length_mm: 800,
  original_report_width_mm: 200,
  effective_demand_piece_qty: 20,
  cutting_mode: "一开一",
  cutting_plan_fingerprint: "a".repeat(64),
};
const states = [];
for (const factor of [1, 2, 3, 1]) {
  assert(helpers.applyMergedRequisitionCuttingPlan(row, factor), `factor ${factor} rejected`);
  states.push([row.cutting_mode, row.report_length_mm, row.report_width_mm,
    row.requisition_qty, row.theoretical_output_piece_qty, row.remainder_piece_qty]);
}
assert(row.cutting_plan_fingerprint === "a".repeat(64), "CAS fingerprint changed in browser");
console.log(JSON.stringify({states, summary:helpers.mergedRequisitionCuttingPlanSummary({...row, cutting_mode:"一开三"})}));
"""
    )
    assert result["states"] == [
        ["一开一", 800, 200, 20, 20, 0],
        ["一开二", 800, 400, 10, 20, 0],
        ["一开三", 800, 600, 7, 21, 1],
        ["一开一", 800, 200, 20, 20, 0],
    ]
    assert result["summary"] == "一开三｜单片宽200 → 采购宽600mm｜需求20片 → 采购7张｜余1片"


def test_cutting_helper_handles_ten_piece_rounding_and_fails_closed_without_base() -> None:
    result = _run_cutting_helper(
        """
const ten = {original_report_length_mm:800, original_report_width_mm:200,
  effective_demand_piece_qty:10, report_width_mm:999};
assert(helpers.applyMergedRequisitionCuttingPlan(ten, 3), "10-piece plan rejected");
const missingWidth = {original_report_length_mm:800, effective_demand_piece_qty:20,
  report_width_mm:999, requisition_qty:99};
const missingDemand = {original_report_length_mm:800, original_report_width_mm:200,
  report_width_mm:999, requisition_qty:99};
assert(!helpers.applyMergedRequisitionCuttingPlan(missingWidth, 3), "missing width accepted");
assert(!helpers.applyMergedRequisitionCuttingPlan(missingDemand, 3), "missing demand accepted");
console.log(JSON.stringify({ten, missingWidth, missingDemand}));
"""
    )
    assert result["ten"]["report_width_mm"] == 600
    assert result["ten"]["requisition_qty"] == 4
    assert result["ten"]["theoretical_output_piece_qty"] == 12
    assert result["ten"]["remainder_piece_qty"] == 2
    assert result["missingWidth"]["report_width_mm"] == 999
    assert "原始单片报料宽" in result["missingWidth"]["_cutting_plan_error"]
    assert result["missingDemand"]["requisition_qty"] == 99
    assert "有效需求片数" in result["missingDemand"]["_cutting_plan_error"]


def test_both_merged_entry_points_call_the_same_helper_and_show_plan() -> None:
    pending = INDEX_HTML.split('class="requisition-pending-table"', 1)[1].split(
        "<pager v-if=\"requisitionTab==='pending'\"", 1
    )[0]
    supplier = INDEX_HTML.split("modal.type === 'supplierRequisitionDraft'", 1)[1].split(
        "modal.type === 'requisition'", 1
    )[0]
    assert '@change="applyMergedRequisitionCuttingPlan(row,$event.target.value)"' in pending
    assert "mergedRequisitionCuttingPlanSummary(row)" in pending
    assert "recalculateSupplierDraftLine(line)" in supplier
    assert "mergedRequisitionCuttingPlanSummary(line)" in supplier
    assert 'v-model.number="line.requisition_qty"' in supplier
    assert "line.quantity_override_acknowledged=false" in supplier


def test_merge_and_supplier_payloads_round_trip_server_cutting_contract() -> None:
    merge_payload = _method_source("mergeGroupPayload(row) {", "async updateMergeGroup(row) {")
    save_payload = _method_source(
        "async saveSupplierRequisitionDraft() {", "supplierRequisitionSelectionSignature(selections) {"
    )
    for field in (
        "original_report_length_mm",
        "original_report_width_mm",
        "effective_demand_piece_qty",
        "theoretical_output_piece_qty",
        "remainder_piece_qty",
    ):
        assert field in merge_payload
        assert field in save_payload
    for field in (
        "expected_cutting_plan_fingerprint",
        "calculated_report_length_mm",
        "calculated_report_width_mm",
        "calculated_requisition_qty",
        "calculated_effective_demand_piece_qty",
    ):
        assert field in merge_payload
    assert "cutting_plan_fingerprint: line.cutting_plan_fingerprint" in save_payload
    assert "cutting_plan_fingerprint:" not in _method_source(
        "applyMergedRequisitionCuttingPlan(line, rawMode = null) {",
        "mergedRequisitionCuttingPlanSummary(line) {",
    )


def test_backend_supplier_line_schema_exposes_fail_closed_cutting_fields() -> None:
    from app.api.requisition import PendingSupplierOrderDraftLine

    required = {
        "original_report_length_mm",
        "original_report_width_mm",
        "effective_demand_piece_qty",
        "theoretical_output_piece_qty",
        "remainder_piece_qty",
        "cutting_plan_fingerprint",
    }
    assert required <= set(PendingSupplierOrderDraftLine.model_fields)


@pytest.fixture()
def p1_46a_merge(requisition_app):
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        46,
        product_code="P146A-002",
        product_name="P1-46A 合并箱二",
        quantity=10,
    )
    with session_factory() as session:
        for item_id in (1, second_id):
            item = session.get(OrderItem, item_id)
            assert item is not None
            item.quantity = 10
            item.subtotal = Decimal("36")
            item.snapshot_report_length_mm = 800
            item.snapshot_report_width_mm = 200
            item.snapshot_pieces_per_box = 1
            item.snapshot_splice_mode = "single"
            item.special_process = "一开一"
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        created_response = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, second_id],
                "supplier_name": "苏州纸板供应商",
                "report_length_mm": 800,
                "report_width_mm": 200,
                "cutting_mode": "一开一",
                "remark": "P1-46A 权威尺寸夹具",
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
    return app, session_factory, created, second_id


def _merge_update_payload(row: dict, factor: int) -> dict:
    demand = int(row["effective_demand_piece_qty"])
    requisition_qty = (demand + factor - 1) // factor
    return {
        "supplier_name": row.get("supplier_name"),
        "report_length_mm": row["original_report_length_mm"],
        "report_width_mm": int(row["original_report_width_mm"]) * factor,
        "cutting_mode": f"一开{factor}",
        "remark": f"切换一开{factor}",
        "expected_cutting_plan_fingerprint": row["cutting_plan_fingerprint"],
        "calculated_report_length_mm": row["original_report_length_mm"],
        "calculated_report_width_mm": int(row["original_report_width_mm"]) * factor,
        "calculated_requisition_qty": requisition_qty,
        "calculated_effective_demand_piece_qty": demand,
    }


def _preview_merge(client: TestClient, row: dict) -> dict:
    response = client.post(
        "/api/requisition/supplier-orders/preview-from-pending-selection",
        json={
            "selections": [
                {
                    "type": "merge_group",
                    "merge_group_id": row["id"],
                    "supplier_name": row["supplier_name"],
                    "report_length_mm": row["report_length_mm"],
                    "report_width_mm": row["report_width_mm"],
                    "cutting_mode": row["cutting_mode"],
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_api_aggregate_two_ten_piece_sources_before_ceiling_and_supplier_preview(
    p1_46a_merge,
) -> None:
    app, _session_factory, created, _second_id = p1_46a_merge
    with TestClient(app) as client:
        _login(client, "sales")
        updated = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json=_merge_update_payload(created, 3),
        )
        assert updated.status_code == 200, updated.text
        row = updated.json()
        draft = _preview_merge(client, row)

    assert Decimal(str(row["original_report_length_mm"])) == Decimal("800")
    assert Decimal(str(row["original_report_width_mm"])) == Decimal("200")
    assert row["effective_demand_piece_qty"] == 20
    assert Decimal(str(row["report_length_mm"])) == Decimal("800")
    assert Decimal(str(row["report_width_mm"])) == Decimal("600")
    assert row["requisition_qty"] == 7
    assert row["theoretical_output_piece_qty"] == 21
    assert row["remainder_piece_qty"] == 1
    line = draft["supplier_groups"][0]["lines"][0]
    assert line["source_type"] == "merge_group"
    assert len(line["source_items"]) == 2
    assert line["effective_demand_piece_qty"] == 20
    assert Decimal(str(line["report_width_mm"])) == Decimal("600")
    assert line["requisition_qty"] == 7
    assert line["theoretical_output_piece_qty"] == 21
    assert line["remainder_piece_qty"] == 1
    assert line["cutting_plan_fingerprint"] == row["cutting_plan_fingerprint"]


def test_api_repeated_one_two_three_one_switches_never_accumulate_width(
    p1_46a_merge,
) -> None:
    app, _session_factory, row, _second_id = p1_46a_merge
    observed = []
    with TestClient(app) as client:
        _login(client, "sales")
        for factor in (1, 2, 3, 1):
            response = client.put(
                f"/api/requisition/merge-groups/{row['id']}",
                json=_merge_update_payload(row, factor),
            )
            assert response.status_code == 200, response.text
            row = response.json()
            observed.append(
                (
                    row["cutting_mode"],
                    int(row["report_width_mm"]),
                    row["requisition_qty"],
                    row["theoretical_output_piece_qty"],
                    row["remainder_piece_qty"],
                )
            )
    assert observed == [
        ("一开一", 200, 20, 20, 0),
        ("一开二", 400, 10, 20, 0),
        ("一开三", 600, 7, 21, 1),
        ("一开一", 200, 20, 20, 0),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_width_mm", 601),
        ("calculated_report_width_mm", 601),
        ("calculated_requisition_qty", 8),
        ("calculated_effective_demand_piece_qty", 19),
    ],
)
def test_api_rejects_tampered_calculated_plan_without_partial_mutation(
    p1_46a_merge,
    field: str,
    value: int,
) -> None:
    from app.models.audit import OperationLog
    from app.models.requisition import RequisitionItem

    app, session_factory, created, _second_id = p1_46a_merge
    payload = _merge_update_payload(created, 3)
    payload[field] = value
    with session_factory() as session:
        before = [
            (row.id, row.cardboard_len, row.cardboard_width, row.special_process, row.requisition_qty)
            for row in session.scalars(
                select(RequisitionItem)
                .where(RequisitionItem.requisition_id == created["id"])
                .order_by(RequisitionItem.id)
            )
        ]
        audit_before = session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code == "requisition.merge_group.update"
            )
        )
    with TestClient(app) as client:
        _login(client, "sales")
        response = client.put(
            f"/api/requisition/merge-groups/{created['id']}", json=payload
        )
    assert response.status_code == 409, response.text
    with session_factory() as session:
        after = [
            (row.id, row.cardboard_len, row.cardboard_width, row.special_process, row.requisition_qty)
            for row in session.scalars(
                select(RequisitionItem)
                .where(RequisitionItem.requisition_id == created["id"])
                .order_by(RequisitionItem.id)
            )
        ]
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code == "requisition.merge_group.update"
            )
        ) == audit_before
    assert after == before


def test_api_stale_fingerprint_rejects_second_writer_and_audits_old_new_once(
    p1_46a_merge,
) -> None:
    from app.models.audit import OperationLog

    app, session_factory, created, _second_id = p1_46a_merge
    first_payload = _merge_update_payload(created, 2)
    stale_payload = _merge_update_payload(created, 3)
    with TestClient(app) as client:
        _login(client, "sales")
        first = client.put(
            f"/api/requisition/merge-groups/{created['id']}", json=first_payload
        )
        assert first.status_code == 200, first.text
        stale = client.put(
            f"/api/requisition/merge-groups/{created['id']}", json=stale_payload
        )
    assert stale.status_code == 409, stale.text
    assert "刷新" in stale.text
    with session_factory() as session:
        logs = list(
            session.scalars(
                select(OperationLog)
                .where(
                    OperationLog.action_code == "requisition.merge_group.update",
                    OperationLog.entity_id == created["id"],
                )
                .order_by(OperationLog.id)
            )
        )
    assert len(logs) == 1
    details = json.loads(logs[0].details)
    assert details["old"]["cutting_mode"] == "一开一"
    assert details["new"]["cutting_mode"] == "一开二"
    assert details["old"]["report_width_mm"] == "200"
    assert details["new"]["report_width_mm"] == "400"


def test_api_double_splice_determines_r_before_cutting_factor(p1_46a_merge) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem

    app, session_factory, created, second_id = p1_46a_merge
    with session_factory() as session:
        for item_id in (1, second_id):
            item = session.get(OrderItem, item_id)
            assert item is not None
            item.snapshot_pieces_per_box = 2
            item.snapshot_splice_mode = "double"
        for req_item in session.scalars(
            select(RequisitionItem).where(RequisitionItem.requisition_id == created["id"])
        ):
            req_item.pieces_per_box = 2
        session.commit()
    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        current = next(
            row for row in pending.json()["items"] if row.get("merge_group_id") == created["id"]
        )
        response = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json=_merge_update_payload(current, 3),
        )
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["effective_demand_piece_qty"] == 40
    assert row["requisition_qty"] == 14
    assert row["theoretical_output_piece_qty"] == 42
    assert row["remainder_piece_qty"] == 2


def test_supplier_finalize_accepts_authoritative_merged_plan_and_writes_seven_sheets(
    p1_46a_merge,
) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import Requisition
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory, created, second_id = p1_46a_merge
    with TestClient(app) as client:
        _login(client, "sales")
        updated = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json=_merge_update_payload(created, 3),
        )
        assert updated.status_code == 200, updated.text
        draft = _preview_merge(client, updated.json())
        saved = client.post(
            "/api/requisition/supplier-orders/from-pending-selection", json=draft
        )
    assert saved.status_code == 201, saved.text
    with session_factory() as session:
        order = session.scalar(select(SupplierRequisitionOrder))
        assert order is not None
        lines = list(
            session.scalars(
                select(SupplierRequisitionOrderItem)
                .where(SupplierRequisitionOrderItem.supplier_order_id == order.id)
                .order_by(SupplierRequisitionOrderItem.id)
            )
        )
        assert sum(int(line.requisition_qty) for line in lines) == 7
        assert {line.order_item_id for line in lines} == {1, second_id}
        assert {line.cutting_mode for line in lines} == {"一开三"}
        assert {int(line.report_width_mm) for line in lines} == {600}
        assert session.get(Requisition, created["id"]).status == "supplier_requisition_created"
        assert session.get(OrderItem, 1).requisition_status == "已报料"
        assert session.get(OrderItem, second_id).requisition_status == "已报料"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_width_mm", 601),
        ("requisition_qty", 8),
        ("effective_demand_piece_qty", 19),
        ("theoretical_output_piece_qty", 22),
        ("remainder_piece_qty", 2),
        ("cutting_plan_fingerprint", "f" * 64),
    ],
)
def test_supplier_finalize_rejects_merged_plan_tamper_and_rolls_back_everything(
    p1_46a_merge,
    field: str,
    value,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory, created, second_id = p1_46a_merge
    with TestClient(app) as client:
        _login(client, "sales")
        updated = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json=_merge_update_payload(created, 3),
        )
        assert updated.status_code == 200, updated.text
        draft = _preview_merge(client, updated.json())
        line = draft["supplier_groups"][0]["lines"][0]
        line[field] = value
        response = client.post(
            "/api/requisition/supplier-orders/from-pending-selection", json=draft
        )
    assert response.status_code in {403, 409}, response.text
    with session_factory() as session:
        assert session.scalar(select(func.count(SupplierRequisitionOrder.id))) == 0
        assert session.get(Requisition, created["id"]).status == "merged_pending"
        assert session.get(OrderItem, 1).requisition_status == "未报料"
        assert session.get(OrderItem, second_id).requisition_status == "未报料"
        assert {
            row.status
            for row in session.scalars(
                select(RequisitionItem).where(
                    RequisitionItem.requisition_id == created["id"]
                )
            )
        } == {"merged_pending"}
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code == "requisition.supplier_order.create"
            )
        ) == 0


def test_supplier_finalize_rejects_stale_preview_after_live_demand_change(
    p1_46a_merge,
) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import Requisition
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory, created, second_id = p1_46a_merge
    with TestClient(app) as client:
        _login(client, "sales")
        updated = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json=_merge_update_payload(created, 3),
        )
        assert updated.status_code == 200, updated.text
        stale_draft = _preview_merge(client, updated.json())
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            item.quantity = 9
            session.commit()
        response = client.post(
            "/api/requisition/supplier-orders/from-pending-selection",
            json=stale_draft,
        )
    assert response.status_code == 409, response.text
    assert "刷新" in response.text
    with session_factory() as session:
        assert session.scalar(select(func.count(SupplierRequisitionOrder.id))) == 0
        assert session.get(Requisition, created["id"]).status == "merged_pending"
        assert session.get(OrderItem, 1).requisition_status == "未报料"
        assert session.get(OrderItem, second_id).requisition_status == "未报料"


def test_inventory_reservation_changes_live_r_and_invalidates_old_plan(
    p1_46a_merge,
) -> None:
    from app.models.order import OrderItem
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.warehouse_inventory import (
        manual_finished_in,
        reserve_finished_inventory,
    )

    app, session_factory, created, _second_id = p1_46a_merge
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        admin = session.scalar(select(User).where(User.username == "admin"))
        assert item is not None and admin is not None
        location = WarehouseLocation(
            location_code="P146A-FG-01",
            location_name="P1-46A 成品测试位",
            warehouse_type="finished",
            is_active=True,
            placement_status="placed",
        )
        session.add(location)
        session.flush()
        lot = manual_finished_in(
            session,
            customer_id=1,
            product_id=item.product_id,
            location_id=location.id,
            quantity=3,
            stock_date=date.today(),
            source_type="manual",
            remarks="P1-46A 临时库存夹具",
            operator_id=admin.id,
            idempotency_key="p146a-finished-lot",
        )
        session.commit()
        lot_version = lot.version
        lot_id = lot.id

    with TestClient(app) as client:
        _login(client, "sales")
        before = client.get("/api/requisition/pending").json()
        old = next(
            row for row in before["items"] if row.get("merge_group_id") == created["id"]
        )
        with session_factory() as session:
            admin = session.scalar(select(User).where(User.username == "admin"))
            assert admin is not None
            reserve_finished_inventory(
                session,
                order_item_id=1,
                inventory_lot_id=lot_id,
                quantity=3,
                expected_version=lot_version,
                operator_id=admin.id,
                idempotency_key="p146a-finished-reserve",
                warning_acknowledged_codes=[],
            )
            session.commit()
        stale = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json=_merge_update_payload(old, 3),
        )
        assert stale.status_code == 409, stale.text
        after = client.get("/api/requisition/pending")
    assert after.status_code == 200, after.text
    current = next(
        row for row in after.json()["items"] if row.get("merge_group_id") == created["id"]
    )
    assert old["effective_demand_piece_qty"] == 20
    assert current["effective_demand_piece_qty"] == 17
    assert current["cutting_plan_fingerprint"] != old["cutting_plan_fingerprint"]
    assert current["requisition_qty"] == 17
    assert current["finished_inventory_reserved_qty"] == 3


def test_scoped_user_cannot_read_or_update_other_customer_merge(p1_46a_merge) -> None:
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.user import User

    app, session_factory, created, _second_id = p1_46a_merge
    with session_factory() as session:
        sales = session.scalar(select(User).where(User.username == "sales"))
        assert sales is not None
        sales.customer_access_mode = "selected"
        hidden_only = Customer(
            customer_number=46,
            customer_code="P146A-HIDDEN",
            name="P1-46A 无关客户",
            payment_term_days=30,
            credit_limit=Decimal("10000"),
        )
        session.add(hidden_only)
        session.flush()
        session.add(UserCustomerScope(user_id=sales.id, customer_id=hidden_only.id))
        session.commit()
    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        update = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json={"remark": "越权尝试"},
        )
    assert pending.status_code == 200, pending.text
    assert not [
        row
        for row in pending.json()["items"]
        if row.get("merge_group_id") == created["id"]
    ]
    assert update.status_code in {403, 404}


def test_legacy_merge_without_snapshot_base_keeps_read_and_remark_compatibility(
    requisition_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        47,
        product_code="P146A-LEGACY",
        product_name="P1-46A 历史合并组",
        quantity=10,
    )
    with session_factory() as session:
        for item_id in (1, second_id):
            item = session.get(OrderItem, item_id)
            assert item is not None
            item.snapshot_report_length_mm = 800
            item.snapshot_report_width_mm = 200
        session.commit()
    with TestClient(app) as client:
        _login(client, "sales")
        created_response = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, second_id],
                "supplier_name": "苏州纸板供应商",
                "report_length_mm": 800,
                "report_width_mm": 200,
                "cutting_mode": "一开一",
                "remark": "旧数据创建前状态",
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        with session_factory() as session:
            for item_id in (1, second_id):
                item = session.get(OrderItem, item_id)
                assert item is not None
                item.snapshot_report_length_mm = None
                item.snapshot_report_width_mm = None
            session.commit()
        pending = client.get("/api/requisition/pending")
        remark_only = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json={"remark": "历史合并组仅改备注"},
        )
        mode_change = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json={
                "report_length_mm": 800,
                "report_width_mm": 400,
                "cutting_mode": "一开二",
                "expected_cutting_plan_fingerprint": created[
                    "cutting_plan_fingerprint"
                ],
                "calculated_report_length_mm": 800,
                "calculated_report_width_mm": 400,
                "calculated_requisition_qty": 55,
                "calculated_effective_demand_piece_qty": 110,
            },
        )
    assert pending.status_code == 200, pending.text
    legacy = next(
        row for row in pending.json()["items"] if row.get("merge_group_id") == created["id"]
    )
    assert legacy["cutting_mode"] == "一开一"
    assert remark_only.status_code == 200, remark_only.text
    assert remark_only.json()["remark"] == "历史合并组仅改备注"
    assert mode_change.status_code == 409, mode_change.text
    assert "原始单片" in mode_change.text


def test_frontend_legacy_merge_disables_cutting_but_keeps_remark_only_save() -> None:
    pending = INDEX_HTML.split('class="requisition-pending-table"', 1)[1].split(
        "<pager v-if=\"requisitionTab==='pending'\"", 1
    )[0]
    assert ':disabled="!hasAuthoritativeMergedCuttingBase(row)"' in pending
    update = _method_source("async updateMergeGroup(row) {", "pendingSupplierSelectionPayload(row) {")
    assert "const hasCuttingBase = this.hasAuthoritativeMergedCuttingBase(row)" in update
    assert "supplier_name:row.supplier_name" in update
    assert "remark:row.remark" in update


def test_posted_supplier_facts_and_history_are_immutable_after_merge_finalize(
    p1_46a_merge,
) -> None:
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory, created, _second_id = p1_46a_merge
    with TestClient(app) as client:
        _login(client, "sales")
        updated = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json=_merge_update_payload(created, 3),
        )
        assert updated.status_code == 200, updated.text
        draft = _preview_merge(client, updated.json())
        saved = client.post(
            "/api/requisition/supplier-orders/from-pending-selection", json=draft
        )
        assert saved.status_code == 201, saved.text
        with session_factory() as session:
            before_orders = [
                (
                    order.id,
                    order.status,
                    order.report_length_mm,
                    order.report_width_mm,
                    order.cutting_mode,
                    order.requisition_qty,
                )
                for order in session.scalars(
                    select(SupplierRequisitionOrder).order_by(
                        SupplierRequisitionOrder.id
                    )
                )
            ]
            before_items = [
                (
                    item.id,
                    item.order_item_id,
                    item.report_length_mm,
                    item.report_width_mm,
                    item.cutting_mode,
                    item.requisition_qty,
                )
                for item in session.scalars(
                    select(SupplierRequisitionOrderItem).order_by(
                        SupplierRequisitionOrderItem.id
                    )
                )
            ]
        rejected = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json={"remark": "不得覆盖已生成正式单据"},
        )
    assert rejected.status_code == 409, rejected.text
    with session_factory() as session:
        after_orders = [
            (
                order.id,
                order.status,
                order.report_length_mm,
                order.report_width_mm,
                order.cutting_mode,
                order.requisition_qty,
            )
            for order in session.scalars(
                select(SupplierRequisitionOrder).order_by(SupplierRequisitionOrder.id)
            )
        ]
        after_items = [
            (
                item.id,
                item.order_item_id,
                item.report_length_mm,
                item.report_width_mm,
                item.cutting_mode,
                item.requisition_qty,
            )
            for item in session.scalars(
                select(SupplierRequisitionOrderItem).order_by(
                    SupplierRequisitionOrderItem.id
                )
            )
        ]
    assert after_orders == before_orders
    assert after_items == before_items


def test_ordinary_supplier_draft_keeps_manual_partial_then_remaining_quantity(
    requisition_app,
) -> None:
    app, _session_factory = requisition_app
    selection = {
        "type": "order_item",
        "order_item_id": 1,
        "supplier_name": "苏州纸板供应商",
        "report_length_mm": 1000,
        "report_width_mm": 800,
        "cutting_mode": "一开一",
    }
    with TestClient(app) as client:
        _login(client, "sales")
        first_preview_response = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={"selections": [selection]},
        )
        assert first_preview_response.status_code == 200, first_preview_response.text
        first_draft = first_preview_response.json()
        first_line = first_draft["supplier_groups"][0]["lines"][0]
        assert first_line["source_type"] == "normal"
        assert first_line["remaining_requisition_qty"] == 100
        first_line["requisition_qty"] = 40
        first_saved = client.post(
            "/api/requisition/supplier-orders/from-pending-selection",
            json=first_draft,
        )
        assert first_saved.status_code == 201, first_saved.text
        second_preview_response = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={"selections": [selection]},
        )
    assert second_preview_response.status_code == 200, second_preview_response.text
    second_line = second_preview_response.json()["supplier_groups"][0]["lines"][0]
    assert second_line["source_type"] == "normal"
    assert second_line["already_requisitioned_qty"] == 40
    assert second_line["remaining_requisition_qty"] == 60
    assert second_line["requisition_qty"] == 60

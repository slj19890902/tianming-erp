from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_phase11_requisition import (
    _add_second_merge_candidate,
    _login,
    requisition_app,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _seed_merged_receipt_group(session_factory) -> dict:
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User

    second_order_item_id = _add_second_merge_candidate(session_factory)
    now = datetime(2026, 8, 24, 9, 30)
    with session_factory() as session:
        workshop = session.scalar(select(User).where(User.username == "workshop"))
        assert workshop is not None
        order_items = [session.get(OrderItem, value) for value in (1, second_order_item_id)]
        assert all(row is not None for row in order_items)
        requisition = Requisition(
            requisition_number="BL-20260824-GROUP-001",
            requisition_date=now.date(),
            supplier_name="苏州纸板供应商",
            status="supplier_requisition_created",
            created_by=workshop.id,
        )
        supplier_order = SupplierRequisitionOrder(
            order_number="SRO-20260824-GROUP-001",
            supplier_name="苏州纸板供应商",
            layer_count=5,
            flute_type="AB",
            report_length_mm=992,
            report_width_mm=1061,
            total_quantity=sum(int(row.quantity) for row in order_items),
            requisition_qty=sum(int(row.quantity) for row in order_items),
            status="confirmed",
            created_by=workshop.id,
        )
        session.add_all([requisition, supplier_order])
        session.flush()

        requisition_items: list[RequisitionItem] = []
        supplier_items: list[SupplierRequisitionOrderItem] = []
        for order_item in order_items:
            order_item.material_status = "received"
            order_item.requisition_status = "已入库"
            order_item.requisition_qty = int(order_item.quantity)
            order_item.cardboard_len = Decimal("992")
            order_item.cardboard_width = Decimal("1061")
            order_item.layer_count = 5
            order_item.flute_type = "AB"
            order_item.snapshot_material = "W618K"
            order_item.material_received_at = now
            order_item.material_received_by = workshop.id
            order_item.supplier_order_number = supplier_order.order_number
            requisition_item = RequisitionItem(
                requisition_id=requisition.id,
                order_item_id=order_item.id,
                inventory_deducted_qty=0,
                requisition_qty=int(order_item.quantity),
                cardboard_len=Decimal("992"),
                cardboard_width=Decimal("1061"),
                special_process="无",
                material_snapshot="W618K",
                product_code_snapshot=order_item.snapshot_product_code,
                product_name_snapshot=order_item.snapshot_product_name,
                specification_snapshot=order_item.snapshot_spec,
                status="已入库",
                purpose_contract_status="legacy_unset",
            )
            session.add(requisition_item)
            session.flush()
            supplier_item = SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=order_item.id,
                source_key=f"requisition_item:{requisition_item.id}",
                product_id=order_item.product_id,
                material_code_snapshot="W618K",
                supplier_name_snapshot="苏州纸板供应商",
                layer_count_snapshot=5,
                flute_type_snapshot="AB",
                product_code=order_item.snapshot_product_code,
                product_name=order_item.snapshot_product_name,
                report_length_mm=992,
                report_width_mm=1061,
                quantity=int(order_item.quantity),
                requisition_qty=int(order_item.quantity),
                status="active",
                purpose_contract_status="legacy_unset",
            )
            session.add(supplier_item)
            session.flush()
            requisition_items.append(requisition_item)
            supplier_items.append(supplier_item)

        receipt = IncomingReceipt(
            receipt_number="IR-20260824-GROUP-001",
            status="posted",
            received_at=now,
            received_by=workshop.id,
            idempotency_key="p191u-seeded-group-receipt",
        )
        session.add(receipt)
        session.flush()
        receipt_items: list[IncomingReceiptItem] = []
        for order_item, requisition_item, supplier_item in zip(
            order_items, requisition_items, supplier_items, strict=True
        ):
            receipt_item = IncomingReceiptItem(
                receipt_id=receipt.id,
                order_id=order_item.order_id,
                order_item_id=order_item.id,
                requisition_id=None,
                requisition_item_id=None,
                supplier_order_id=supplier_order.id,
                supplier_order_item_id=supplier_item.id,
                planned_quantity=int(order_item.quantity),
                received_quantity=int(order_item.quantity),
                cumulative_received_quantity=int(order_item.quantity),
                variance_quantity=0,
                variance_type="matched",
                resolution_status="not_required",
                status="posted",
            )
            session.add(receipt_item)
            session.flush()
            receipt_items.append(receipt_item)

        receive_batch_id = "p191u-authoritative-receive-batch"
        for receipt_item in receipt_items:
            session.add(
                OperationLog(
                    user_id=workshop.id,
                    action="RECEIVE_MATERIAL",
                    resource="IncomingReceiptItem",
                    username=workshop.username,
                    role=workshop.role,
                    entity_type="incoming_receipt_item",
                    entity_id=receipt_item.id,
                    event_category="business",
                    result="success",
                    source="web",
                    module_code="incoming",
                    action_code="incoming.receive",
                    actor_user_id_snapshot=workshop.id,
                    operator_name_snapshot=workshop.display_name,
                    object_ref=f"incoming:{receipt_item.id}",
                    batch_id=receive_batch_id,
                    schema_version=1,
                )
            )
        session.commit()
        return {
            "receipt_id": receipt.id,
            "receipt_item_ids": [row.id for row in receipt_items],
            "order_ids": [row.order_id for row in order_items],
        }


def _group_payload(row: dict, *, key: str) -> dict:
    return {
        "idempotency_key": key,
        "expected_group_key": row["reversal_group_key"],
        "expected_receipt_item_ids": row["reversal_group_receipt_item_ids"],
    }


def test_merged_receipt_projects_one_group_and_reverts_atomically(requisition_app) -> None:
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.purchase_receipt import IncomingReceiptReversalFact

    app, session_factory = requisition_app
    seeded = _seed_merged_receipt_group(session_factory)
    with TestClient(app) as client:
        _login(client, "workshop")
        recent = client.get("/api/incoming/received", params={"page": 1, "page_size": 20})
        assert recent.status_code == 200, recent.text
        group_rows = [row for row in recent.json()["items"] if row.get("reversal_group_key")]
        assert len(group_rows) == 2
        assert len({row["reversal_group_key"] for row in group_rows}) == 1
        assert all(row["reversal_group_size"] == 2 for row in group_rows)
        history = client.get("/api/incoming/history", params={"page": 1, "page_size": 20})
        assert history.status_code == 200, history.text
        history_group_rows = [
            row for row in history.json()["items"] if row.get("reversal_group_key")
        ]
        assert {
            row["receipt_item_id"] for row in history_group_rows
        } == set(seeded["receipt_item_ids"])
        anchor = next(
            row
            for row in group_rows
            if row["receipt_item_id"] == row["reversal_group_anchor_receipt_item_id"]
        )
        payload = _group_payload(anchor, key="p191u-group-revert")

        denied = client.put(
            f"/api/incoming/receipt-reversal-groups/{anchor['receipt_item_id']}/revert",
            json=payload,
        )
        assert denied.status_code == 403
        client.post("/api/auth/logout")
        _login(client, "admin")
        stale = client.put(
            f"/api/incoming/receipt-reversal-groups/{anchor['receipt_item_id']}/revert",
            json={**payload, "expected_receipt_item_ids": [anchor["receipt_item_id"], 999999]},
        )
        assert stale.status_code == 409
        reverted = client.put(
            f"/api/incoming/receipt-reversal-groups/{anchor['receipt_item_id']}/revert",
            json=payload,
        )
        replay = client.put(
            f"/api/incoming/receipt-reversal-groups/{anchor['receipt_item_id']}/revert",
            json=payload,
        )

    assert reverted.status_code == 200, reverted.text
    assert reverted.json() == replay.json()
    assert reverted.json()["receipt_item_ids"] == sorted(seeded["receipt_item_ids"])
    assert reverted.json()["reverted_count"] == 2
    with session_factory() as session:
        assert {
            session.get(IncomingReceiptItem, value).status
            for value in seeded["receipt_item_ids"]
        } == {"reversed"}
        assert session.get(IncomingReceipt, seeded["receipt_id"]).status == "reversed"
        assert session.scalar(
            select(func.count()).select_from(IncomingReceiptReversalFact)
        ) == 2
        group_audits = session.scalars(
            select(OperationLog).where(OperationLog.action_code == "incoming.revert_group")
        ).all()
        assert len(group_audits) == 1
        assert json.loads(group_audits[0].details)["receipt_item_ids"] == sorted(
            seeded["receipt_item_ids"]
        )


def test_same_signature_from_different_receive_batches_never_groups(
    requisition_app,
) -> None:
    from app.models.audit import OperationLog

    app, session_factory = requisition_app
    seeded = _seed_merged_receipt_group(session_factory)
    with session_factory() as session:
        receive_logs = session.scalars(
            select(OperationLog)
            .where(
                OperationLog.action_code == "incoming.receive",
                OperationLog.entity_type == "incoming_receipt_item",
                OperationLog.entity_id.in_(seeded["receipt_item_ids"]),
            )
            .order_by(OperationLog.entity_id)
        ).all()
        assert len(receive_logs) == 2
        receive_logs[0].batch_id = "p191u-authoritative-receive-batch-a"
        receive_logs[1].batch_id = "p191u-authoritative-receive-batch-b"
        session.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        recent = client.get("/api/incoming/received", params={"page": 1, "page_size": 20})

    assert recent.status_code == 200, recent.text
    rows = [
        row
        for row in recent.json()["items"]
        if row.get("receipt_item_id") in seeded["receipt_item_ids"]
    ]
    assert len(rows) == 2
    assert all(not row.get("reversal_group_key") for row in rows)


def test_merged_receipt_second_failure_rolls_back_first(requisition_app) -> None:
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import Order
    from app.models.purchase_receipt import IncomingReceiptReversalFact

    app, session_factory = requisition_app
    seeded = _seed_merged_receipt_group(session_factory)
    blocked_receipt_id = min(seeded["receipt_item_ids"])
    with session_factory() as session:
        blocked = session.get(IncomingReceiptItem, blocked_receipt_id)
        session.get(Order, blocked.order_id).status = "delivered"
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        recent = client.get("/api/incoming/received", params={"page": 1, "page_size": 20})
        anchor = next(
            row
            for row in recent.json()["items"]
            if row.get("reversal_group_anchor_receipt_item_id") == row.get("receipt_item_id")
        )
        failed = client.put(
            f"/api/incoming/receipt-reversal-groups/{anchor['receipt_item_id']}/revert",
            json=_group_payload(anchor, key="p191u-group-rollback"),
        )

    assert failed.status_code == 409
    assert "已发货" in failed.text
    with session_factory() as session:
        assert {
            session.get(IncomingReceiptItem, value).status
            for value in seeded["receipt_item_ids"]
        } == {"posted"}
        assert session.scalar(
            select(func.count()).select_from(IncomingReceiptReversalFact)
        ) == 0
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code.in_(["incoming.revert", "incoming.revert_group"])
            )
        ) == 0


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def test_incoming_received_history_page_size_fills_viewport_and_preserves_anchor(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    page_size = _method_body("incomingListPageSize() {", "incomingRowsForTab() {")
    refresh = _method_body(
        "async refreshIncomingViewportPageSize({reload=false}={}) {",
        "async toggleIncomingHistoryFilters() {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.window={{innerHeight:1080}};
global.document={{documentElement:{{clientHeight:1080}}}};
let tableTop=250;
const rows=Array.from({{length:12}},()=>({{getBoundingClientRect:()=>({{height:36}})}}));
const table={{
  getBoundingClientRect:()=>({{top:tableTop}}),
  tHead:{{getBoundingClientRect:()=>({{height:32}})}},
  tBodies:[{{rows}}],
}};
const body={{closest:selector=>selector==="table"?table:null}};
const calls=[];
const vm={{
  activePage:"incoming",incomingWorkspace:"board",incomingTab:"received",isLargeUi:false,
  incomingViewportPageSizes:{{received:12,history:12}},pages:{{incomingReceived:3,incomingHistory:1}},
  $refs:{{incomingCompactTableBody:body}},$nextTick:async()=>{{}},
  loadIncomingReceived:async options=>calls.push(["received",options]),
  loadIncomingHistory:async()=>calls.push(["history"]),
}};
vm.incomingListPageSize=new Function({json.dumps(page_size, ensure_ascii=False)}).bind(vm);
vm.refreshIncomingViewportPageSize=new AsyncFunction("{{reload=false}}={{}}",{json.dumps(refresh, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const changed=await vm.refreshIncomingViewportPageSize({{reload:true}});
  expect(changed,"viewport size did not change");
  expect(vm.incomingViewportPageSizes.received>12,"1080px viewport still stopped at 12 rows");
  expect(vm.pages.incomingReceived===2,"first visible row anchor was not preserved");
  expect(calls.length===1&&calls[0][0]==="received","responsive resize did not reload once");
  const filledSize=vm.incomingViewportPageSizes.received;
  vm.incomingTab="history";tableTop=410;
  await vm.refreshIncomingViewportPageSize({{reload:false}});
  expect(vm.incomingViewportPageSizes.history<filledSize,"opened filter space did not reduce the history page size");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    target = tmp_path / "p1-91u-incoming-viewport.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_incoming_group_button_is_shared_while_rows_remain_independent() -> None:
    incoming = INDEX.split('<template v-else-if="activePage === \'incoming\'">', 1)[1]
    incoming = incoming.split('<template v-else-if="activePage === \'production\'">', 1)[0]
    assert 'ref="incomingCompactTableBody"' in incoming
    assert "v-for=\"(row,index) in incomingRowsForTab()\"" in incoming
    assert "incomingGroupRevertButtonVisible(row)" in incoming
    assert "整组撤销中…" in incoming
    assert "/api/incoming/receipt-reversal-groups/" in INDEX
    assert "expected_group_key:groupKey" in INDEX
    assert "expected_receipt_item_ids:memberIds" in INDEX
    assert ".table-wrap:has(> .incoming-compact-table) { max-height:none; overflow-x:hidden; overflow-y:visible; }" in INDEX

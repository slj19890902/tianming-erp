from __future__ import annotations

import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PRINT_PAGE = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)


def _method_body(name: str, next_name: str) -> tuple[list[str], str]:
    markers = [f"          {name}(", f"          async {name}("]
    marker = next((value for value in markers if value in INDEX), None)
    assert marker is not None, name
    start = INDEX.index(marker) + len(marker)
    params_end = INDEX.index(") {", start)
    body_start = params_end + len(") {")
    end_markers = [f"\n          {next_name}(", f"\n          async {next_name}("]
    body_end = min(INDEX.index(value, body_start) for value in end_markers if value in INDEX[body_start:])
    params = [part.strip() for part in INDEX[start:params_end].split(",") if part.strip()]
    return params, INDEX[body_start:body_end].strip().removesuffix("},").rstrip()


def _run_node(script: str, tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _selection(card: dict, supplier_order_id: int) -> dict:
    return {
        "supplier_order_id": supplier_order_id,
        "source_identity": card["source_identity"],
        "selection_fingerprint": card["selection_fingerprint"],
        "task_versions": card["production_task_versions"],
    }


def _without_replay(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key != "replayed"}


def test_prepare_only_prints_explicit_selection_and_replays_one_audit(
    production_print_app,
) -> None:
    from app.models.audit import OperationLog

    order_id = production_print_app["supplier_order_id"]
    with TestClient(production_print_app["app"]) as client:
        _login(client, "p132a2-admin")
        source = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        )
        assert source.status_code == 200, source.text
        cards = source.json()["cards"]
        assert len(cards) >= 4
        chosen = [_selection(cards[index], order_id) for index in (3, 0, 2)]
        payload = {
            "idempotency_key": "p1-64b-explicit-selection-001",
            "confirmed": True,
            "items": chosen,
        }
        first = client.post(
            "/api/requisition/production-print-batches/prepare", json=payload
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["replayed"] is False
        assert first_body["card_count"] == 3
        assert [row["source_identity"] for row in first_body["cards"]] == [
            item["source_identity"] for item in chosen
        ]
        assert first_body["page_count"] == 2
        assert first_body["print_url"].startswith(
            "/requisition-production-print.html?batch_id="
        )
        assert cards[1]["source_identity"] not in {
            row["source_identity"] for row in first_body["cards"]
        }

        second = client.post(
            "/api/requisition/production-print-batches/prepare", json=payload
        )
        assert second.status_code == 200, second.text
        assert second.json()["replayed"] is True
        assert _without_replay(second.json()) == _without_replay(first_body)

        loaded = client.get(
            f"/api/requisition/production-print-batches/{first_body['batch_id']}"
        )
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["package_fingerprint"] == first_body[
            "package_fingerprint"
        ]

    with production_print_app["session_factory"]() as db:
        logs = db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .where(
                OperationLog.action_code
                == "requisition.production_print_batch.prepared"
            )
        )
        log = db.scalar(
            select(OperationLog).where(
                OperationLog.action_code
                == "requisition.production_print_batch.prepared"
            )
        )
    assert logs == 1
    details = json.loads(log.details)
    assert details["card_count"] == 3
    assert details["page_count"] == 2
    assert details.get("_truncated") is None
    assert [row["source_identity"] for row in details["items"]] == [
        item["source_identity"] for item in chosen
    ]


def test_same_key_different_selection_and_cross_actor_fail_closed(
    production_print_app,
) -> None:
    order_id = production_print_app["supplier_order_id"]
    with TestClient(production_print_app["app"]) as client:
        _login(client, "p132a2-admin")
        package = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        ).json()
        first_item = _selection(package["cards"][0], order_id)
        key = "p1-64b-conflict-selection-001"
        first = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={"idempotency_key": key, "confirmed": True, "items": [first_item]},
        )
        assert first.status_code == 200
        batch_id = first.json()["batch_id"]
        changed = _selection(package["cards"][1], order_id)
        conflict = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={"idempotency_key": key, "confirmed": True, "items": [changed]},
        )
        assert conflict.status_code == 409

        client.cookies.clear()
        _login(client, "p132a2-sales")
        cross_actor = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={"idempotency_key": key, "confirmed": True, "items": [first_item]},
        )
        assert cross_actor.status_code == 409
        assert client.get(
            f"/api/requisition/production-print-batches/{batch_id}"
        ).status_code == 404


def test_empty_or_duplicate_selection_is_rejected_without_audit(
    production_print_app,
) -> None:
    from app.models.audit import OperationLog

    order_id = production_print_app["supplier_order_id"]
    with TestClient(production_print_app["app"]) as client:
        _login(client, "p132a2-admin")
        empty = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={
                "idempotency_key": "p1-64b-empty-selection-001",
                "confirmed": True,
                "items": [],
            },
        )
        assert empty.status_code == 422
        card = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        ).json()["cards"][0]
        item = _selection(card, order_id)
        duplicate = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={
                "idempotency_key": "p1-64b-duplicate-selection-001",
                "confirmed": True,
                "items": [item, item],
            },
        )
        assert duplicate.status_code == 422

    with production_print_app["session_factory"]() as db:
        assert db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .where(
                OperationLog.action_code
                == "requisition.production_print_batch.prepared"
            )
        ) == 0


def test_stale_task_version_or_voided_order_stops_whole_batch_without_audit(
    production_print_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    order_id = production_print_app["supplier_order_id"]
    with TestClient(production_print_app["app"]) as client:
        _login(client, "p132a2-admin")
        cards = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        ).json()["cards"]
        chosen = [_selection(cards[0], order_id), _selection(cards[1], order_id)]
        stale_task_id = chosen[1]["task_versions"][0]["task_id"]
        with production_print_app["session_factory"]() as db:
            task = db.get(ProductionTask, stale_task_id)
            task.version += 1
            db.commit()
        stale = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={
                "idempotency_key": "p1-64b-stale-selection-001",
                "confirmed": True,
                "items": chosen,
            },
        )
        assert stale.status_code == 409, stale.text
        detail = stale.json()["detail"]
        assert detail["code"] == "production_print_batch_invalid_items"
        assert any("版本已变化" in row["reason"] for row in detail["invalid_items"])

        fresh_cards = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        ).json()["cards"]
        with production_print_app["session_factory"]() as db:
            db.get(SupplierRequisitionOrder, order_id).status = "voided"
            db.commit()
        voided = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={
                "idempotency_key": "p1-64b-voided-selection-001",
                "confirmed": True,
                "items": [_selection(fresh_cards[0], order_id)],
            },
        )
        assert voided.status_code == 409

    with production_print_app["session_factory"]() as db:
        assert db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .where(
                OperationLog.action_code
                == "requisition.production_print_batch.prepared"
            )
        ) == 0


def test_fixed_half_page_pagination_is_deterministic() -> None:
    from app.services.requisition_production_print_batch import (
        production_print_batch_pages,
    )

    simple = lambda name: {
        "source_identity": name,
        "components": [{"printing_situation": "无印刷"}],
    }
    complex_card = {
        "source_identity": "complex",
        "components": [
            {
                "printing_situation": "三色印刷",
                "printing_plate_mode": "plate",
                "printing_plates": [{}, {}, {}],
            }
        ],
    }
    pages = production_print_batch_pages(
        [simple("one"), simple("two"), complex_card, simple("three")]
    )
    assert len(pages) == 2
    assert [pages[0]["top"]["source_identity"], pages[0]["bottom"]["source_identity"]] == [
        "one",
        "two",
    ]
    assert pages[1]["full_page"] is False
    assert pages[1]["top"]["source_identity"] == "complex"
    assert pages[1]["bottom"]["source_identity"] == "three"


def test_erp_selection_ui_and_print_page_keep_explicit_mutation_allowlist() -> None:
    for marker in (
        "productionPrintSelections",
        "loadProductionPrintCandidates",
        "toggleProductionPrintCandidate",
        "clearProductionPrintSelections",
        "prepareProductionPrintBatch",
        "批量打印待来料任务单",
        "/api/requisition/production-print-batches/prepare",
        "selection_fingerprint",
        "production_task_versions",
    ):
        assert marker in INDEX
    assert "默认不选" in INDEX
    assert "batch_id" in PRINT_PAGE
    assert "/api/requisition/production-print-batches/" in PRINT_PAGE
    assert 'method:"GET"' in PRINT_PAGE
    assert PRINT_PAGE.count('method:"POST"') == 1
    assert "/api/production/tasks/${encodeURIComponent(row.task_id)}/label-plan-refresh" in PRINT_PAGE
    assert "expected_task_version:row.expected_task_version" in PRINT_PAGE
    assert "expected_product_version:row.expected_product_version" in PRINT_PAGE
    assert PRINT_PAGE.count("window.print()") == 1
    _, loader = _method_body(
        "loadProductionPrintCandidates", "toggleProductionPrintCandidate"
    )
    permission_failure = loader[loader.index("status === 401 || status === 403") :]
    assert "this.productionPrintCandidatePanels = {};" in permission_failure
    assert "this.productionPrintSelections = {};" in permission_failure
    assert permission_failure.index("return false;") < permission_failure.index(
        "items:previous.items || []"
    )


def test_frontend_selection_snapshot_singleflight_and_exact_retry(tmp_path: Path) -> None:
    names = [
        ("productionPrintSelectedItems", "productionPrintTaskVersionText"),
        ("productionPrintCandidateSelected", "productionPrintAttemptUncertain"),
        ("productionPrintAttemptUncertain", "resetProductionPrintBatchOutcome"),
        ("resetProductionPrintBatchOutcome", "loadProductionPrintCandidates"),
        ("toggleProductionPrintCandidate", "clearProductionPrintSelections"),
        ("clearProductionPrintSelections", "productionPrintPayloadItems"),
        ("productionPrintPayloadItems", "prepareProductionPrintBatch"),
        ("prepareProductionPrintBatch", "stockReplenishmentVoidBusy"),
    ]
    methods = {name: _method_body(name, next_name) for name, next_name in names}
    script = f"""
const assert=(condition,message)=>{{if(!condition)throw new Error(message);}};
global.confirm=()=>true;
global.createIdempotencyKey=()=>"fixed-idempotency-key";
let openCount=0;
const windows=[];
global.window={{open:()=>{{openCount+=1;const win={{opener:{{}},location:{{href:""}},closed:false,close(){{this.closed=true;}}}};windows.push(win);return win;}}}};
let pendingResolve;
let postCalls=[];
global.axios={{post:(url,payload)=>{{postCalls.push({{url,payload}});return new Promise(resolve=>{{pendingResolve=resolve;}});}}}};
const vm={{
  productionPrintSelections:{{}},productionPrintSelectionSequence:0,productionPrintBatchBusy:false,
  productionPrintBatchAttempt:null,productionPrintRecoveryUrl:"",productionPrintBatchError:"",productionPrintInvalidItems:[],
  authGeneration:4,user:{{id:9}},toasts:[],showToast(message,error=false){{this.toasts.push({{message,error}});}},
  errorMessage(error){{return error?.response?.data?.detail?.message||error?.response?.data?.detail||error?.message||"error";}},
}};
const FunctionCtor=Function;
{''.join(f'vm.{name}=new FunctionCtor({json.dumps(params, ensure_ascii=False)[1:-1]}{"," if params else ""}{json.dumps(("return async function(){{" + body + "}}") if name == "prepareProductionPrintBatch" else body, ensure_ascii=False)});\n' if name != 'prepareProductionPrintBatch' else f'vm.{name}=new FunctionCtor({json.dumps("return async function(){" + body + "}", ensure_ascii=False)})().bind(vm);\n' for name,(params,body) in methods.items())}
for(const name of ["productionPrintSelectedItems","productionPrintCandidateSelected","productionPrintAttemptUncertain","resetProductionPrintBatchOutcome","toggleProductionPrintCandidate","clearProductionPrintSelections","productionPrintPayloadItems"]) vm[name]=vm[name].bind(vm);
const card=(key,task)=>({{selection_key:key,selection_eligible:true,supplier_order_id:1,source_identity:key,selection_fingerprint:"a".repeat(64),task_versions:[{{task_id:task,version:1}}],product_code:key,product_name:`产品${{key}}`}});
assert(vm.toggleProductionPrintCandidate(card("page-1",11),true)===true,"first explicit selection failed");
assert(vm.toggleProductionPrintCandidate(card("page-2",22),true)===true,"cross-page explicit selection failed");
assert(vm.productionPrintSelectedItems().map(row=>row.selection_key).join(",")==="page-1,page-2","selection order was not stable");
const firstPromise=vm.prepareProductionPrintBatch();
const duplicate=await vm.prepareProductionPrintBatch();
assert(duplicate===false&&openCount===1&&postCalls.length===1,"double click opened or posted twice");
assert(vm.toggleProductionPrintCandidate(card("page-3",33),true)===false,"busy selection was mutable");
const frozen=JSON.stringify(postCalls[0].payload.items);
pendingResolve({{data:{{print_url:"/requisition-production-print.html?batch_id="+"b".repeat(64),batch_id:"b".repeat(64),package_fingerprint:"c".repeat(64),card_count:2,replayed:false}}}});
assert(await firstPromise===true,"prepared batch did not resolve");
assert(JSON.stringify(postCalls[0].payload.items)===frozen,"in-flight selection snapshot changed");
assert(windows[0].location.href.includes("batch_id="),"allowed popup was not navigated");
assert(vm.productionPrintRecoveryUrl.includes("batch_id="),"persistent recovery link missing");
assert(vm.toggleProductionPrintCandidate(card("page-3",33),true)===true,"new explicit selection after commit failed");
assert(vm.productionPrintRecoveryUrl===""&&vm.productionPrintBatchAttempt===null,"changed selection reused old frozen batch");

let uncertainCalls=[];
global.axios={{post:async(url,payload)=>{{uncertainCalls.push({{url,payload}});throw new TypeError("network");}}}};
const uncertainPromise=vm.prepareProductionPrintBatch();
assert(await uncertainPromise===false,"network uncertainty was treated as success");
assert(vm.productionPrintAttemptUncertain()===true,"uncertain attempt was not retained");
const uncertainKey=vm.productionPrintBatchAttempt.idempotencyKey;
const uncertainItems=JSON.stringify(vm.productionPrintBatchAttempt.items);
assert(vm.toggleProductionPrintCandidate(card("page-4",44),true)===false,"uncertain snapshot was mutable");
global.axios={{post:async(url,payload)=>{{uncertainCalls.push({{url,payload}});return {{data:{{print_url:"/requisition-production-print.html?batch_id="+"d".repeat(64),batch_id:"d".repeat(64),package_fingerprint:"e".repeat(64),card_count:3,replayed:true}}}};}}}};
assert(await vm.prepareProductionPrintBatch()===true,"exact retry did not recover");
assert(uncertainCalls.length===2,"uncertain retry count wrong");
assert(uncertainCalls[1].payload.idempotency_key===uncertainKey,"retry changed idempotency key");
assert(JSON.stringify(uncertainCalls[1].payload.items)===uncertainItems,"retry changed frozen items");
"""
    _run_node(script, tmp_path, "p1-64b-selection-behavior.mjs")

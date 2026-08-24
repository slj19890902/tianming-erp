from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX
    assert next_signature in INDEX
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def test_structured_409_releases_single_and_batch_attempts_and_drops_unselected_rows(
    tmp_path: Path,
) -> None:
    error_message = _method_body("errorMessage(error) {", "normalizeValidationErrors(")
    batch_receive = _method_body(
        "async batchReceiveIncoming() {",
        "async receiveIncoming(row) {",
    )
    single_receive = _method_body(
        "async receiveIncoming(row) {",
        "async acceptShortIncoming(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
const toasts=[];const prepared=[];const writes=[];let keySequence=0;
global.confirm=()=>true;
global.createIdempotencyKey=()=>`p1-91v-${{++keySequence}}`;
global.axios={{put:async(url,payload)=>{{writes.push({{url,payload:JSON.parse(JSON.stringify(payload))}});return {{data:{{succeeded:1,failed:0,results:[{{item_id:"good",success:true,item:{{material_status:"received"}}}}]}}}};}}}};
const business409=()=>Object.assign(new Error("Request failed with status code 409"),{{response:{{status:409,data:{{detail:{{code:"MATERIAL_MASTER_PRICE_REQUIRED",message:"材质 J416D 的采购价格合同不完整：缺少有效采购币种；缺少是否含税；缺少采购税率。请在材质主档补齐后重新实收。"}}}}}}}});
const rows=[
  {{item_id:"td",incoming_quantity:32,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",product_code:"TD004"}},
  {{item_id:"cpn",incoming_quantity:30,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",product_code:"CPN087073"}},
  {{item_id:"good",incoming_quantity:18,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",product_code:"GOOD"}},
];
const vm={{
  authGeneration:1,user:{{id:7}},incomingPendingAppliedPage:1,
  incomingPending:rows,incomingSelected:{{td:true,cpn:false,good:false}},
  incomingReceiveAttempts:{{}},incomingBatchReceiveAttempt:null,
  canReceiveIncoming(row){{return row.material_status==="pending";}},
  async ensureAutomaticPurchaseReceiptFact(row){{prepared.push(row.item_id);if(row.item_id==="td")throw business409();if(row.item_id==="good")row.incoming_quantity=999;}},
  incomingPayload(row){{return {{item_id:row.item_id,received_quantity:Number(row.incoming_quantity),resolution_action:null,resolution_reason:null,surplus_location_id:null}};}},
  showIncomingNextStepGuide(){{}},async refreshIncomingAfterWrite(){{return true;}},
  showToast(message,isError){{toasts.push({{message:String(message),isError:!!isError}});}},
}};
vm.errorMessage=new FunctionCtor("error",{json.dumps(error_message, ensure_ascii=False)}).bind(vm);
vm.batchReceiveIncoming=new AsyncFunction({json.dumps(batch_receive, ensure_ascii=False)}).bind(vm);
vm.receiveIncoming=new AsyncFunction("row",{json.dumps(single_receive, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  await vm.batchReceiveIncoming();
  expect(prepared.join("|")==="td","unselected CPN entered failed batch preparation");
  expect(writes.length===0,"business 409 reached the batch receive endpoint");
  expect(vm.incomingBatchReceiveAttempt===null,"explicit preparation 409 left batch busy");
  expect(toasts.at(-1).message.includes("缺少有效采购币种")&&!toasts.at(-1).message.includes("Request failed"),"structured business message was discarded");

  vm.incomingSelected={{td:false,cpn:false,good:true}};
  await vm.batchReceiveIncoming();
  expect(prepared.join("|")==="td|good","old rejected selection polluted the next batch");
  expect(writes.length===1&&writes[0].url==="/api/incoming/batch-receive","corrected batch was not submitted");
  expect(writes[0].payload.items.length===1&&writes[0].payload.items[0].item_id==="good","unselected row leaked into corrected batch");
  expect(writes[0].payload.items[0].received_quantity===18,"batch quantity was not frozen at click time");
  expect(vm.incomingBatchReceiveAttempt===null,"successful batch did not release state");

  writes.length=0;prepared.length=0;toasts.length=0;
  vm.incomingReceiveAttempts={{}};
  vm.ensureAutomaticPurchaseReceiptFact=async()=>{{throw business409();}};
  rows[0].incoming_quantity=32;
  await vm.receiveIncoming(rows[0]);
  expect(writes.length===0,"single business 409 reached receive endpoint");
  expect(!vm.incomingReceiveAttempts.td,"single preparation 409 left row busy");
  expect(toasts.at(-1).message.includes("材质 J416D")&&!toasts.at(-1).message.includes("Request failed"),"single receive exposed Axios English text");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-91v-incoming-409-release.js")


def test_batch_preparation_is_single_flight_and_always_releases_busy(
    tmp_path: Path,
) -> None:
    error_message = _method_body("errorMessage(error) {", "normalizeValidationErrors(")
    batch_receive = _method_body(
        "async batchReceiveIncoming() {",
        "async receiveIncoming(row) {",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const FunctionCtor=Function;
let rejectPreparation;let preparationCalls=0;let writeCalls=0;
global.createIdempotencyKey=()=>"unused";
global.axios={{put:async()=>{{writeCalls++;throw new Error("batch endpoint must not run");}}}};
const row={{item_id:"td",incoming_quantity:32,source_type:"supplier_order",material_status:"pending",requisition_status:"已报料",product_code:"TD004"}};
const vm={{
  incomingPending:[row],incomingSelected:{{td:true}},incomingBatchReceiveAttempt:null,
  canReceiveIncoming(){{return true;}},
  ensureAutomaticPurchaseReceiptFact(){{preparationCalls++;return new Promise((_resolve,reject)=>{{rejectPreparation=reject;}});}},
  incomingPayload(){{throw new Error("payload must not be built");}},
  showToast(){{}},
}};
vm.errorMessage=new FunctionCtor("error",{json.dumps(error_message, ensure_ascii=False)}).bind(vm);
vm.batchReceiveIncoming=new AsyncFunction({json.dumps(batch_receive, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const first=vm.batchReceiveIncoming();
  const doubleClick=vm.batchReceiveIncoming();
  await new Promise(resolve=>setImmediate(resolve));
  expect(preparationCalls===1,"double click started duplicate receipt-fact preparation");
  expect(vm.incomingBatchReceiveAttempt?.saving===true,"preparation was not marked busy");
  rejectPreparation(Object.assign(new Error("Request failed with status code 409"),{{response:{{status:409,data:{{detail:{{code:"MATERIAL_MASTER_PRICE_REQUIRED",message:"采购价格合同不完整"}}}}}}}}));
  await Promise.all([first,doubleClick]);
  expect(writeCalls===0,"failed preparation reached batch write");
  expect(vm.incomingBatchReceiveAttempt===null,"failed preparation did not release busy");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-91v-incoming-preparation-single-flight.js")


def test_material_contract_error_lists_all_missing_fields_in_chinese() -> None:
    from app.api.materials import MaterialPayload
    from app.api.requisition import _material_master_price_contract
    from app.models.material import Material
    from app.services.purchase_receipt_facts import PurchaseReceiptFactValidationError

    material = Material(
        id=386,
        code="J416D",
        quote_price=Decimal("2.61"),
        price_unit="元/㎡",
        purchase_currency=None,
        purchase_tax_included=None,
        purchase_tax_rate=None,
    )
    with pytest.raises(PurchaseReceiptFactValidationError) as captured:
        _material_master_price_contract(material)
    message = str(captured.value)
    assert "材质 J416D" in message
    assert "缺少有效采购币种" in message
    assert "缺少是否含税" in message
    assert "缺少采购税率" in message
    assert "Request failed" not in message

    payload = MaterialPayload(code="ABC", layer_count=3, quote_price=Decimal("1.25"))
    assert payload.price_unit == "元/㎡"
    assert payload.purchase_currency == "CNY"
    assert payload.purchase_tax_included is True
    assert payload.purchase_tax_rate == Decimal("0.13")


def test_audited_material_contract_backfill_is_exact_and_idempotent(
    isolated_database_path: Path,
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.audit import OperationLog
    from app.models.material import Material
    from app.models.material_price_history import (
        MaterialPriceAdjustmentBatch,
        MaterialPriceHistory,
    )
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.user import User
    from scripts.admin.backfill_material_purchase_contracts import (
        apply_backfill,
        inspect_plan,
    )

    engine = create_sqlite_engine(isolated_database_path)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            User(
                username="admin",
                password_hash=hash_password("PytestOnly123!"),
                role="admin",
                real_name="测试管理员",
                display_name="测试管理员",
                is_active=True,
                must_change_password=False,
            )
        )
        db.add_all(
            [
                Material(
                    code="J416D",
                    quote_price=Decimal("2.61"),
                    price_unit="元/㎡",
                    supplier_name="测试纸厂",
                    is_active=True,
                    version=1,
                ),
                Material(
                    code="READY",
                    quote_price=Decimal("1.23"),
                    price_unit="元/张",
                    purchase_currency="CNY",
                    purchase_tax_included=True,
                    purchase_tax_rate=Decimal("0.13"),
                    is_active=True,
                    version=1,
                ),
                Material(code="NOPRICE", quote_price=None, is_active=True, version=1),
            ]
        )
        db.commit()
    engine.dispose()

    before_hash = _sha256(isolated_database_path)
    plan = inspect_plan(isolated_database_path)
    assert plan["status"] == "ready"
    assert plan["target_count"] == 1
    assert plan["targets"][0]["code"] == "J416D"
    assert _sha256(isolated_database_path) == before_hash

    result = apply_backfill(
        database=isolated_database_path,
        backup_dir=tmp_path / "backups",
        actor_username="admin",
        expected_sha256=before_hash,
        report_path=tmp_path / "report.json",
    )
    assert result["changed"] is True
    assert Path(result["backup"]["path"]).is_file()
    assert result["plan_after"]["status"] == "already_applied"

    engine = create_sqlite_engine(isolated_database_path)
    with Session(engine) as db:
        target = db.scalar(select(Material).where(Material.code == "J416D"))
        ready = db.scalar(select(Material).where(Material.code == "READY"))
        assert target is not None and ready is not None
        assert target.quote_price == Decimal("2.6100")
        assert target.price_unit == "元/㎡"
        assert target.purchase_currency == "CNY"
        assert target.purchase_tax_included is True
        assert target.purchase_tax_rate == Decimal("0.130000")
        assert target.version == 2
        assert ready.version == 1
        assert db.scalar(select(func.count()).select_from(MaterialPriceHistory)) == 1
        assert db.scalar(select(func.count()).select_from(MaterialPriceAdjustmentBatch)) == 1
        assert db.scalar(select(func.count()).select_from(MasterDataObjectVersion)) == 2
        assert db.scalar(select(func.count()).select_from(OperationLog)) == 2
    engine.dispose()

    applied_hash = _sha256(isolated_database_path)
    replay = apply_backfill(
        database=isolated_database_path,
        backup_dir=tmp_path / "backups",
        actor_username="admin",
        expected_sha256=applied_hash,
    )
    assert replay["changed"] is False
    assert _sha256(isolated_database_path) == applied_hash

    engine = create_sqlite_engine(isolated_database_path)
    with Session(engine) as db:
        db.add(
            Material(
                code="CONFLICT",
                quote_price=Decimal("3.00"),
                price_unit="元/㎡",
                purchase_currency="USD",
                purchase_tax_included=False,
                purchase_tax_rate=Decimal("0.06"),
                is_active=True,
                version=1,
            )
        )
        db.commit()
    engine.dispose()
    conflict_hash = _sha256(isolated_database_path)
    conflict_plan = inspect_plan(isolated_database_path)
    assert conflict_plan["status"] == "blocked"
    assert conflict_plan["blocker_count"] == 1
    with pytest.raises(RuntimeError, match="拒绝静默覆盖"):
        apply_backfill(
            database=isolated_database_path,
            backup_dir=tmp_path / "backups",
            actor_username="admin",
            expected_sha256=conflict_hash,
        )
    assert _sha256(isolated_database_path) == conflict_hash

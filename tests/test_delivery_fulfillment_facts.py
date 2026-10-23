"""Real SQLite tests; no production database, migrations, or browser required."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from threading import Barrier

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_current_user, get_db
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.delivery_fulfillment_fact import DeliveryFulfillmentRevision, DeliveryFulfillmentSegment
from app.models.finance import Invoice, ReturnReceipt, ReturnReceiptItem, Statement, StatementItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.services import delivery_fulfillment_facts as service
from app.services.delivery_fulfillment_facts import FulfillmentError, FulfillmentRequest, FulfillmentSubmitRequest


@pytest.fixture
def fulfillment_db(tmp_path):
    engine = create_sqlite_engine(tmp_path / "fulfillment.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([User(id=1, username="admin", role="admin", real_name="管理员", password_hash="test"),
                    User(id=2, username="sales", role="sales", real_name="业务", password_hash="test", customer_access_mode="selected"),
                    User(id=3, username="other-admin", role="admin", real_name="另一管理员", password_hash="test"),
                    Customer(id=46, name="高泰", customer_code="GT"), Customer(id=47, name="异属客户", customer_code="OTHER")])
        db.flush()
        db.add_all([Product(id=3285, customer_id=46, product_code="3D30515", customer_material_code="3D30515", product_name="纸箱50", box_category="normal"),
                    Product(id=2402, customer_id=46, product_code="3D60089", customer_material_code="3D60089", product_name="纸箱800", box_category="normal"),
                    Order(id=9754, order_number="GT-9754", customer_id=46, order_date=date(2026, 9, 17), status="pending_payment", total_amount=Decimal("5030"))])
        db.flush()
        db.add_all([OrderItem(id=10375, order_id=9754, product_id=3285, quantity=50, delivered_quantity=50, unit_price=Decimal("6.2"), subtotal=Decimal("310"), snapshot_product_name="纸箱50"),
                    OrderItem(id=10376, order_id=9754, product_id=2402, quantity=800, delivered_quantity=800, unit_price=Decimal("5.9"), subtotal=Decimal("4720"), snapshot_product_name="纸箱800"),
                    Delivery(id=142, customer_id=46, delivery_number="GT-20260924-002", delivery_date=date(2026, 9, 24), total_quantity=850,
                             status="dispatched", dispatched_at=datetime(2026, 9, 24, 10), dispatched_by=1)])
        db.flush()
        db.add_all([DeliveryItem(id=817, delivery_id=142, order_item_id=10375, delivered_quantity=50),
                    DeliveryItem(id=818, delivery_id=142, order_item_id=10376, delivered_quantity=800),
                    ReturnReceipt(id=129, delivery_id=142, actual_received_date=date(2026, 9, 24), reconciliation_month="2026-09", status="confirmed"),
                    Statement(id=57, customer_id=46, statement_number="ST-202609-020", statement_month="2026-09", confirmation_status="confirmed",
                              total_receivable=Decimal("5030"), invoiced_amount=Decimal("5030"), version=2, ledger_version=2)])
        db.flush()
        db.add_all([ReturnReceiptItem(id=1001, return_receipt_id=129, delivery_item_id=817, actual_received_quantity=50),
                    ReturnReceiptItem(id=1002, return_receipt_id=129, delivery_item_id=818, actual_received_quantity=800),
                    Invoice(id=55, statement_id=57, invoice_number="2632200000779827053101", invoice_date=date(2026, 9, 24), invoice_amount=Decimal("5030"), invoice_status="issued")])
        db.flush()
        db.add_all([StatementItem(id=2001, statement_id=57, source_customer_id=46, return_receipt_item_id=1001, actual_received_quantity=50,
                                 unit_price_snapshot=Decimal("6.2"), unit_cost_snapshot=Decimal("1"), receivable_amount=Decimal("310"), gross_profit_amount=Decimal("260")),
                    StatementItem(id=2002, statement_id=57, source_customer_id=46, return_receipt_item_id=1002, actual_received_quantity=800,
                                 unit_price_snapshot=Decimal("5.9"), unit_cost_snapshot=Decimal("1"), receivable_amount=Decimal("4720"), gross_profit_amount=Decimal("3920"))])
        db.commit()
    yield factory
    engine.dispose()


def _payload(**changes):
    evidence = dict(source_file="高泰.xls", source_sha256="a" * 64, sheet="对账单", rows=[8, 9], confirmation="客户确认50只9月22日；800只9月22日和10月12日各400",
                    quantity_basis_confirmed=True, customer_unit="只", physical_unit="只")
    value = dict(customer_id=46, reason="保留已开票原单并补全实际交付分批", evidence=evidence, segments=[
        dict(delivery_item_id=817, actual_date="2026-09-22", quantity=50, evidence=evidence),
        dict(delivery_item_id=818, actual_date="2026-09-22", quantity=400, evidence=evidence),
        dict(delivery_item_id=818, actual_date="2026-10-12", quantity=400, evidence=evidence)])
    value.update(changes)
    return FulfillmentRequest.model_validate(value)


def _submit_payload(db, actor, key="fulfillment-test-001", payload=None):
    payload = payload or _payload()
    preview = service.preview_delivery_fulfillment(db, actor, 142, payload)
    return FulfillmentSubmitRequest(**payload.model_dump(), operation_key=key, reviewed_hash=preview["reviewed_hash"], expected_revision=preview["expected_revision"])


def _original_rows(db):
    return {name: [dict(r) for r in db.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
            for name, table in Base.metadata.tables.items()
            if not name.startswith("delivery_fulfillment_") and name != "operation_logs"}


def test_append_preserves_every_original_table_and_replays(fulfillment_db):
    with fulfillment_db() as db:
        actor = db.get(User, 1)
        before = _original_rows(db)
        request = _submit_payload(db, actor)
        result = service.append_delivery_fulfillment(db, actor, 142, request)
        assert result["revision"] == 1 and not result["replayed"]
        assert db.connection().connection.driver_connection.in_transaction
        # The real outer transaction has not committed at a SAVEPOINT boundary.
        with fulfillment_db() as other:
            assert other.scalar(select(DeliveryFulfillmentRevision.id)) is None
        db.commit()
        assert _original_rows(db) == before
        assert db.scalar(select(Statement.total_receivable).where(Statement.id == 57)) == Decimal("5030")
        assert len(db.scalars(select(OperationLog)).all()) == 1
        replay = service.append_delivery_fulfillment(db, actor, 142, request)
        assert replay["replayed"] and replay["revision_id"] == result["revision_id"]
        db.commit()
        assert len(db.scalars(select(OperationLog)).all()) == 1
        assert _original_rows(db) == before


def test_preview_no_dml_even_expired_actor_and_pending_work(fulfillment_db):
    with fulfillment_db() as db:
        actor = db.get(User, 1)
        db.expire(actor)
        pending = Customer(name="未保存客户")
        db.add(pending)
        statements = []
        def observe(_c, _cur, sql, *_args):
            statements.append(sql)
        event.listen(db.get_bind(), "before_cursor_execute", observe)
        try:
            result = service.preview_delivery_fulfillment(db, actor, 142, _payload())
            assert result["can_submit"] and result["expected_revision"] == 0
            assert pending in db.new and pending.id is None
            assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", observe)


@pytest.mark.parametrize("change,code", [("permission", "fulfillment_permission_denied"), ("customer", "fulfillment_customer_denied"),
    ("quantity", "fulfillment_quantity_mismatch"), ("missing", "fulfillment_quantity_mismatch"), ("duplicate", "fulfillment_quantity_mismatch"),
    ("early", "fulfillment_invalid_date"), ("future", "fulfillment_invalid_date"), ("unconfirmed", "fulfillment_unsupported_chain"),
    ("underinvoice", "fulfillment_not_fully_invoiced"), ("settled", "fulfillment_unsupported_chain"), ("ratio", "fulfillment_unsupported_source")])
def test_preview_rejects_invalid_chain(fulfillment_db, change, code):
    with fulfillment_db() as db:
        actor = db.get(User, 2 if change == "permission" else 1)
        value = _payload().model_dump()
        if change == "customer": value["customer_id"] = 47
        if change == "quantity": value["segments"][1]["quantity"] = 399
        if change == "missing": value["segments"] = value["segments"][1:]
        if change == "duplicate": value["segments"].append(value["segments"][0])
        if change == "early": value["segments"][0]["actual_date"] = date(2026, 9, 16)
        if change == "future": value["segments"][0]["actual_date"] = date(2099, 1, 1)
        if change == "unconfirmed": db.get(Statement, 57).confirmation_status = "draft"
        if change == "underinvoice": db.get(Invoice, 55).invoice_amount = Decimal("5000")
        if change == "settled": db.get(Statement, 57).settled_amount = Decimal("1")
        if change == "ratio":
            import json
            db.get(DeliveryItem, 817).quantity_contract_json = json.dumps(dict(schema=1, customer_id=46, product_id=3285, customer_basis=1, physical_basis=2, customer_quantity=50, physical_quantity=100, customer_unit="只", physical_unit="片"))
        db.commit()
        with pytest.raises(FulfillmentError) as error:
            service.preview_delivery_fulfillment(db, actor, 142, FulfillmentRequest(**value))
        assert error.value.code == code
        assert db.scalar(select(DeliveryFulfillmentRevision.id)) is None


def test_stale_preview_and_key_conflicts(fulfillment_db):
    with fulfillment_db() as db:
        actor = db.get(User, 1)
        request = _submit_payload(db, actor)
        db.get(Statement, 57).ledger_version += 1
        db.commit()
        with pytest.raises(FulfillmentError, match="重新预览"):
            service.append_delivery_fulfillment(db, actor, 142, request)
        fresh = _submit_payload(db, actor)
        service.append_delivery_fulfillment(db, actor, 142, fresh)
        db.commit()
        for different in (fresh.model_copy(update={"reason": "不同载荷"}),):
            with pytest.raises(FulfillmentError) as error:
                service.append_delivery_fulfillment(db, actor, 142, different)
            assert error.value.code == "fulfillment_idempotency_conflict"
        with pytest.raises(FulfillmentError) as error:
            service.append_delivery_fulfillment(db, db.get(User, 3), 142, fresh)
        assert error.value.code == "fulfillment_idempotency_conflict"


def test_late_failure_rolls_back_facts_and_audit(fulfillment_db, monkeypatch):
    original = service.append_audit_event
    def fail_after_audit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("late failure after audit flush")
    monkeypatch.setattr(service, "append_audit_event", fail_after_audit)
    with fulfillment_db() as db:
        actor = db.get(User, 1)
        before = _original_rows(db)
        request = _submit_payload(db, actor)
        with pytest.raises(RuntimeError, match="late failure"):
            service.append_delivery_fulfillment(db, actor, 142, request)
        db.commit()
        assert db.scalar(select(DeliveryFulfillmentRevision.id)) is None
        assert db.scalar(select(DeliveryFulfillmentSegment.id)) is None
        assert db.scalar(select(OperationLog.id)) is None
        assert _original_rows(db) == before


@pytest.mark.parametrize("same_key", [True, False])
def test_two_real_sessions_serialize_duplicate_or_stale_submit(fulfillment_db, same_key):
    barrier = Barrier(2)
    def worker(index):
        with fulfillment_db() as db:
            actor = db.get(User, 1)
            payload = _submit_payload(db, actor, key="concurrent-same" if same_key else f"concurrent-{index}")
            barrier.wait(timeout=10)
            try:
                result = service.append_delivery_fulfillment(db, actor, 142, payload)
                db.commit()
                return "replay" if result["replayed"] else "created"
            except FulfillmentError as error:
                return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [1, 2]))
    assert sorted(results) == sorted(["created", "replay" if same_key else "fulfillment_stale_preview"])
    with fulfillment_db() as db:
        assert len(db.scalars(select(DeliveryFulfillmentRevision)).all()) == 1
        assert len(db.scalars(select(DeliveryFulfillmentSegment)).all()) == 3
        assert len(db.scalars(select(OperationLog)).all()) == 1


def test_append_only_sealed_and_source_guards(fulfillment_db):
    with fulfillment_db() as db:
        actor = db.get(User, 1)
        service.append_delivery_fulfillment(db, actor, 142, _submit_payload(db, actor))
        db.commit()
        forbidden = [
            "UPDATE delivery_fulfillment_revisions SET reason='overwrite'", "DELETE FROM delivery_fulfillment_revisions",
            "UPDATE delivery_fulfillment_segments SET quantity=1", "DELETE FROM delivery_fulfillment_segments",
            "INSERT INTO delivery_fulfillment_segments(revision_id,delivery_item_id,actual_date,quantity,evidence_json) VALUES(1,817,'2026-09-23',1,'{}')",
            "UPDATE sales_deliveries SET delivery_date='2026-09-22' WHERE id=142", "UPDATE sales_deliveries SET status='voided' WHERE id=142",
            "UPDATE sales_delivery_items SET delivered_quantity=801 WHERE id=818", "DELETE FROM sales_delivery_items WHERE id=818",
            "INSERT INTO sales_delivery_items(delivery_id,order_item_id,delivered_quantity) VALUES(142,10375,1)"]
        for sql in forbidden:
            with pytest.raises(IntegrityError): db.execute(text(sql))
            db.rollback()
        db.execute(text("UPDATE sales_deliveries SET printed_at='2026-10-22 10:00:00' WHERE id=142"))
        db.commit()
        revised = _submit_payload(db, actor, "next-revision-001")
        result = service.append_delivery_fulfillment(db, actor, 142, revised)
        db.commit()
        assert result["revision"] == 2
        assert len(db.scalars(select(DeliveryFulfillmentSegment)).all()) == 6


def test_api_commits_and_existing_date_route_stays_locked(fulfillment_db):
    from app.api.delivery_fulfillment_facts import router
    from app.api.deliveries import router as delivery_router
    app = FastAPI()
    app.include_router(router, prefix="/api/deliveries")
    app.include_router(delivery_router, prefix="/api/deliveries")
    def get_test_db():
        with fulfillment_db() as db: yield db
    from fastapi import Depends
    def actor(db=Depends(get_db)): return db.get(User, 1)
    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_current_user] = actor
    with TestClient(app) as client:
        payload = _payload().model_dump(mode="json")
        preview = client.post("/api/deliveries/142/fulfillment-facts/preview", json=payload)
        assert preview.status_code == 200, preview.text
        values = preview.json()
        response = client.post("/api/deliveries/142/fulfillment-facts", json=dict(payload,
            reviewed_hash=values["reviewed_hash"], expected_revision=values["expected_revision"], operation_key="api-fulfillment-001"))
        assert response.status_code == 201, response.text
        locked = client.put("/api/deliveries/142/actual-date", json=dict(actual_delivery_date="2026-09-22", expected_version=1, idempotency_key="date-still-locked"))
        assert locked.status_code == 409, locked.text
        assert locked.json()["detail"]["code"] == "delivery_actual_date_locked"
    with fulfillment_db() as db:
        assert len(db.scalars(select(DeliveryFulfillmentRevision)).all()) == 1
        assert db.get(Delivery, 142).delivery_date == date(2026, 9, 24)


def test_cost_supplement_phantom_invalidates_preview_and_is_frozen(fulfillment_db):
    from app.models.material_cost_supplement import FinanceMaterialCostSupplement
    with fulfillment_db() as db:
        actor = db.get(User, 1)
        request = _submit_payload(db, actor)
        supplement = FinanceMaterialCostSupplement(delivery_item_id=818, month="2026-09", source_kind="legacy_reference", source_id=818,
            target_fingerprint="b" * 64, target_json="{}", quantity_limit=800, unit_cost=Decimal("1.25"), currency="CNY",
            reference_kind="purchase", evidence_json="{}", evidence_fingerprint="c" * 64, algorithm_version="test",
            batch_id="fulfillment-test", reason="保留原成本参考", created_by=1)
        db.add(supplement)
        db.commit()
        with pytest.raises(FulfillmentError) as error:
            service.append_delivery_fulfillment(db, actor, 142, request)
        assert error.value.code == "fulfillment_stale_preview"
        fresh = _submit_payload(db, actor)
        originals = _original_rows(db)
        result = service.append_delivery_fulfillment(db, actor, 142, fresh)
        db.commit()
        import json
        revision = db.get(DeliveryFulfillmentRevision, result["revision_id"])
        frozen = json.loads(revision.before_json)["finance_material_cost_supplements"]
        assert frozen[0]["quantity_limit"] == 800 and Decimal(frozen[0]["unit_cost"]) == Decimal("1.25")
        assert _original_rows(db) == originals


def test_permissions_scope_and_transient_actor_fail_before_snapshot(fulfillment_db, monkeypatch):
    from app.models.access_control import UserPermissionOverride, UserCustomerScope
    with fulfillment_db() as db:
        actor = db.get(User, 2)
        db.add_all([UserPermissionOverride(user_id=2, permission_code=p, is_allowed=True) for p in
                    ("deliveries.execute", "finance.return_receipt.period.adjust", "finance.view")])
        db.add(UserCustomerScope(user_id=2, customer_id=47))
        db.commit()
        db.expire(actor)
        def must_not_read(*_args): raise AssertionError("customer facts read before authorization")
        monkeypatch.setattr(service, "_snapshot", must_not_read)
        with pytest.raises(FulfillmentError) as error:
            service.preview_delivery_fulfillment(db, actor, 142, _payload())
        assert error.value.code == "fulfillment_customer_denied"
        transient = User(username="unsaved-admin", role="admin", is_active=True)
        db.add(transient)
        with pytest.raises(FulfillmentError) as error:
            service.preview_delivery_fulfillment(db, transient, 142, _payload())
        assert error.value.code == "fulfillment_permission_denied"
        assert transient.id is None


def test_unsealed_incomplete_or_orphan_facts_cannot_commit(fulfillment_db):
    with fulfillment_db() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        db.execute(DeliveryFulfillmentSegment.__table__.insert().values(revision_id=1, delivery_item_id=817, actual_date=date(2026, 9, 22), quantity=50, evidence_json="{}"))
        with pytest.raises(IntegrityError):
            db.execute(DeliveryFulfillmentRevision.__table__.insert().values(id=1, delivery_id=142, customer_id=46, revision=1,
                previous_revision_id=None, operation_key="incomplete-source", request_hash="a" * 64, reviewed_hash="b" * 64,
                before_json="{}", evidence_json="{}", reason="incomplete", actor_id=1))
        db.rollback()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        db.execute(DeliveryFulfillmentSegment.__table__.insert().values(revision_id=99, delivery_item_id=817, actual_date=date(2026, 9, 22), quantity=50, evidence_json="{}"))
        with pytest.raises(IntegrityError): db.commit()
        db.rollback()
        assert db.scalar(select(DeliveryFulfillmentSegment.id)) is None


def test_caller_rollback_removes_success_and_new_version_requires_review(fulfillment_db):
    with fulfillment_db() as db:
        actor = db.get(User, 1)
        request = _submit_payload(db, actor)
        service.append_delivery_fulfillment(db, actor, 142, request)
        db.rollback()
        assert db.scalar(select(DeliveryFulfillmentRevision.id)) is None
        assert db.scalar(select(OperationLog.id)) is None
        service.append_delivery_fulfillment(db, actor, 142, request)
        db.commit()
        with pytest.raises(FulfillmentError) as error:
            service.append_delivery_fulfillment(db, actor, 142, request.model_copy(update={"operation_key": "second-old-review"}))
        assert error.value.code == "fulfillment_stale_preview"


def test_submit_rejects_pending_changes_and_missing_guard(fulfillment_db):
    with fulfillment_db() as db:
        actor = db.get(User, 1)
        request = _submit_payload(db, actor)
        pending = Customer(name="pending")
        db.add(pending)
        with pytest.raises(FulfillmentError) as error:
            service.append_delivery_fulfillment(db, actor, 142, request)
        assert error.value.code == "fulfillment_session_dirty" and pending in db.new
        db.rollback()
        db.execute(text("DROP TRIGGER delivery_fulfillment_segments_delete_immutable"))
        db.commit()
        with pytest.raises(FulfillmentError) as error:
            service.append_delivery_fulfillment(db, actor, 142, request)
        assert error.value.code == "fulfillment_schema_not_ready"


@pytest.mark.parametrize("field,value", [("quantity", 1.5), ("quantity", True), ("actual_date", "2026-09-22T01:00:00"), ("actual_date", 0)])
def test_payload_rejects_ambiguous_numbers_and_dates(field, value):
    from pydantic import ValidationError
    data = _payload().model_dump()
    data["segments"][0][field] = value
    with pytest.raises(ValidationError): FulfillmentRequest.model_validate(data)


def test_revisions_reference_prior_snapshots_without_recursive_growth(fulfillment_db):
    import hashlib
    import json
    with fulfillment_db() as db:
        actor=db.get(User,1)
        frozen=[]
        for index in range(5):
            result=service.append_delivery_fulfillment(db,actor,142,_submit_payload(db,actor,f"complete-revision-{index}"))
            db.commit()
            frozen.append(db.get(DeliveryFulfillmentRevision,result["revision_id"]).before_json)
        for index, raw in enumerate(frozen):
            prior=json.loads(raw)["delivery_fulfillment_revisions"]
            assert len(prior)==index
            for entry in prior:
                assert "before_json" not in entry
                assert entry["before_json_sha256"]==hashlib.sha256(frozen[entry["revision"]-1].encode()).hexdigest()
        increments=[len(frozen[i])-len(frozen[i-1]) for i in range(1,len(frozen))]
        assert max(increments)<min(increments)*1.2
        assert [r.before_json for r in db.scalars(select(DeliveryFulfillmentRevision).order_by(DeliveryFulfillmentRevision.id))]==frozen


def test_quantity_basis_invoice_overage_and_preview_guards_fail_closed(fulfillment_db):
    with fulfillment_db() as db:
        actor=db.get(User,1)
        payload=_payload()
        payload.evidence.quantity_basis_confirmed=False
        with pytest.raises(FulfillmentError) as error: service.preview_delivery_fulfillment(db,actor,142,payload)
        assert error.value.code=="fulfillment_quantity_basis_unproven"
        db.get(Invoice,55).invoice_amount=Decimal("5031")
        db.get(Statement,57).invoiced_amount=Decimal("5031")
        db.commit()
        with pytest.raises(FulfillmentError) as error: service.preview_delivery_fulfillment(db,actor,142,_payload())
        assert error.value.code=="fulfillment_not_fully_invoiced"
        db.execute(text("DROP TRIGGER delivery_fulfillment_original_delivery_delete"))
        db.commit()
        with pytest.raises(FulfillmentError) as error: service.preview_delivery_fulfillment(db,actor,142,_payload())
        assert error.value.code=="fulfillment_schema_not_ready"


def test_ui_preview_edit_retry_conflict_and_auth_boundaries(tmp_path):
    import json
    from tests.test_p1_46_common_box_final_layout import _method, _run_node
    methods=("fulfillmentItemLabel", "deliveryFulfillmentCurrent", "openDeliveryFulfillment", "closeDeliveryFulfillment",
             "invalidateDeliveryFulfillmentPreview", "deliveryFulfillmentPayload", "previewDeliveryFulfillment", "submitDeliveryFulfillment")
    asynchronous={"openDeliveryFulfillment", "previewDeliveryFulfillment", "submitDeliveryFulfillment"}
    bindings=[]
    for name in methods:
        params,body=_method(name)
        bindings.append(f"vm[{json.dumps(name)}]=new {'AsyncFunction' if name in asynchronous else 'Function'}({json.dumps(params)},{json.dumps(body)}).bind(vm);")
    source=r"""
const assert=require('node:assert/strict'),AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const stored=new Map(),calls=[],toasts=[];let key=0,mode='network';
global.createIdempotencyKey=()=>`key-${++key}`;
global.sessionStorage={getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)};
global.document={getElementById:()=>null};
global.axios={post:async(url,body)=>{
 calls.push({url,body:JSON.stringify(body)});
 if(url.endsWith('/preview'))return {data:{can_submit:true,delivery_id:142,customer_id:46,expected_revision:0,reviewed_hash:'a'.repeat(64),segments:body.segments,before:{}}};
 if(mode==='network')throw new Error('connection lost after possible commit');
 if(mode==='conflict')throw {response:{status:409,data:{detail:{message:'stale'}}}};
 return {data:{revision_id:1,revision:1,delivery_id:142,customer_id:46,replayed:true}};
}};
const row={id:142,customer_id:46,customer_name:'高泰',delivery_number:'GT-142',status:'dispatched',return_receipt_status:'confirmed',delivery_date:'2026-09-24',total_quantity:850,
 items:[{id:817,delivered_quantity:50,product_code:'30515'},{id:818,delivered_quantity:800,product_code:'60089'}]};
const vm={user:{id:1},authGeneration:1,canDelivery:true,canAdjustReconciliationPeriod:true,hasPermission:()=>true,
 deliveryOperationState:{action:''},receiptOperationState:{action:''},deliveryInlineEdit:null,deliveryFulfillmentEditor:null,$refs:{},$nextTick:fn=>fn(),
 loadDeliveryListDetail:async()=>true,showToast:(...args)=>toasts.push(args),invalidateDeliveryListDetail:()=>{},loadDeliveries:async()=>{},loadFinance:async()=>{},loadKpi:async()=>{},errorMessage:()=> 'stale'};
"""+"\n".join(bindings)+r"""
const sourceEvidence={source_file:'高泰.xls',source_sha256:'b'.repeat(64),sheet:'对账',rows:[8],confirmation:'客户已确认',quantity_basis_confirmed:true,customer_unit:'只',physical_unit:'只'};
function fill(){const e=vm.deliveryFulfillmentEditor;e.evidence={...sourceEvidence};e.sourceRows='8';e.reason='实际分批确认';
 e.lines[0].segments=[{key:'a',actual_date:'2026-09-22',quantity:50}];e.lines[1].segments=[{key:'b',actual_date:'2026-09-22',quantity:400},{key:'c',actual_date:'2026-10-12',quantity:400}];}
(async()=>{
 assert.equal(await vm.openDeliveryFulfillment(row),true);fill();
 assert.equal(await vm.previewDeliveryFulfillment(),true);
 const e=vm.deliveryFulfillmentEditor;
 assert.equal(await vm.submitDeliveryFulfillment(),false,'explicit review approval required');
 e.approved=true;e.reason='edited without DOM input event';
 assert.equal(await vm.submitDeliveryFulfillment(),false,'signature catches all post-preview edits');assert.equal(e.preview,null);
 assert.equal(await vm.previewDeliveryFulfillment(),true);e.approved=true;
 assert.equal(await vm.submitDeliveryFulfillment(),false);assert.equal(e.uncertain,true);assert.equal(stored.size,1);
 const first=calls.at(-1);assert.equal(vm.closeDeliveryFulfillment(),true);
 assert.equal(await vm.openDeliveryFulfillment(row),true);assert.equal(vm.deliveryFulfillmentEditor.uncertain,true);
 mode='saved';assert.equal(await vm.submitDeliveryFulfillment(),true);
 assert.equal(calls.at(-1).body,first.body,'reopen uses original key and payload');assert.equal(stored.size,0);assert.equal(vm.deliveryFulfillmentEditor,null);
 assert.equal(await vm.openDeliveryFulfillment(row),true);fill();await vm.previewDeliveryFulfillment();vm.deliveryFulfillmentEditor.approved=true;
 mode='conflict';assert.equal(await vm.submitDeliveryFulfillment(),false);
 assert.equal(vm.deliveryFulfillmentEditor.preview,null);assert.equal(vm.deliveryFulfillmentEditor.approved,false);assert.equal(stored.size,0);
 const rejectedCount=calls.length;assert.equal(await vm.submitDeliveryFulfillment(),false);assert.equal(calls.length,rejectedCount,'409 cannot auto-resubmit');
 await vm.previewDeliveryFulfillment();vm.deliveryFulfillmentEditor.approved=true;
 vm.authGeneration++;assert.equal(await vm.submitDeliveryFulfillment(),false);assert.equal(calls.length,rejectedCount+1,'old account context cannot submit');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    _run_node(tmp_path,source)


def test_ui_inline_script_syntax_and_filtered_quantity_preserved(tmp_path):
    import json
    from pathlib import Path
    from tests.test_p1_46_common_box_final_layout import INDEX, _method, _run_node
    params,body=_method("loadDeliveryListDetail")
    source="const html="+json.dumps(INDEX)+r""";
const assert=require('node:assert/strict'),nativeVM=require('node:vm');
for(const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi))if(!/\bsrc=/.test(match[1])&&!/application\/json/.test(match[1]))new nativeVM.Script(match[2]);
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
global.axios={get:async()=>({data:{id:142,items:[{id:818}],actual_fulfillment:{revision:1,matched_physical_quantity:850}}})};
const vm={deliveryDetailState:{},deliveryDetailRequestSequence:0};
"""+f"vm.load=new AsyncFunction({json.dumps(params)},{json.dumps(body)}).bind(vm);"+r"""
(async()=>{const row={id:142,actual_fulfillment:{revision:1,matched_physical_quantity:400}};
assert.equal(await vm.load(row,{force:true}),true);assert.equal(row.actual_fulfillment.matched_physical_quantity,400);
row.actual_fulfillment.revision_id=1;global.axios.get=async()=>({data:{id:142,items:[],actual_fulfillment:{revision_id:2,matched_physical_quantity:850}}});
assert.equal(await vm.load(row,{force:true}),false);assert.equal(row.actual_fulfillment.matched_physical_quantity,400);assert.match(vm.deliveryDetailState[142].error,/版本已变化/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    _run_node(tmp_path,source)

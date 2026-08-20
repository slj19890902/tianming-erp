from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import sessionmaker


def _contract_test_client(tmp_path, *, selected_customer_scope: bool = False):
    from app.api.auth import router as auth_router
    from app.api.contracts import router as contracts_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n041-contract.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(
            customer_number=4101,
            customer_code="N041-CUSTOMER",
            name="N041 合同测试客户",
            credit_limit=Decimal("0"),
            contact_person="合同联系人",
            phone="13800000000",
        )
        other_customer = Customer(
            customer_number=4102,
            customer_code="N041-OTHER",
            name="N041 其他客户",
            credit_limit=Decimal("0"),
        )
        user = User(
            username="n041-sales",
            password_hash=hash_password("N041Pass123!"),
            role="sales",
            real_name="合同业务员",
            must_change_password=False,
            customer_access_mode="selected" if selected_customer_scope else "all",
        )
        db.add_all([customer, other_customer, user])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="N041-BOX-01",
            customer_material_code="N041-BOX-01",
            product_name="N041 合同纸箱",
            box_category="normal",
            is_active=True,
            layer_count=5,
            flute_type="AB",
        )
        other_product = Product(
            customer_id=other_customer.id,
            product_code="N041-OTHER-BOX",
            customer_material_code="N041-OTHER-BOX",
            product_name="其他客户纸箱",
            box_category="normal",
            is_active=True,
            layer_count=5,
            flute_type="AB",
        )
        db.add_all([product, other_product])
        if selected_customer_scope:
            db.add(UserCustomerScope(user_id=user.id, customer_id=customer.id))
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=user.id, permission_code=permission, is_allowed=True
                )
                for permission in (
                    "contracts.view",
                    "contracts.edit",
                    "contracts.convert",
                )
            ]
        )
        db.commit()
        ids = {
            "customer_id": customer.id,
            "other_customer_id": other_customer.id,
            "product_id": product.id,
            "other_product_id": other_product.id,
            "user_id": user.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(contracts_router, prefix="/api/contracts")
    app.include_router(orders_router, prefix="/api/orders")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    assert client.post(
        "/api/auth/login", json={"username": "n041-sales", "password": "N041Pass123!"}
    ).status_code == 200
    return client, factory, ids


def _payload(ids: dict, *, product_id: int | None = None) -> dict:
    return {
        "customer_id": ids["customer_id"],
        "contract_date": "2026-07-19",
        "customer_po": "CONTRACT-PO-001",
        "delivery_date": "2026-07-25",
        "remarks": "合同备注",
        "items": [
            {
                "product_id": product_id if product_id is not None else ids["product_id"],
                "product_code": "N041-BOX-01",
                "product_name": "N041 合同纸箱",
                "specification": "300×200×150",
                "box_type": "A1",
                "material": "A416D",
                "flute_type": "AB",
                "quantity": 10,
                "unit_price": 2.5,
            }
        ],
    }


def test_contract_draft_confirm_convert_once_and_audit(tmp_path):
    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract
    from app.models.order import Order

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids))
    assert created.status_code == 201
    contract = created.json()
    assert contract["contract_no"] == "CT-20260719-001"
    assert contract["items"][0]["box_type"] == "A1"
    assert contract["items"][0]["material"] == "A416D"
    assert contract["total_amount"] == "25.00"

    edited = _payload(ids)
    edited["expected_version"] = contract["version"]
    edited["items"][0]["quantity"] = 12
    updated = client.put(f"/api/contracts/{contract['id']}", json=edited)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["total_amount"] == "30.00"

    confirmed = client.post(
        f"/api/contracts/{contract['id']}/confirm", json={"expected_version": 2}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["confirmed_at"]
    assert client.put(f"/api/contracts/{contract['id']}", json=edited).status_code == 409
    assert client.request(
        "DELETE",
        f"/api/contracts/{contract['id']}",
        json={"confirm_text": "我确认删除合同", "expected_version": confirmed.json()["version"]},
    ).status_code == 409

    first = client.post(
        f"/api/contracts/{contract['id']}/convert-order",
        json={"expected_version": 3, "idempotency_key": "n041-key-one"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "converted"
    assert first.json()["already_converted"] is False
    assert first.json()["source_contract_id"] == contract["id"]
    assert first.json()["converted_order_number"]
    order_id = first.json()["order_id"]
    retry = client.post(
        f"/api/contracts/{contract['id']}/convert-order",
        json={"expected_version": 1, "idempotency_key": "other-key-is-safe-after-convert"},
    )
    assert retry.status_code == 200
    assert retry.json()["already_converted"] is True
    assert retry.json()["source_contract_id"] == contract["id"]
    assert retry.json()["order_id"] == order_id

    refreshed = client.get(f"/api/contracts/{contract['id']}")
    assert refreshed.status_code == 200
    assert refreshed.json()["converted_order_number"] == first.json()["converted_order_number"]
    listed = client.get("/api/contracts", params={"customer_id": ids["customer_id"]})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["converted_order_number"] == first.json()["converted_order_number"]
    assert refreshed.json()["confirmed_at"].endswith("Z")
    assert refreshed.json()["converted_at"].endswith("Z")

    printed = client.get(f"/api/contracts/{contract['id']}/print")
    assert printed.status_code == 200
    assert printed.json()["contract"]["items"][0]["quantity"] == 12

    with factory() as db:
        assert db.get(Order, order_id).source_contract_id == contract["id"]
        assert db.get(CustomerContract, contract["id"]).converted_order_id == order_id
        actions = db.query(OperationLog.action).filter(
            OperationLog.entity_type == "customer_contract"
        ).all()
        assert {row[0] for row in actions} >= {"CREATE", "UPDATE", "CONFIRM", "CONVERT"}


def test_contract_item_replacement_flushes_old_line_numbers_first(tmp_path):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.api.contracts import ContractItemPayload, _replace_items
    from app.models.customer_contract import CustomerContract
    from app.models.product import Product

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids))
    assert created.status_code == 201

    with factory() as db:
        contract = db.scalar(
            select(CustomerContract)
            .options(selectinload(CustomerContract.items))
            .where(CustomerContract.id == created.json()["id"])
        )
        assert contract is not None
        # Keep the product in the identity map so db.get() cannot incidentally
        # flush the orphaned old item for us. This reproduces the UAT failure.
        assert db.get(Product, ids["product_id"]) is not None
        old_item = contract.items[0]
        replacement = ContractItemPayload(
            product_id=old_item.product_id,
            product_code=old_item.product_code,
            product_name=old_item.product_name,
            specification=old_item.specification,
            box_style=old_item.box_style,
            length_mm=old_item.length_mm,
            width_mm=old_item.width_mm,
            height_mm=old_item.height_mm,
            material_code=old_item.material_code,
            flute_type=old_item.flute_type,
            quantity=4,
            unit_price=old_item.unit_price,
            remarks=old_item.remarks,
        )
        _replace_items(db, contract, [replacement])
        db.flush()
        assert contract.items[0].quantity == 4
        db.rollback()


def test_contract_defaults_po_and_delivery_to_contract_business_defaults(tmp_path):
    client, _factory, ids = _contract_test_client(tmp_path)
    payload = _payload(ids)
    payload.pop("customer_po")
    payload.pop("delivery_date")

    created = client.post("/api/contracts", json=payload)
    assert created.status_code == 201, created.text
    contract = created.json()
    assert contract["contract_no"] == "CT-20260719-001"
    assert contract["customer_po"] == contract["contract_no"]
    # 2026-07-19 is Sunday; seven Monday-Friday working days ends on Tuesday.
    assert contract["delivery_date"] == "2026-07-28"

    update_payload = _payload(ids)
    update_payload["expected_version"] = contract["version"]
    update_payload["contract_date"] = "2026-07-24"
    update_payload["customer_po"] = ""
    update_payload["delivery_date"] = None
    updated = client.put(f"/api/contracts/{contract['id']}", json=update_payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()["customer_po"] == contract["contract_no"]
    assert updated.json()["delivery_date"] == "2026-08-04"

    manual_payload = _payload(ids)
    manual_payload["expected_version"] = updated.json()["version"]
    manual_payload["customer_po"] = "CUSTOMER-PO-MANUAL"
    manual_payload["delivery_date"] = "2026-08-10"
    manual = client.put(f"/api/contracts/{contract['id']}", json=manual_payload)
    assert manual.status_code == 200, manual.text
    assert manual.json()["customer_po"] == "CUSTOMER-PO-MANUAL"
    assert manual.json()["delivery_date"] == "2026-08-10"


def test_contract_rejects_other_customer_product_and_requires_delete_text(tmp_path):
    client, _factory, ids = _contract_test_client(tmp_path)
    wrong = client.post("/api/contracts", json=_payload(ids, product_id=ids["other_product_id"]))
    assert wrong.status_code == 400

    created = client.post("/api/contracts", json=_payload(ids))
    assert created.status_code == 201
    contract_id = created.json()["id"]
    assert client.request(
        "DELETE",
        f"/api/contracts/{contract_id}",
        json={"confirm_text": "确认", "expected_version": created.json()["version"]},
    ).status_code == 400
    deleted = client.request(
        "DELETE",
        f"/api/contracts/{contract_id}",
        json={"confirm_text": "我确认删除合同", "expected_version": created.json()["version"]},
    )
    assert deleted.status_code == 200


def test_contract_backed_order_response_exposes_source_and_edits_do_not_rewrite_contract(
    tmp_path,
):
    client, _factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids))
    assert created.status_code == 201
    contract = created.json()
    confirmed = client.post(
        f"/api/contracts/{contract['id']}/confirm",
        json={"expected_version": contract["version"]},
    )
    assert confirmed.status_code == 200
    confirmed_snapshot = confirmed.json()
    converted = client.post(
        f"/api/contracts/{contract['id']}/convert-order",
        json={
            "expected_version": confirmed.json()["version"],
            "idempotency_key": "n041-order-provenance",
        },
    )
    assert converted.status_code == 200, converted.text
    order_id = converted.json()["order_id"]
    assert converted.json()["source_contract_id"] == contract["id"]

    before_edit = client.get(f"/api/contracts/{contract['id']}").json()
    order = client.get(f"/api/orders/{order_id}")
    assert order.status_code == 200
    assert order.json()["source_contract_id"] == contract["id"]
    for field in (
        "customer_id",
        "customer_name",
        "contract_date",
        "customer_po",
        "delivery_date",
        "remarks",
        "total_amount",
        "items",
    ):
        assert before_edit[field] == confirmed_snapshot[field]

    edited_order = client.put(
        f"/api/orders/{order_id}",
        json={
            "customer_po": "OPERATIONS-PO-CHANGED",
            "delivery_date": "2026-08-18",
            "remark": "订单正常业务调整",
        },
    )
    assert edited_order.status_code == 200, edited_order.text
    assert edited_order.json()["source_contract_id"] == contract["id"]
    assert edited_order.json()["customer_po"] == "OPERATIONS-PO-CHANGED"
    order_item = order.json()["items"][0]
    edited_item = client.put(
        f"/api/orders/items/{order_item['id']}",
        json={
            "quantity": 13,
            "unit_price": 3,
            "product_code": "OPERATIONS-BOX-CHANGED",
            "product_name": "订单正常业务修改",
            "material": "ORDER-MATERIAL-CHANGED",
            "specification": "320×220×160",
        },
    )
    assert edited_item.status_code == 200, edited_item.text
    assert edited_item.json()["quantity"] == 13

    after_edit = client.get(f"/api/contracts/{contract['id']}").json()
    for field in (
        "customer_po",
        "delivery_date",
        "remarks",
        "total_amount",
        "version",
        "status",
        "items",
    ):
        assert after_edit[field] == before_edit[field]


def test_confirmed_contract_print_and_conversion_keep_contract_snapshot_after_master_changes(
    tmp_path,
):
    from app.models.customer import Customer
    from app.models.product import Product

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids))
    assert created.status_code == 201
    confirmed = client.post(
        f"/api/contracts/{created.json()['id']}/confirm",
        json={"expected_version": created.json()["version"]},
    )
    assert confirmed.status_code == 200
    confirmed_snapshot = confirmed.json()
    confirmed_item = confirmed_snapshot["items"][0]

    with factory() as db:
        customer = db.get(Customer, ids["customer_id"])
        product = db.get(Product, ids["product_id"])
        assert customer is not None and product is not None
        customer.name = "确认后修改的客户名称"
        customer.contact_person = "确认后修改的联系人"
        customer.phone = "13999999999"
        customer.address = "确认后修改的地址"
        customer.invoice_title = "确认后修改的开票抬头"
        customer.tax_no = "MASTER-TAX-CHANGED"
        customer.bank_account = "MASTER-BANK-CHANGED"
        customer.credit_terms = "确认后修改的账期"
        product.product_code = "MASTER-BOX-CHANGED"
        product.customer_material_code = "MASTER-BOX-CHANGED"
        product.product_name = "确认后修改的常用箱名称"
        product.default_material_code = "MASTER-MATERIAL-CHANGED"
        product.flute_type = "B"
        db.commit()

    printed = client.get(f"/api/contracts/{confirmed_snapshot['id']}/print")
    assert printed.status_code == 200
    printed_contract = printed.json()["contract"]
    for field in (
        "customer_name",
        "customer_contact",
        "customer_phone",
        "customer_address",
        "invoice_title",
        "tax_no",
        "bank_account",
        "payment_terms",
        "items",
    ):
        assert printed_contract[field] == confirmed_snapshot[field]

    converted = client.post(
        f"/api/contracts/{confirmed_snapshot['id']}/convert-order",
        json={
            "expected_version": confirmed_snapshot["version"],
            "idempotency_key": "n041-master-change-snapshot",
        },
    )
    assert converted.status_code == 200, converted.text
    converted_body = converted.json()
    assert converted_body["customer_name"] == confirmed_snapshot["customer_name"]
    assert converted_body["source_contract_id"] == confirmed_snapshot["id"]

    order = client.get(f"/api/orders/{converted_body['order_id']}")
    assert order.status_code == 200
    order_body = order.json()
    assert order_body["customer_po"] == confirmed_snapshot["customer_po"]
    assert order_body["order_date"] == confirmed_snapshot["contract_date"]
    assert order_body["delivery_date"] == confirmed_snapshot["delivery_date"]
    assert order_body["remark"] == confirmed_snapshot["remarks"]
    assert Decimal(str(order_body["total_amount"])) == Decimal(
        str(confirmed_snapshot["total_amount"])
    )
    order_item = order_body["items"][0]
    assert order_item["snapshot_product_code"] == confirmed_item["product_code"]
    assert order_item["snapshot_product_name"] == confirmed_item["product_name"]
    assert order_item["snapshot_spec"].removesuffix("mm") == confirmed_item[
        "specification"
    ].removesuffix("mm")
    assert order_item["snapshot_material"] == confirmed_item["material_code"]
    assert order_item["flute_type"] == confirmed_item["flute_type"]
    assert order_item["quantity"] == confirmed_item["quantity"]
    assert Decimal(str(order_item["unit_price"])) == Decimal(
        str(confirmed_item["unit_price"])
    )


@pytest.mark.parametrize("_attempt", range(3))
def test_concurrent_contract_updates_use_database_cas_without_lost_update(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _attempt: int,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    import app.api.contracts as contracts_api
    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids)).json()
    barrier = Barrier(2)
    original_claim = contracts_api._claim_draft_version

    def synchronized_claim(db, *, contract, expected_version) -> None:
        barrier.wait(timeout=10)
        original_claim(
            db,
            contract=contract,
            expected_version=expected_version,
        )

    monkeypatch.setattr(contracts_api, "_claim_draft_version", synchronized_claim)
    payloads = {}
    for name, quantity in (("first", 11), ("second", 17)):
        payload = _payload(ids)
        payload["expected_version"] = created["version"]
        payload["remarks"] = f"{name}-wins-only"
        payload["items"][0]["quantity"] = quantity
        payloads[name] = payload

    with TestClient(client.app) as second:
        second.cookies.update(client.cookies)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                name: executor.submit(
                    request_client.put,
                    f"/api/contracts/{created['id']}",
                    json=payload,
                )
                for name, request_client, payload in (
                    ("first", client, payloads["first"]),
                    ("second", second, payloads["second"]),
                )
            }
            responses = {name: future.result(timeout=20) for name, future in futures.items()}

    assert sorted(response.status_code for response in responses.values()) == [200, 409]
    winner = next(name for name, response in responses.items() if response.status_code == 200)
    expected_quantity = payloads[winner]["items"][0]["quantity"]
    with factory() as db:
        stored = db.scalar(
            select(CustomerContract)
            .options(selectinload(CustomerContract.items))
            .where(CustomerContract.id == created["id"])
        )
        update_logs = db.scalars(
            select(OperationLog).where(
                OperationLog.entity_type == "customer_contract",
                OperationLog.entity_id == created["id"],
                OperationLog.action == "UPDATE",
            )
        ).all()
    assert stored is not None
    assert stored.version == 2
    assert stored.remarks == f"{winner}-wins-only"
    assert stored.items[0].quantity == expected_quantity
    assert stored.total_amount == Decimal(expected_quantity) * Decimal("2.50")
    assert len(update_logs) == 1


@pytest.mark.parametrize("_attempt", range(3))
def test_concurrent_update_and_confirm_have_one_cas_winner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _attempt: int,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    import app.api.contracts as contracts_api
    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids)).json()
    barrier = Barrier(2)
    original_claim = contracts_api._claim_draft_version

    def synchronized_claim(db, *, contract, expected_version) -> None:
        barrier.wait(timeout=10)
        original_claim(
            db,
            contract=contract,
            expected_version=expected_version,
        )

    monkeypatch.setattr(contracts_api, "_claim_draft_version", synchronized_claim)
    update_payload = _payload(ids)
    update_payload["expected_version"] = created["version"]
    update_payload["remarks"] = "CAS 更新胜出"
    update_payload["items"][0]["quantity"] = 19

    with TestClient(client.app) as second:
        second.cookies.update(client.cookies)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                "update": executor.submit(
                    client.put,
                    f"/api/contracts/{created['id']}",
                    json=update_payload,
                ),
                "confirm": executor.submit(
                    second.post,
                    f"/api/contracts/{created['id']}/confirm",
                    json={"expected_version": created["version"]},
                ),
            }
            responses = {name: future.result(timeout=20) for name, future in futures.items()}

    assert sorted(response.status_code for response in responses.values()) == [200, 409]
    winner = next(name for name, response in responses.items() if response.status_code == 200)
    with factory() as db:
        stored = db.scalar(
            select(CustomerContract)
            .options(selectinload(CustomerContract.items))
            .where(CustomerContract.id == created["id"])
        )
        transition_logs = db.scalars(
            select(OperationLog).where(
                OperationLog.entity_type == "customer_contract",
                OperationLog.entity_id == created["id"],
                OperationLog.action.in_(("UPDATE", "CONFIRM")),
            )
        ).all()
    assert stored is not None
    assert stored.version == 2
    assert len(transition_logs) == 1
    if winner == "update":
        assert stored.status == "draft"
        assert stored.remarks == "CAS 更新胜出"
        assert stored.items[0].quantity == 19
        assert stored.confirmed_at is None
    else:
        assert stored.status == "confirmed"
        assert stored.remarks == "合同备注"
        assert stored.items[0].quantity == 10
        assert stored.confirmed_at is not None


def test_delete_rejects_stale_version_after_update_and_confirm(tmp_path):
    from sqlalchemy import select

    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids)).json()
    stale_version = created["version"]

    update_payload = _payload(ids)
    update_payload["expected_version"] = stale_version
    update_payload["remarks"] = "删除请求必须失效"
    updated = client.put(f"/api/contracts/{created['id']}", json=update_payload)
    assert updated.status_code == 200, updated.text

    stale_delete = client.request(
        "DELETE",
        f"/api/contracts/{created['id']}",
        json={"confirm_text": "我确认删除合同", "expected_version": stale_version},
    )
    assert stale_delete.status_code == 409

    confirmed = client.post(
        f"/api/contracts/{created['id']}/confirm",
        json={"expected_version": updated.json()["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_delete = client.request(
        "DELETE",
        f"/api/contracts/{created['id']}",
        json={
            "confirm_text": "我确认删除合同",
            "expected_version": confirmed.json()["version"],
        },
    )
    assert confirmed_delete.status_code == 409

    with factory() as db:
        stored = db.get(CustomerContract, created["id"])
        delete_logs = db.scalars(
            select(OperationLog).where(
                OperationLog.entity_type == "customer_contract",
                OperationLog.entity_id == created["id"],
                OperationLog.action == "DELETE",
            )
        ).all()
    assert stored is not None
    assert stored.status == "confirmed"
    assert stored.version == 3
    assert delete_logs == []


@pytest.mark.parametrize("mutation", ("update", "confirm"))
@pytest.mark.parametrize("_attempt", range(2))
def test_concurrent_delete_and_mutation_have_one_database_cas_winner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    _attempt: int,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import select

    import app.api.contracts as contracts_api
    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids)).json()
    barrier = Barrier(2)
    original_claim = contracts_api._claim_draft_version
    original_delete = contracts_api._delete_draft_version

    def synchronized_claim(db, *, contract, expected_version) -> None:
        barrier.wait(timeout=10)
        original_claim(db, contract=contract, expected_version=expected_version)

    def synchronized_delete(db, *, contract_id, expected_version) -> None:
        barrier.wait(timeout=10)
        original_delete(
            db,
            contract_id=contract_id,
            expected_version=expected_version,
        )

    monkeypatch.setattr(contracts_api, "_claim_draft_version", synchronized_claim)
    monkeypatch.setattr(contracts_api, "_delete_draft_version", synchronized_delete)
    update_payload = _payload(ids)
    update_payload["expected_version"] = created["version"]
    update_payload["remarks"] = "并发更新胜出"

    with TestClient(client.app) as second:
        second.cookies.update(client.cookies)
        with ThreadPoolExecutor(max_workers=2) as executor:
            mutation_future = (
                executor.submit(
                    client.put,
                    f"/api/contracts/{created['id']}",
                    json=update_payload,
                )
                if mutation == "update"
                else executor.submit(
                    client.post,
                    f"/api/contracts/{created['id']}/confirm",
                    json={"expected_version": created["version"]},
                )
            )
            delete_future = executor.submit(
                second.request,
                "DELETE",
                f"/api/contracts/{created['id']}",
                json={
                    "confirm_text": "我确认删除合同",
                    "expected_version": created["version"],
                },
            )
            responses = {
                mutation: mutation_future.result(timeout=20),
                "delete": delete_future.result(timeout=20),
            }

    assert sorted(response.status_code for response in responses.values()) == [200, 409]
    winner = next(name for name, response in responses.items() if response.status_code == 200)
    with factory() as db:
        stored = db.get(CustomerContract, created["id"])
        transition_logs = db.scalars(
            select(OperationLog).where(
                OperationLog.entity_type == "customer_contract",
                OperationLog.entity_id == created["id"],
                OperationLog.action.in_(("UPDATE", "CONFIRM", "DELETE")),
            )
        ).all()
    assert len(transition_logs) == 1
    if winner == "delete":
        assert stored is None
        assert transition_logs[0].action == "DELETE"
    else:
        assert stored is not None
        assert stored.version == 2
        assert transition_logs[0].action == winner.upper()
        if mutation == "update":
            assert stored.status == "draft"
            assert stored.remarks == "并发更新胜出"
        else:
            assert stored.status == "confirmed"


@pytest.mark.parametrize("_attempt", range(3))
def test_concurrent_contract_conversion_returns_one_idempotent_order(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _attempt: int,
):
    from contextlib import ExitStack
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import func, select

    import app.api.orders as orders_api
    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract
    from app.models.order import Order, OrderDailySequence

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids)).json()
    confirmed = client.post(
        f"/api/contracts/{created['id']}/confirm",
        json={"expected_version": created["version"]},
    ).json()
    concurrency = 10
    barrier = Barrier(concurrency)
    original_reserve = orders_api.reserve_next_order_number

    def synchronized_reserve(db, order_date):
        # Every request has passed duplicate-order preflight before any can
        # reserve/insert, forcing all losers through source_contract_id UNIQUE.
        barrier.wait(timeout=30)
        return original_reserve(db, order_date)

    monkeypatch.setattr(orders_api, "reserve_next_order_number", synchronized_reserve)
    with ExitStack() as stack:
        request_clients = [
            stack.enter_context(TestClient(client.app)) for _ in range(concurrency)
        ]
        for request_client in request_clients:
            login = request_client.post(
                "/api/auth/login",
                json={"username": "n041-sales", "password": "N041Pass123!"},
            )
            assert login.status_code == 200
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(
                    request_client.post,
                    f"/api/contracts/{created['id']}/convert-order",
                    json={
                        "expected_version": confirmed["version"],
                        "idempotency_key": f"n041-concurrent-{index}",
                    },
                )
                for index, request_client in enumerate(request_clients, start=1)
            ]
            responses = [future.result(timeout=60) for future in futures]

    assert [response.status_code for response in responses] == [200] * concurrency
    bodies = [response.json() for response in responses]
    assert sum(body["already_converted"] is False for body in bodies) == 1
    assert sum(body["already_converted"] is True for body in bodies) == concurrency - 1
    order_ids = {body["order_id"] for body in bodies}
    assert len(order_ids) == 1
    assert {body["source_contract_id"] for body in bodies} == {created["id"]}

    with factory() as db:
        order_count = db.scalar(
            select(func.count(Order.id)).where(Order.source_contract_id == created["id"])
        )
        sequence = db.get(OrderDailySequence, date(2026, 7, 19))
        contract = db.get(CustomerContract, created["id"])
        convert_logs = db.scalars(
            select(OperationLog).where(
                OperationLog.entity_type == "customer_contract",
                OperationLog.entity_id == created["id"],
                OperationLog.action == "CONVERT",
            )
        ).all()
    assert order_count == 1
    assert sequence is not None and sequence.last_value == 1
    assert contract is not None and contract.status == "converted"
    assert contract.converted_order_id == order_ids.pop()
    assert len(convert_logs) == 1


def test_contract_endpoints_fail_closed_outside_selected_customer_scope(tmp_path):
    from sqlalchemy import func, select

    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract, CustomerContractItem
    from app.models.order import Order

    client, factory, ids = _contract_test_client(
        tmp_path,
        selected_customer_scope=True,
    )
    with factory() as db:
        restricted = CustomerContract(
            contract_no="CT-20260719-999",
            customer_id=ids["other_customer_id"],
            customer_name="N041 其他客户",
            contract_date=date(2026, 7, 19),
            customer_po="RESTRICTED-PO",
            delivery_date=date(2026, 7, 25),
            total_amount=Decimal("25.00"),
            status="draft",
            version=1,
            created_by=ids["user_id"],
        )
        restricted.items.append(
            CustomerContractItem(
                line_no=1,
                product_id=ids["other_product_id"],
                product_code="N041-OTHER-BOX",
                product_name="其他客户纸箱",
                quantity=10,
                unit_price=Decimal("2.5000"),
                subtotal=Decimal("25.00"),
            )
        )
        db.add(restricted)
        db.commit()
        restricted_id = restricted.id

    assert client.get(
        "/api/contracts", params={"customer_id": ids["customer_id"]}
    ).status_code == 200
    assert client.get(
        "/api/contracts", params={"customer_id": ids["other_customer_id"]}
    ).status_code == 403
    cross_create = _payload(ids, product_id=ids["other_product_id"])
    cross_create["customer_id"] = ids["other_customer_id"]
    assert client.post("/api/contracts", json=cross_create).status_code == 403
    assert client.get(f"/api/contracts/{restricted_id}").status_code == 403
    assert client.get(f"/api/contracts/{restricted_id}/print").status_code == 403
    cross_update = _payload(ids, product_id=ids["other_product_id"])
    cross_update["expected_version"] = 1
    assert client.put(
        f"/api/contracts/{restricted_id}", json=cross_update
    ).status_code == 403
    assert client.request(
        "DELETE",
        f"/api/contracts/{restricted_id}",
        json={"confirm_text": "我确认删除合同", "expected_version": 1},
    ).status_code == 403
    assert client.post(
        f"/api/contracts/{restricted_id}/confirm",
        json={"expected_version": 1},
    ).status_code == 403
    assert client.post(
        f"/api/contracts/{restricted_id}/convert-order",
        json={"expected_version": 1, "idempotency_key": "scope-denied"},
    ).status_code == 403

    with factory() as db:
        stored = db.get(CustomerContract, restricted_id)
        order_count = db.scalar(select(func.count(Order.id)))
        log_count = db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.entity_type == "customer_contract",
                OperationLog.entity_id == restricted_id,
            )
        )
    assert stored is not None and stored.status == "draft" and stored.version == 1
    assert order_count == 0
    assert log_count == 0

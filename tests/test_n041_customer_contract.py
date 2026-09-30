from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


def _contract_test_client(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.contracts import router as contracts_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
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
            "product_id": product.id,
            "other_product_id": other_product.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(contracts_router, prefix="/api/contracts")

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
        json={"confirm_text": "我确认删除合同", "expected_version": 3},
    ).status_code == 409

    first = client.post(
        f"/api/contracts/{contract['id']}/convert-order",
        json={"expected_version": 3, "idempotency_key": "n041-key-one"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "converted"
    assert first.json()["already_converted"] is False
    assert first.json()["converted_order_number"]
    order_id = first.json()["order_id"]
    retry = client.post(
        f"/api/contracts/{contract['id']}/convert-order",
        json={"expected_version": 1, "idempotency_key": "other-key-is-safe-after-convert"},
    )
    assert retry.status_code == 200
    assert retry.json()["already_converted"] is True
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
    from app.models.customer_contract import CustomerContract, CustomerContractItem

    client, factory, ids = _contract_test_client(tmp_path)
    wrong = client.post("/api/contracts", json=_payload(ids, product_id=ids["other_product_id"]))
    assert wrong.status_code == 400

    created = client.post("/api/contracts", json=_payload(ids))
    assert created.status_code == 201
    contract_id = created.json()["id"]
    assert client.request(
        "DELETE",
        f"/api/contracts/{contract_id}",
        json={"confirm_text": "我确认删除合同"},
    ).status_code == 422
    assert client.request(
        "DELETE",
        f"/api/contracts/{contract_id}",
        json={"confirm_text": "确认", "expected_version": created.json()["version"]},
    ).status_code == 400
    deleted = client.request(
        "DELETE",
        f"/api/contracts/{contract_id}",
        json={
            "confirm_text": "我确认删除合同",
            "expected_version": created.json()["version"],
        },
    )
    assert deleted.status_code == 200
    with factory() as db:
        assert db.get(CustomerContract, contract_id) is None
        assert db.query(CustomerContractItem).filter_by(contract_id=contract_id).count() == 0


def test_contract_delete_rejects_stale_version_and_preserves_latest_draft(tmp_path):
    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract, CustomerContractItem

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids))
    assert created.status_code == 201
    stale = created.json()

    updated_payload = _payload(ids)
    updated_payload["expected_version"] = stale["version"]
    updated_payload["remarks"] = "另一操作员已保存的新内容"
    updated_payload["items"][0]["quantity"] = 12
    updated = client.put(f"/api/contracts/{stale['id']}", json=updated_payload)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    rejected = client.request(
        "DELETE",
        f"/api/contracts/{stale['id']}",
        json={
            "confirm_text": "我确认删除合同",
            "expected_version": stale["version"],
        },
    )
    assert rejected.status_code == 409
    assert "刷新" in rejected.json()["detail"]

    with factory() as db:
        contract = db.get(CustomerContract, stale["id"])
        assert contract is not None
        assert contract.version == 2
        assert contract.remarks == "另一操作员已保存的新内容"
        items = db.query(CustomerContractItem).filter_by(contract_id=stale["id"]).all()
        assert len(items) == 1
        assert items[0].quantity == 12
        delete_audits = db.query(OperationLog).filter(
            OperationLog.entity_type == "customer_contract",
            OperationLog.entity_id == stale["id"],
            OperationLog.action == "DELETE",
        ).all()
        assert delete_audits == []


def test_contract_delete_cas_rejects_change_after_initial_read(tmp_path, monkeypatch):
    from sqlalchemy import update

    from app.api import contracts as contracts_api
    from app.models.audit import OperationLog
    from app.models.customer_contract import CustomerContract

    client, factory, ids = _contract_test_client(tmp_path)
    created = client.post("/api/contracts", json=_payload(ids))
    assert created.status_code == 201
    draft = created.json()

    original_require_customer_access = contracts_api.require_customer_access
    injected = False

    def update_after_initial_read(customer_id, current_user, db):
        nonlocal injected
        result = original_require_customer_access(
            customer_id, current_user=current_user, db=db
        )
        if not injected:
            injected = True
            with factory() as concurrent_db:
                concurrent_db.execute(
                    update(CustomerContract)
                    .where(CustomerContract.id == draft["id"])
                    .values(version=2, remarks="并发保存发生在删除初读之后")
                )
                concurrent_db.commit()
        return result

    monkeypatch.setattr(
        contracts_api, "require_customer_access", update_after_initial_read
    )
    rejected = client.request(
        "DELETE",
        f"/api/contracts/{draft['id']}",
        json={
            "confirm_text": "我确认删除合同",
            "expected_version": draft["version"],
        },
    )
    assert rejected.status_code == 409
    assert "刷新" in rejected.json()["detail"]

    with factory() as db:
        contract = db.get(CustomerContract, draft["id"])
        assert contract is not None
        assert contract.version == 2
        assert contract.remarks == "并发保存发生在删除初读之后"
        assert db.query(OperationLog).filter(
            OperationLog.entity_type == "customer_contract",
            OperationLog.entity_id == draft["id"],
            OperationLog.action == "DELETE",
        ).count() == 0

from __future__ import annotations

from sqlalchemy import select

from tests.test_phase11_requisition import _login, requisition_app


def _customer_payload(customer, *, status: str) -> dict:
    return {
        "customer_number": customer.customer_number,
        "customer_code": customer.customer_code,
        "name": customer.name,
        "chinese_short_name": customer.chinese_short_name,
        "payment_term_days": customer.payment_term_days,
        "statement_cycle_start_day": customer.statement_cycle_start_day,
        "credit_limit": str(customer.credit_limit),
        "delivery_method": customer.delivery_method,
        "contact_person": customer.contact_person,
        "phone": customer.phone,
        "address": customer.address,
        "default_tax_rate": str(customer.default_tax_rate),
        "invoice_title": customer.invoice_title,
        "tax_no": customer.tax_no,
        "bank_account": customer.bank_account,
        "remark": customer.remark,
        "status": status,
        "expected_version": customer.version,
    }


def _customer_api(requisition_app):
    from app.api.customers import router as customers_router

    app, session_factory = requisition_app
    app.include_router(customers_router, prefix="/api/master/customers")
    return app, session_factory


def _customer(session_factory):
    from app.models.customer import Customer

    with session_factory() as db:
        return db.scalar(select(Customer).where(Customer.customer_code == "SME"))


def test_full_update_cannot_deactivate_customer_with_open_order(requisition_app) -> None:
    from fastapi.testclient import TestClient
    from app.models.customer import Customer

    app, session_factory = _customer_api(requisition_app)
    customer = _customer(session_factory)
    assert customer is not None
    payload = _customer_payload(customer, status="inactive")

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put(f"/api/master/customers/{customer.id}", json=payload)
        preview = client.post(
            f"/api/master/customers/{customer.id}/update-preview", json=payload
        )
        status_response = client.put(
            f"/api/master/customers/{customer.id}/status",
            json={"is_active": False, "expected_version": customer.version},
        )

    assert response.status_code == 400
    assert preview.status_code == 400
    assert status_response.status_code == 400
    with session_factory() as db:
        persisted = db.get(Customer, customer.id)
        assert persisted is not None
        assert persisted.is_active is True
        assert persisted.status == "active"
        assert persisted.version == customer.version


def test_full_update_status_transition_requires_deactivation_permission(requisition_app) -> None:
    from fastapi.testclient import TestClient
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order
    from app.models.user import User

    app, session_factory = _customer_api(requisition_app)
    customer = _customer(session_factory)
    assert customer is not None
    with session_factory() as db:
        sales = db.scalar(select(User).where(User.username == "sales"))
        order = db.scalar(select(Order).where(Order.customer_id == customer.id))
        assert sales is not None and order is not None
        sales.customer_access_mode = "selected"
        db.add(UserCustomerScope(user_id=sales.id, customer_id=customer.id, assigned_by=sales.id))
        order.status = "cancelled"
        db.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.put(
            f"/api/master/customers/{customer.id}",
            json=_customer_payload(customer, status="inactive"),
        )

    assert response.status_code == 403
    with session_factory() as db:
        persisted = db.get(Customer, customer.id)
        assert persisted is not None
        assert persisted.is_active is True
        assert persisted.status == "active"
        assert persisted.version == customer.version


def test_full_update_reactivation_requires_deactivation_permission(requisition_app) -> None:
    from fastapi.testclient import TestClient
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order
    from app.models.user import User

    app, session_factory = _customer_api(requisition_app)
    customer = _customer(session_factory)
    assert customer is not None
    with session_factory() as db:
        sales = db.scalar(select(User).where(User.username == "sales"))
        order = db.scalar(select(Order).where(Order.customer_id == customer.id))
        persisted = db.get(Customer, customer.id)
        assert sales is not None and order is not None and persisted is not None
        sales.customer_access_mode = "selected"
        db.add(UserCustomerScope(user_id=sales.id, customer_id=customer.id, assigned_by=sales.id))
        order.status = "cancelled"
        persisted.is_active = False
        persisted.status = "inactive"
        db.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.put(
            f"/api/master/customers/{customer.id}",
            json=_customer_payload(customer, status="active"),
        )

    assert response.status_code == 403
    with session_factory() as db:
        persisted = db.get(Customer, customer.id)
        assert persisted is not None
        assert persisted.is_active is False
        assert persisted.status == "inactive"


def test_regular_full_update_keeps_edit_permission_without_status_transition(
    requisition_app,
) -> None:
    from fastapi.testclient import TestClient
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.user import User

    app, session_factory = _customer_api(requisition_app)
    customer = _customer(session_factory)
    assert customer is not None
    with session_factory() as db:
        sales = db.scalar(select(User).where(User.username == "sales"))
        assert sales is not None
        sales.customer_access_mode = "selected"
        db.add(UserCustomerScope(user_id=sales.id, customer_id=customer.id, assigned_by=sales.id))
        db.commit()
    payload = _customer_payload(customer, status="active")
    payload["remark"] = "仅编辑资料"

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.put(f"/api/master/customers/{customer.id}", json=payload)

    assert response.status_code == 200
    with session_factory() as db:
        persisted = db.get(Customer, customer.id)
        assert persisted is not None
        assert persisted.is_active is True
        assert persisted.status == "active"
        assert persisted.remark == "仅编辑资料"


def test_admin_full_update_keeps_status_transition_version_and_audit(requisition_app) -> None:
    from fastapi.testclient import TestClient
    from app.models.customer import Customer
    from app.models.order import Order
    from app.models.master_data_object_version import MasterDataObjectVersion

    app, session_factory = _customer_api(requisition_app)
    customer = _customer(session_factory)
    assert customer is not None
    with session_factory() as db:
        order = db.scalar(select(Order).where(Order.customer_id == customer.id))
        assert order is not None
        order.status = "cancelled"
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put(
            f"/api/master/customers/{customer.id}",
            json=_customer_payload(customer, status="inactive"),
        )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert response.json()["status"] == "inactive"
    with session_factory() as db:
        persisted = db.get(Customer, customer.id)
        revision = db.scalar(
            select(MasterDataObjectVersion)
            .where(
                MasterDataObjectVersion.object_type == "customer",
                MasterDataObjectVersion.object_id == customer.id,
            )
            .order_by(MasterDataObjectVersion.id.desc())
        )
        assert persisted is not None
        assert persisted.version == customer.version + 1
        assert revision is not None and revision.action == "update"


def test_customer_status_input_is_shared_and_limited_to_active_or_inactive(
    requisition_app,
) -> None:
    from fastapi.testclient import TestClient
    from app.models.customer import Customer

    app, session_factory = _customer_api(requisition_app)
    customer = _customer(session_factory)
    assert customer is not None
    invalid_update = _customer_payload(customer, status="suspended")
    invalid_create = {
        **_customer_payload(customer, status="suspended"),
        "customer_number": 2,
        "customer_code": "OPT10-03-INVALID",
        "name": "OPT10-03 invalid status",
    }

    with TestClient(app) as client:
        _login(client, "admin")
        update = client.put(f"/api/master/customers/{customer.id}", json=invalid_update)
        preview = client.post(
            f"/api/master/customers/{customer.id}/update-preview", json=invalid_update
        )
        create = client.post("/api/master/customers", json=invalid_create)

    assert update.status_code == 422
    assert preview.status_code == 422
    assert create.status_code == 422
    with session_factory() as db:
        persisted = db.get(Customer, customer.id)
        assert persisted is not None and persisted.is_active is True
        assert db.scalar(select(Customer).where(Customer.customer_code == "OPT10-03-INVALID")) is None


def test_customer_response_keeps_unknown_historical_status_readable() -> None:
    from app.api.customers import CustomerResponse

    historical = CustomerResponse.model_validate(
        {
            "id": 1,
            "customer_number": 1,
            "customer_code": "LEGACY",
            "name": "旧客户",
            "payment_term_days": 0,
            "statement_cycle_start_day": 20,
            "credit_limit": "0",
            "delivery_method": "配送",
            "default_tax_rate": "0.13",
            "status": "legacy-pending-review",
            "is_active": False,
            "version": 1,
        }
    )
    assert historical.status == "legacy-pending-review"

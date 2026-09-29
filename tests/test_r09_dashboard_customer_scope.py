"""The homepage delivery entry needs only its authorized source identities."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_direct_external_finished import prepare, receive, routing_app, p1_40a_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def test_delivery_only_customer_options_preserve_scope_and_active_state(routing_app):
    from app.api.deliveries import router
    from app.core.security import hash_password
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order
    from app.models.user import User

    routing_app.include_router(router, prefix="/api/deliveries")
    with TestClient(routing_app) as client:
        order_id, purchase_id, line_id = prepare(routing_app, client, ratio="0.5")
        received = receive(client, purchase_id, line_id, 500, key="r09-home-scope")
        assert received.status_code == 200, received.text
        with routing_app.state.factory() as db:
            customer_id = db.get(Order, order_id).customer_id
            customer = db.get(Customer, customer_id)
            customer.customer_code = "UAT-SCOPE"
            actor = User(username="r09-delivery-only", password_hash=hash_password("synthetic-uat-only"),
                role="sales", real_name="Synthetic delivery", must_change_password=False,
                customer_access_mode="selected")
            actor.permission_overrides = [UserPermissionOverride(permission_code=code, is_allowed=allowed)
                for code, allowed in (("deliveries.view", True), ("deliveries.execute", True), ("customers.view", False))]
            actor.customer_scopes = [UserCustomerScope(customer_id=customer_id)]
            db.add(actor); db.commit()
        login = client.post('/api/auth/login', json={"username":"r09-delivery-only", "password":"synthetic-uat-only"})
        assert login.status_code == 200, login.text
        response = client.get('/api/deliveries/pending-customer-options')
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        assert len(rows) == 1 and rows[0]["customer_id"] == customer_id
        assert rows[0]["customer_code"] == "UAT-SCOPE"
        assert not {"phone", "address", "credit_limit", "tax_no"} & rows[0].keys()
        with routing_app.state.factory() as db:
            db.get(Customer, customer_id).is_active = False
            db.commit()
        assert client.get('/api/deliveries/pending-customer-options').json()["items"] == []
        with routing_app.state.factory() as db:
            db.get(Customer, customer_id).is_active = True
            actor = db.scalar(select(User).where(User.username == "r09-delivery-only"))
            actor.customer_scopes.clear()
            db.commit()
        assert client.get('/api/deliveries/pending-customer-options').json()["items"] == []

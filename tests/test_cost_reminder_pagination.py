from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.order import OrderItem
from app.models.order_estimated_cost_snapshot import SalesOrderItemEstimatedCostSnapshot as Snapshot
from app.models.user import User
from tests.test_phase5_orders import _login, _payload, order_api_app


@pytest.mark.parametrize("route,filter_key,filter_value,status", [
    ("cost-review", "health", "estimated_loss", "calculated"),
    ("cost-readiness", "category", "report_dimensions", "missing"),
])
def test_full_scope_paging_reaches_beyond_old_200_limit_without_writes(
    order_api_app, route, filter_key, filter_value, status
):
    app, factory = order_api_app
    with TestClient(app) as client:
        _login(client)
        payload = _payload()
        payload["customer_po"] = "PAGE-LITERAL%_END"
        payload["items"] = payload["items"][:1]
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]
        with factory() as db:
            original = db.get(OrderItem, item_id)
            snapshot = db.scalar(select(Snapshot).where(Snapshot.sales_order_item_id == item_id))
            assert snapshot is not None
            snapshot.calculation_status = status
            snapshot.estimated_order_total_cost = Decimal("999999") if status == "calculated" else None
            snapshot.missing_items_json = json.dumps(["缺少报料长宽"], ensure_ascii=False)
            db.flush()
            item_values = {c.name:getattr(original, c.name) for c in OrderItem.__table__.columns if c.name != "id"}
            cost_values = {c.name:getattr(snapshot, c.name) for c in Snapshot.__table__.columns if c.name != "id"}
            for i in range(205):
                row = OrderItem(**{**item_values, "item_order_number":f"UI-PAGE-{i}", "item_sequence":i+2})
                db.add(row); db.flush()
                db.add(Snapshot(**{**cost_values, "sales_order_item_id":row.id, "order_item_reference_snapshot":row.item_order_number}))
            finance = db.scalar(select(User).where(User.username == "finance"))
            finance.customer_access_mode = "selected"
            db.commit()
            before = list(db.execute(select(Snapshot.id, Snapshot.source_fingerprint, Snapshot.estimated_order_total_cost)).all())
        ids = []
        for page in (1, 2, 3):
            result = client.get(f"/api/orders/{route}", params={"page":page,"page_size":100, filter_key:filter_value, "keyword":"%_"})
            assert result.status_code == 200, result.text
            data = result.json()
            assert data["total_items"] == 206 and data["page_count"] == 3
            assert data["truncated"] is False
            ids += [row["item_id"] for row in data["items"]]
        assert len(ids) == len(set(ids)) == 206
        assert client.get(f"/api/orders/{route}",params={"page_size":101}).status_code == 422
        assert client.get(f"/api/orders/{route}",params={"keyword":"unknown"}).json()["total_items"] == 0
        _login(client, "finance")
        result = client.get(f"/api/orders/{route}",params={"page_size":100,"keyword":"%_",filter_key:filter_value}).json()
        assert result["total_items"] == 0 and result["all_items"] == 0 and result["items"] == []
        with factory() as db:
            assert list(db.execute(select(Snapshot.id, Snapshot.source_fingerprint, Snapshot.estimated_order_total_cost)).all()) == before
            assert db.scalar(select(func.count()).select_from(OrderItem)) == 206

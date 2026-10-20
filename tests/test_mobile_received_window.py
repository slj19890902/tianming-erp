"""Mobile seven-day window preserves existing scope/source reader and paging."""
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import incoming
from app.api.deps import get_db
from app.models.user import User


def test_received_window_cutoff_validation_and_pagination(monkeypatch):
    now = datetime(2026, 10, 20, 12)
    user = User(id=55, username="window-reader", role="admin", is_active=True)
    dates = [now - timedelta(hours=n * 6) for n in range(35)]
    dates += [now - timedelta(days=7), now - timedelta(days=7, seconds=1)]
    seen = []
    def rows(db, *, user, received_since):
        assert db == "reader" and user.id == 55
        seen.append(received_since)
        return [{"receipt_item_id": n, "material_received_at": at}
                for n, at in enumerate(dates) if at >= received_since]
    monkeypatch.setattr(incoming, "_utc_now", lambda: now)
    monkeypatch.setattr(incoming, "_received_rows", rows)
    app = FastAPI()
    app.include_router(incoming.router, prefix="/api/incoming")
    app.dependency_overrides[get_db] = lambda: "reader"
    app.dependency_overrides[incoming.can_read] = lambda: user
    with TestClient(app) as client:
        default = client.get("/api/incoming/received")
        assert default.status_code == 200 and len(default.json()["items"]) == 5
        assert seen[-1] == now - timedelta(days=1)
        first = client.get("/api/incoming/received?days=7&page=1&page_size=20").json()
        second = client.get("/api/incoming/received?days=7&page=2&page_size=20").json()
        assert seen[-1] == now - timedelta(days=7)
        assert first["total"] == second["total"] == 30
        assert len(first["items"]) == 20 and len(second["items"]) == 10
        assert set(r["receipt_item_id"] for r in first["items"]).isdisjoint(r["receipt_item_id"] for r in second["items"])
        assert second["items"][-1]["receipt_item_id"] == 35
        for days in (0, 8):
            assert client.get(f"/api/incoming/received?days={days}").status_code == 422

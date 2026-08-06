from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from factory_twin.backend.app import create_app
from factory_twin.backend.production_projection import read_pending_production_tasks


EDIT_HEADERS = {"X-Editor-Token": "test-token"}


def _source(items: list[dict]):
    def provider() -> dict:
        return {
            "available": True,
            "source_label": "ERP生产任务 · 隔离测试副本",
            "source_read_only": True,
            "fetched_at": "2026-08-06T08:00:00+00:00",
            "items": items,
            "error": None,
        }

    return provider


def _task(task_id: int, order_number: str) -> dict:
    return {
        "source_task_id": task_id,
        "source_version": 3,
        "order_id": task_id + 100,
        "order_number": order_number,
        "customer_name": "昆山华诚电子有限公司",
        "product_code": "HC-520",
        "product_name": "五层加强纸箱",
        "specification": "520×350×300mm",
        "status": "pending",
        "planned_quantity": 800,
        "production_quantity_unit": "sets",
        "task_updated_at": "2026-08-06T15:40:00",
        "delivery_date": "2026-08-08",
    }


def _demo_layout(client: TestClient) -> dict:
    summary = next(
        item
        for item in client.get("/api/layouts").json()
        if item["source_name"] == "demo_factory.dxf"
    )
    return client.get(f"/api/layouts/{summary['id']}").json()


def test_manual_projection_mapping_is_isolated_versioned_and_stale_safe(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'twin.sqlite3').as_posix()}"
    tasks = [_task(11, "TM20260806-0011"), _task(12, "TM20260806-0012")]
    app = create_app(
        database_url=database_url,
        data_dir=tmp_path / "runtime",
        editor_token="test-token",
        production_task_provider=_source(tasks),
    )
    assert not any(
        getattr(route, "path", "").startswith("/api/production")
        for route in app.routes
    )
    with TestClient(app) as client:
        layout = _demo_layout(client)
        zone = next(
            item for item in layout["features"] if item["feature_kind"] == "zone"
        )
        initial = client.get(
            f"/api/layouts/{layout['id']}/production-projections"
        )
        assert initial.status_code == 200, initial.text
        payload = initial.json()
        assert payload["source_read_only"] is True
        assert len(payload["items"]) == 2
        assert all(item["mapping"] is None for item in payload["items"])

        blocked = client.put(
            f"/api/layouts/{layout['id']}/production-projections/11",
            json={"target_kind": "zone", "target_id": zone["id"]},
        )
        assert blocked.status_code == 403

        bound = client.put(
            f"/api/layouts/{layout['id']}/production-projections/11",
            headers=EDIT_HEADERS,
            json={"target_kind": "zone", "target_id": zone["id"]},
        )
        assert bound.status_code == 200, bound.text
        mapping = bound.json()
        assert mapping["source_type"] == "erp_production_task"
        assert mapping["target_code"] == zone["feature_code"]
        assert mapping["confirmed_at"]

        stale_version = client.put(
            f"/api/layouts/{layout['id']}/production-projections/11",
            headers=EDIT_HEADERS,
            json={
                "target_kind": "zone",
                "target_id": zone["id"],
                "version": mapping["version"] + 10,
            },
        )
        assert stale_version.status_code == 409

    restarted = create_app(
        database_url=database_url,
        data_dir=tmp_path / "runtime",
        editor_token="test-token",
        production_task_provider=_source([tasks[1]]),
    )
    with TestClient(restarted) as client:
        layout = _demo_layout(client)
        refreshed = client.get(
            f"/api/layouts/{layout['id']}/production-projections"
        ).json()
        assert [item["source_task_id"] for item in refreshed["items"]] == [12]
        assert refreshed["items"][0]["mapping"] is None
        assert refreshed["stale_mappings"][0]["source_task_id"] == 11
        assert refreshed["stale_mappings"][0]["source_state"] == "not_pending_or_missing"


def test_sqlite_production_source_is_read_only_and_schema_drift_tolerant(
    tmp_path: Path,
) -> None:
    source = tmp_path / "erp-source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE production_tasks (
                id INTEGER PRIMARY KEY, order_item_id INTEGER NOT NULL,
                status TEXT NOT NULL, planned_quantity INTEGER NOT NULL,
                version INTEGER NOT NULL, created_at TEXT
            );
            CREATE TABLE sales_order_items (
                id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL, snapshot_product_name TEXT,
                snapshot_product_code TEXT, snapshot_spec TEXT
            );
            CREATE TABLE sales_orders (
                id INTEGER PRIMARY KEY, order_number TEXT NOT NULL,
                customer_id INTEGER NOT NULL, delivery_date TEXT, status TEXT
            );
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE products (
                id INTEGER PRIMARY KEY, product_code TEXT NOT NULL,
                product_name TEXT NOT NULL
            );
            INSERT INTO customers VALUES (1, '苏州思迈尔包装有限公司');
            INSERT INTO products VALUES (2, 'SM-380', '三层瓦楞外箱');
            INSERT INTO sales_orders VALUES (
                3, 'TM20260806-0003', 1, '2026-08-09', 'pending_production'
            );
            INSERT INTO sales_order_items VALUES (
                4, 3, 2, '三层瓦楞外箱', 'SM-380', '380×260×220mm'
            );
            INSERT INTO production_tasks VALUES (
                5, 4, 'pending', 1200, 2, '2026-08-06 09:30:00'
            );
            """
        )
    before = sha256(source.read_bytes()).hexdigest()
    result = read_pending_production_tasks(source)
    after = sha256(source.read_bytes()).hexdigest()

    assert result["available"] is True
    assert result["source_read_only"] is True
    assert result["items"] == [
        {
            "source_task_id": 5,
            "source_version": 2,
            "order_id": 3,
            "order_number": "TM20260806-0003",
            "customer_name": "苏州思迈尔包装有限公司",
            "product_code": "SM-380",
            "product_name": "三层瓦楞外箱",
            "specification": "380×260×220mm",
            "status": "pending",
            "planned_quantity": 1200,
            "production_quantity_unit": "sets",
            "task_updated_at": "2026-08-06 09:30:00",
            "delivery_date": "2026-08-09",
        }
    ]
    assert after == before

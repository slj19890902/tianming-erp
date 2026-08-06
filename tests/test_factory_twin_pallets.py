from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from factory_twin.backend.app import create_app


EDIT_HEADERS = {"X-Editor-Token": "test-token"}


def _demo_layout(client: TestClient) -> dict:
    summary = next(item for item in client.get("/api/layouts").json() if item["source_name"] == "demo_factory.dxf")
    return client.get(f"/api/layouts/{summary['id']}").json()


def _create_storage_zone(client: TestClient, layout_id: str) -> dict:
    response = client.post(
        f"/api/layouts/{layout_id}/features",
        headers=EDIT_HEADERS,
        json={
            "feature_code": "ZONE-1F-PALLET-TEST",
            "name": "栈板自由组合测试区",
            "feature_kind": "zone",
            "subtype": "finished_storage",
            "points": [[6000, 8000], [11000, 8000], [11000, 11500], [6000, 11500]],
            "storage_mode": "floor",
            "color": "#eab308",
            "source": "manual",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _pallet_payload(code: str, x_mm: float, y_mm: float, **extra) -> dict:
    return {
        "pallet_code": code,
        "name": "标准木质栈板",
        "x_mm": x_mm,
        "y_mm": y_mm,
        "width_mm": 1200,
        "depth_mm": 1000,
        "height_mm": 150,
        "rotation_deg": 0,
        "snap_enabled": True,
        "snap_threshold_mm": 180,
        **extra,
    }


def test_pallet_snap_free_move_restrictions_and_restart_persistence(tmp_path: Path) -> None:
    database = tmp_path / "pallets.sqlite3"
    database_url = f"sqlite+pysqlite:///{database.as_posix()}"
    app = create_app(database_url=database_url, data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(app) as client:
        layout = _demo_layout(client)
        zone = _create_storage_zone(client, layout["id"])

        first_response = client.post(
            f"/api/layouts/{layout['id']}/pallets",
            headers=EDIT_HEADERS,
            json=_pallet_payload("PAL-1F-001", 7000, 8500),
        )
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        assert first["zone_id"] == zone["id"]
        assert first["zone_code"] == zone["feature_code"]

        snapped_response = client.post(
            f"/api/layouts/{layout['id']}/pallets",
            headers=EDIT_HEADERS,
            json=_pallet_payload("PAL-1F-002", 8140, 8500),
        )
        assert snapped_response.status_code == 200, snapped_response.text
        snapped = snapped_response.json()
        assert snapped["snapped"] is True
        assert (snapped["x_mm"], snapped["y_mm"]) == (8200, 8500)

        free_response = client.post(
            f"/api/layouts/{layout['id']}/pallets",
            headers=EDIT_HEADERS,
            json=_pallet_payload("PAL-1F-003", 10000, 10300),
        )
        assert free_response.status_code == 200, free_response.text
        free = free_response.json()
        assert free["snapped"] is False
        assert (free["x_mm"], free["y_mm"]) == (10000, 10300)

        disabled_response = client.patch(
            f"/api/pallets/{snapped['id']}",
            headers=EDIT_HEADERS,
            json={
                "version": snapped["version"],
                "x_mm": 8355,
                "y_mm": 9400,
                "snap_enabled": False,
                "snap_threshold_mm": 180,
            },
        )
        assert disabled_response.status_code == 200, disabled_response.text
        disabled = disabled_response.json()
        assert disabled["snapped"] is False
        assert (disabled["x_mm"], disabled["y_mm"]) == (8355, 9400)

        outside = client.post(
            f"/api/layouts/{layout['id']}/pallets",
            headers=EDIT_HEADERS,
            json=_pallet_payload("PAL-1F-OUTSIDE", 18500, 11000),
        )
        assert outside.status_code == 422
        assert "地面可存放区域" in outside.json()["detail"]

        overlap = client.post(
            f"/api/layouts/{layout['id']}/pallets",
            headers=EDIT_HEADERS,
            json=_pallet_payload("PAL-1F-OVERLAP", first["x_mm"], first["y_mm"], snap_enabled=False),
        )
        assert overlap.status_code == 422
        assert "重叠" in overlap.json()["detail"]

        stale = client.patch(
            f"/api/pallets/{first['id']}",
            headers=EDIT_HEADERS,
            json={"version": first["version"] + 99, "x_mm": 7200},
        )
        assert stale.status_code == 409

    restarted = create_app(database_url=database_url, data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(restarted) as client:
        persisted = _demo_layout(client)
        pallets = {item["pallet_code"]: item for item in persisted["pallets"]}
        assert set(pallets) == {"PAL-1F-001", "PAL-1F-002", "PAL-1F-003"}
        assert (pallets["PAL-1F-002"]["x_mm"], pallets["PAL-1F-002"]["y_mm"]) == (8355, 9400)


def test_pallet_can_share_rack_footprint_but_not_equipment(tmp_path: Path) -> None:
    app = create_app(database_url="sqlite+pysqlite:///:memory:", data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(app) as client:
        layout = _demo_layout(client)
        _create_storage_zone(client, layout["id"])
        rack = client.post(
            f"/api/layouts/{layout['id']}/racks",
            headers=EDIT_HEADERS,
            json={
                "rack_code": "RACK-1F-PALLET-SHARE",
                "name": "允许地面栈板货架",
                "x_mm": 7000,
                "y_mm": 8500,
                "width_mm": 2800,
                "depth_mm": 1100,
                "height_mm": 2200,
            },
        )
        assert rack.status_code == 200, rack.text
        pallet = client.post(
            f"/api/layouts/{layout['id']}/pallets",
            headers=EDIT_HEADERS,
            json=_pallet_payload("PAL-1F-UNDER-RACK", 7000, 8500, snap_enabled=False),
        )
        assert pallet.status_code == 200, pallet.text

        asset = client.get("/api/assets").json()[0]
        equipment = client.post(
            f"/api/layouts/{layout['id']}/placements",
            headers=EDIT_HEADERS,
            json={"template_id": asset["id"], "x_mm": 9600, "y_mm": 8800, "width_mm": 1200, "depth_mm": 1000},
        )
        assert equipment.status_code == 200, equipment.text
        blocked = client.post(
            f"/api/layouts/{layout['id']}/pallets",
            headers=EDIT_HEADERS,
            json=_pallet_payload("PAL-1F-ON-EQUIPMENT", 9600, 8800, snap_enabled=False),
        )
        assert blocked.status_code == 422
        assert "设备" in blocked.json()["detail"]


def test_pallet_turnover_visual_status_is_validated_updated_and_persisted(tmp_path: Path) -> None:
    database = tmp_path / "turnover-status.sqlite3"
    database_url = f"sqlite+pysqlite:///{database.as_posix()}"
    app = create_app(database_url=database_url, data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(app) as client:
        layout = _demo_layout(client)
        _create_storage_zone(client, layout["id"])
        created_response = client.post(
            f"/api/layouts/{layout['id']}/pallets",
            headers=EDIT_HEADERS,
            json=_pallet_payload(
                "SIM-1F-PAL-STATUS",
                6725,
                8500,
                snap_enabled=False,
                visual_status="waiting",
                status_note="模拟·纸板待上机",
                is_simulated=True,
            ),
        )
        assert created_response.status_code == 200, created_response.text
        created = created_response.json()
        assert created["visual_status"] == "waiting"
        assert created["status_note"] == "模拟·纸板待上机"
        assert created["is_simulated"] is True
        assert (created["x_mm"], created["y_mm"]) == (6725, 8500)

        updated_response = client.patch(
            f"/api/pallets/{created['id']}",
            headers=EDIT_HEADERS,
            json={
                "version": created["version"],
                "visual_status": "in_process",
                "status_note": "模拟·印刷后生产周转",
            },
        )
        assert updated_response.status_code == 200, updated_response.text
        updated = updated_response.json()
        assert updated["visual_status"] == "in_process"
        assert updated["status_note"] == "模拟·印刷后生产周转"
        assert (updated["x_mm"], updated["y_mm"]) == (6725, 8500)

        invalid = client.patch(
            f"/api/pallets/{created['id']}",
            headers=EDIT_HEADERS,
            json={"version": updated["version"], "visual_status": "formal_inventory"},
        )
        assert invalid.status_code == 422

    restarted = create_app(database_url=database_url, data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(restarted) as client:
        layout = _demo_layout(client)
        pallet = next(item for item in layout["pallets"] if item["pallet_code"] == "SIM-1F-PAL-STATUS")
        assert pallet["visual_status"] == "in_process"
        assert pallet["status_note"] == "模拟·印刷后生产周转"
        assert pallet["is_simulated"] is True

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from factory_twin.backend.app import create_app


EDIT_HEADERS = {"X-Editor-Token": "test-token"}


def demo_layout(client: TestClient) -> dict:
    summary = next(item for item in client.get("/api/layouts").json() if item["source_name"] == "demo_factory.dxf")
    return client.get(f"/api/layouts/{summary['id']}").json()


def test_equipment_confirmation_lock_blocks_move_rotation_and_delete(tmp_path: Path) -> None:
    app = create_app(database_url="sqlite+pysqlite:///:memory:", data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(app) as client:
        layout = demo_layout(client)
        asset = client.get("/api/assets").json()[0]
        placement = client.post(
            f"/api/layouts/{layout['id']}/placements",
            headers=EDIT_HEADERS,
            json={"template_id": asset["id"], "x_mm": 3200, "y_mm": 4200},
        ).json()
        locked_response = client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={"version": placement["version"], "is_locked": True},
        )
        assert locked_response.status_code == 200, locked_response.text
        locked = locked_response.json()
        assert locked["is_confirmed"] is True
        assert locked["is_locked"] is True
        assert client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={"version": locked["version"], "x_mm": 9999},
        ).status_code == 409
        assert client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={"version": locked["version"], "rotation_deg": 90},
        ).status_code == 409
        assert client.delete(f"/api/placements/{placement['id']}", headers=EDIT_HEADERS).status_code == 409
        persisted = next(item for item in client.get(f"/api/layouts/{layout['id']}").json()["placements"] if item["id"] == placement["id"])
        assert (persisted["x_mm"], persisted["y_mm"]) == (3200, 4200)


def test_rack_feature_candidates_rules_confirmation_and_restart_persistence(tmp_path: Path) -> None:
    database = tmp_path / "phase2a.sqlite3"
    database_url = f"sqlite+pysqlite:///{database.as_posix()}"
    app = create_app(database_url=database_url, data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(app) as client:
        layout = demo_layout(client)
        rack_response = client.post(
            f"/api/layouts/{layout['id']}/racks",
            headers=EDIT_HEADERS,
            json={
                "rack_code": "RACK-1F-TEST-001",
                "name": "纸板周转货架",
                "x_mm": 10000,
                "y_mm": 6000,
                "width_mm": 4000,
                "depth_mm": 1100,
                "height_mm": 2600,
                "levels": 3,
                "bays": 4,
                "access_side": "south",
                "min_aisle_width_mm": 3000,
                "rotation_deg": 90,
                "color": "#8b5cf6",
                "source": "ai",
            },
        )
        assert rack_response.status_code == 200, rack_response.text
        rack = rack_response.json()
        assert rack["status"] == "candidate" and rack["is_locked"] is False
        assert rack["source"] == "ai" and rack["rotation_deg"] == 90

        zone = client.post(
            f"/api/layouts/{layout['id']}/features",
            headers=EDIT_HEADERS,
            json={
                "feature_code": "ZONE-1F-TEST-001",
                "name": "异常纸板隔离区",
                "feature_kind": "zone",
                "subtype": "abnormal_isolation",
                "points": [[4400, 2400], [5600, 2400], [5600, 3600], [4400, 3600]],
                "no_stacking": False,
                "color": "#f97316",
                "source": "ai",
            },
        ).json()
        assert zone["status"] == "candidate"
        assert zone["area_mm2"] == 1_440_000

        aisle = client.post(
            f"/api/layouts/{layout['id']}/features",
            headers=EDIT_HEADERS,
            json={
                "feature_code": "AISLE-1F-TEST-001",
                "name": "测试叉车通道",
                "feature_kind": "aisle",
                "subtype": "forklift",
                "points": [[1000, 8000], [9000, 8000]],
                "width_mm": 2200,
                "direction": "one_way",
                "no_stacking": True,
                "color": "#22c55e",
                "source": "manual",
            },
        ).json()
        assert aisle["area_mm2"] == 17_600_000

        refreshed = client.get(f"/api/layouts/{layout['id']}").json()
        rules = {item["rule_code"] for item in refreshed["violations"]}
        assert "RACK_IN_AISLE" in rules
        assert "ZONE_OVER_COLUMN" in rules
        assert "AISLE_WIDTH_LOW" in rules
        assert refreshed["rule_defaults"]["forklift"] == 3000

        confirmed_rack = client.post(
            f"/api/racks/{rack['id']}/confirm?version={rack['version']}", headers=EDIT_HEADERS
        ).json()
        assert confirmed_rack["status"] == "confirmed" and confirmed_rack["is_locked"] is True
        assert client.patch(
            f"/api/racks/{rack['id']}",
            headers=EDIT_HEADERS,
            json={"version": confirmed_rack["version"], "x_mm": 11000},
        ).status_code == 409
        confirmed_zone = client.post(
            f"/api/features/{zone['id']}/confirm?version={zone['version']}", headers=EDIT_HEADERS
        ).json()
        assert confirmed_zone["status"] == "confirmed"

    restarted_app = create_app(database_url=database_url, data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(restarted_app) as client:
        persisted = demo_layout(client)
        saved_rack = next(item for item in persisted["racks"] if item["rack_code"] == "RACK-1F-TEST-001")
        saved_zone = next(item for item in persisted["features"] if item["feature_code"] == "ZONE-1F-TEST-001")
        assert (saved_rack["x_mm"], saved_rack["y_mm"], saved_rack["rotation_deg"]) == (10000, 6000, 90)
        assert saved_rack["status"] == "confirmed" and saved_rack["is_locked"] is True
        assert saved_zone["points"] == [[4400, 2400], [5600, 2400], [5600, 3600], [4400, 3600]]
        assert saved_zone["status"] == "confirmed"


def test_phase2a_schema_upgrade_preserves_legacy_equipment_coordinates(tmp_path: Path) -> None:
    database = tmp_path / "legacy-layout.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE twin_equipment_placements (
                id VARCHAR(36) PRIMARY KEY,
                layout_id VARCHAR(36) NOT NULL,
                template_id VARCHAR(36) NOT NULL,
                name VARCHAR(160) NOT NULL,
                x_mm FLOAT NOT NULL,
                y_mm FLOAT NOT NULL,
                z_mm FLOAT NOT NULL,
                width_mm FLOAT NOT NULL,
                depth_mm FLOAT NOT NULL,
                height_mm FLOAT NOT NULL,
                rotation_deg INTEGER NOT NULL,
                version INTEGER NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            );
            INSERT INTO twin_equipment_placements VALUES (
                'legacy-placement', 'legacy-layout', 'legacy-template', '人工布置设备',
                1234.5, 6789.25, 0, 3000, 1800, 2000, 90, 7,
                '2026-08-04 00:00:00', '2026-08-04 00:00:00'
            );
            """
        )
    app = create_app(
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        data_dir=tmp_path / "runtime",
        editor_token="test-token",
    )
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(twin_equipment_placements)")}
        row = connection.execute(
            "SELECT x_mm, y_mm, rotation_deg, version, is_confirmed, is_locked FROM twin_equipment_placements WHERE id='legacy-placement'"
        ).fetchone()
    assert {"is_confirmed", "is_locked"}.issubset(columns)
    assert row == (1234.5, 6789.25, 90, 7, 0, 0)


def test_vertical_mold_zone_can_share_footprint_with_die_cutter(tmp_path: Path) -> None:
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path / "runtime",
        editor_token="test-token",
    )
    with TestClient(app) as client:
        layout = demo_layout(client)
        die_cutter = next(item for item in client.get("/api/assets").json() if item["name"] == "模切机")
        placement = client.post(
            f"/api/layouts/{layout['id']}/placements",
            headers=EDIT_HEADERS,
            json={"template_id": die_cutter["id"], "x_mm": 10000, "y_mm": 9500},
        ).json()
        zone_response = client.post(
            f"/api/layouts/{layout['id']}/features",
            headers=EDIT_HEADERS,
            json={
                "feature_code": "ZONE-1F-MOLD-VERTICAL-001",
                "name": "模切机上层模具区",
                "feature_kind": "zone",
                "subtype": "mold",
                "points": [[7800, 8000], [12200, 8000], [12200, 11000], [7800, 11000]],
                "storage_mode": "rack",
                "elevation_mm": 2500,
                "storage_height_mm": 1000,
                "color": "#8b5cf6",
                "source": "manual",
            },
        )
        assert zone_response.status_code == 200, zone_response.text
        zone = zone_response.json()
        assert (zone["storage_mode"], zone["elevation_mm"], zone["storage_height_mm"]) == ("rack", 2500, 1000)
        clear_layout = client.get(f"/api/layouts/{layout['id']}").json()
        overlapping_rules = {
            item["rule_code"]
            for item in clear_layout["violations"]
            if item["entity_id"] == zone["id"] and item.get("related_id") == placement["id"]
        }
        assert "ZONE_OVER_EQUIPMENT" not in overlapping_rules
        assert "ZONE_VERTICAL_OVER_EQUIPMENT" not in overlapping_rules

        lowered = client.patch(
            f"/api/features/{zone['id']}",
            headers=EDIT_HEADERS,
            json={"version": zone["version"], "elevation_mm": 1800},
        )
        assert lowered.status_code == 200, lowered.text
        conflict_layout = client.get(f"/api/layouts/{layout['id']}").json()
        conflict = next(
            item
            for item in conflict_layout["violations"]
            if item["entity_id"] == zone["id"] and item.get("related_id") == placement["id"]
        )
        assert conflict["rule_code"] == "ZONE_VERTICAL_OVER_EQUIPMENT"
        assert "垂直高度区间仍相交" in conflict["message"]


def test_phase2a_frontend_contract() -> None:
    root = Path(__file__).resolve().parents[1] / "factory_twin" / "frontend" / "src"
    app_source = (root / "App.tsx").read_text(encoding="utf-8")
    canvas_source = (root / "EditorCanvas.tsx").read_text(encoding="utf-8")
    for text in ("确认并锁定设备", "参数化货架素材", "区域 / 通道 / 禁放区绘制", "图层开关", "已保存为 AI 候选", "不生成正式库位"):
        assert text in app_source
    assert "resolveFeatureCode" in app_source
    assert "编号重复时自动递增" in app_source
    assert 'setData("application/x-twin-rack"' in app_source
    assert "violationIds" in canvas_source
    assert "onMoveRack" in canvas_source
    assert "placement.is_locked" in canvas_source

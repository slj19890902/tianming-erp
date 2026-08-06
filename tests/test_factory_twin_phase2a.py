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
        unlocked_response = client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={"version": locked["version"], "is_locked": False},
        )
        assert unlocked_response.status_code == 200, unlocked_response.text
        unlocked = unlocked_response.json()
        assert unlocked["is_locked"] is False
        assert unlocked["is_confirmed"] is False
        moved_response = client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={"version": unlocked["version"], "x_mm": 3600, "y_mm": 4400},
        )
        assert moved_response.status_code == 200, moved_response.text
        moved = moved_response.json()
        assert (moved["x_mm"], moved["y_mm"]) == (3600, 4400)
        assert moved["is_confirmed"] is False
        relocked_response = client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={"version": moved["version"], "is_locked": True},
        )
        assert relocked_response.status_code == 200, relocked_response.text
        assert relocked_response.json()["is_confirmed"] is True


def test_confirmed_lift_and_column_are_locked_but_confirmed_wall_remains_editable(
    tmp_path: Path,
) -> None:
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path / "runtime",
        editor_token="test-token",
    )
    with TestClient(app) as client:
        layout = demo_layout(client)
        payloads = {
            "lift": {
                "feature_code": "LIFT-LOCK-001",
                "name": "共同货梯",
                "feature_kind": "structure",
                "subtype": "freight_elevator",
                "points": [[1000, 1000], [3000, 1000]],
                "width_mm": 3000,
                "storage_height_mm": 3500,
                "color": "#0f766e",
                "source": "manual",
            },
            "column": {
                "feature_code": "COL-1F-LOCK-001",
                "name": "投影柱",
                "feature_kind": "structure",
                "subtype": "custom_column",
                "points": [[5000, 1000], [5700, 1000]],
                "width_mm": 700,
                "storage_height_mm": 3000,
                "color": "#334155",
                "source": "manual",
            },
            "wall": {
                "feature_code": "WALL-1F-EDIT-001",
                "name": "可调整墙体",
                "feature_kind": "structure",
                "subtype": "custom_wall",
                "points": [[1000, 6000], [6000, 6000]],
                "width_mm": 120,
                "storage_height_mm": 3000,
                "color": "#475569",
                "source": "manual",
            },
        }
        created = {}
        for key, payload in payloads.items():
            response = client.post(
                f"/api/layouts/{layout['id']}/features",
                headers=EDIT_HEADERS,
                json=payload,
            )
            assert response.status_code == 200, response.text
            row = response.json()
            confirmed = client.post(
                f"/api/features/{row['id']}/confirm?version={row['version']}",
                headers=EDIT_HEADERS,
            )
            assert confirmed.status_code == 200, confirmed.text
            created[key] = confirmed.json()

        assert created["lift"]["is_locked"] is True
        assert created["column"]["is_locked"] is True
        assert created["wall"]["is_locked"] is False
        for key in ("lift", "column"):
            row = created[key]
            assert client.patch(
                f"/api/features/{row['id']}",
                headers=EDIT_HEADERS,
                json={"version": row["version"], "points": [[9000, 9000], [10000, 9000]]},
            ).status_code == 409
            assert client.delete(
                f"/api/features/{row['id']}", headers=EDIT_HEADERS
            ).status_code == 409

        wall = created["wall"]
        edited = client.patch(
            f"/api/features/{wall['id']}",
            headers=EDIT_HEADERS,
            json={"version": wall["version"], "points": [[1200, 6000], [6200, 6000]]},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["status"] == "candidate"
        assert edited.json()["is_locked"] is False
        deleted = client.delete(
            f"/api/features/{wall['id']}", headers=EDIT_HEADERS
        )
        assert deleted.status_code == 204, deleted.text
        remaining_ids = {
            item["id"]
            for item in client.get(f"/api/layouts/{layout['id']}").json()["features"]
        }
        assert wall["id"] not in remaining_ids


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
        assert refreshed["rule_defaults"]["shared_main"] == 1900
        assert refreshed["rule_defaults"]["shared_secondary"] == 1500

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
    for text in ("确认并锁定设备", "参数化货架素材", "区域 / 通道 / 墙门窗柱绘制", "图层开关", "已保存为 AI 候选", "不生成正式库位", "2D拖动区域和人工结构", "LIFT 同编号连接楼层", "区域长宽与位置"):
        assert text in app_source
    assert "resolveFeatureCode" in app_source
    assert "translatePointsMm" in app_source
    assert "onMoveFeature" in canvas_source
    assert "编号重复时自动递增" in app_source
    assert 'setData("application/x-twin-rack"' in app_source
    assert "violationIds" in canvas_source
    assert "onMoveRack" in canvas_source
    assert "placement.is_locked" in canvas_source
    assert "cameraStateRef" in canvas_source
    assert "pendingSelection" in canvas_source
    assert 'kind === "equipment" || kind === "rack" || kind === "feature"' in canvas_source
    assert 'draggable: feature.subtype !== "dxf_hidden" && !feature.is_locked' in canvas_source
    pointer_down = canvas_source.split("const onPointerDown", 1)[1].split("const onPointerMove", 1)[0]
    assert "pendingSelection =" in pointer_down
    assert "onSelect({ kind, id });" not in pointer_down
    assert "Selection is intentionally deferred until release" in canvas_source
    assert "customStructures" in canvas_source
    assert "隐藏误识别结构" in app_source
    assert "实际净宽" in app_source
    assert "custom_column" in app_source
    assert "resizeAndMovePointsMm" in app_source
    assert "freight_elevator" in app_source
    assert "跨楼层定位" in app_source
    assert "elevatorPeers" in app_source
    assert "送剩零头长期存放" in app_source
    assert "地线分区（用途待确认）" in app_source
    assert "保存区域用途" in app_source
    assert "成品存放（可临时混放）" in app_source
    assert "人/液压搬运车共用" in app_source
    assert "Ground pallet + upper pallet loads" in (root / "industrialScene.ts").read_text(encoding="utf-8")
    assert "controls.mouseButtons.LEFT = THREE.MOUSE.PAN" in canvas_source
    assert "pendingCanvasAction" in canvas_source
    assert "左键拖动空白处平移" in app_source
    pointer_down = canvas_source.split("const onPointerDown", 1)[1].split("const onPointerMove", 1)[0]
    assert "onDrawPoint(" not in pointer_down
    assert "onMeasurePoint(" not in pointer_down


def test_manual_wall_door_window_and_dxf_hide_persist(tmp_path: Path) -> None:
    database = tmp_path / "manual-structures.sqlite3"
    database_url = f"sqlite+pysqlite:///{database.as_posix()}"
    app = create_app(database_url=database_url, data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(app) as client:
        layout = demo_layout(client)
        payloads = [
            {
                "feature_code": "STRUCT-1F-WALL-001", "name": "办公室简易隔墙",
                "feature_kind": "structure", "subtype": "custom_wall",
                "points": [[1000, 1000], [5000, 1000], [5000, 4000]],
                "width_mm": 120, "storage_height_mm": 3000, "color": "#475569", "source": "manual",
            },
            {
                "feature_code": "STRUCT-1F-DOOR-001", "name": "物流口卷帘门",
                "feature_kind": "structure", "subtype": "rolling_door",
                "points": [[6000, 1000], [10500, 1000]],
                "width_mm": 100, "storage_height_mm": 3800, "color": "#d97706", "source": "manual",
            },
            {
                "feature_code": "STRUCT-1F-WIN-001", "name": "办公室观察窗",
                "feature_kind": "structure", "subtype": "custom_window",
                "points": [[12000, 1000], [14000, 1000]],
                "width_mm": 80, "elevation_mm": 900, "storage_height_mm": 1400,
                "color": "#38bdf8", "source": "manual",
            },
            {
                "feature_code": "COL-1F-001", "name": "人工补录方柱",
                "feature_kind": "structure", "subtype": "custom_column",
                "points": [[15000, 1000], [15600, 1000]],
                "width_mm": 600, "storage_height_mm": 3000,
                "color": "#334155", "source": "manual",
            },
            {
                "feature_code": "LIFT-001", "name": "1F/3F 共用货梯",
                "feature_kind": "structure", "subtype": "freight_elevator",
                "points": [[16000, 1000], [19000, 1000]],
                "width_mm": 2500, "storage_height_mm": 3500,
                "color": "#0f766e", "source": "manual",
            },
            {
                "feature_code": "HIDE-1F-FAKE-DOOR", "name": "隐藏 DXF 结构 FAKE-DOOR",
                "feature_kind": "structure", "subtype": "dxf_hidden",
                "points": [[1, 1], [2, 1]], "width_mm": 1, "storage_height_mm": 1,
                "color": "#64748b", "source": "manual",
            },
        ]
        created = []
        for payload in payloads:
            response = client.post(
                f"/api/layouts/{layout['id']}/features", headers=EDIT_HEADERS, json=payload
            )
            assert response.status_code == 200, response.text
            created.append(response.json())
        assert created[0]["area_mm2"] == 840_000
        assert created[1]["area_mm2"] == 450_000
        assert created[2]["elevation_mm"] == 900
        assert created[3]["feature_code"] == "COL-1F-001"
        assert created[3]["area_mm2"] == 360_000
        assert created[4]["feature_code"] == "LIFT-001"
        assert created[4]["area_mm2"] == 7_500_000
        semantic_zone = client.post(
            f"/api/layouts/{layout['id']}/features",
            headers=EDIT_HEADERS,
            json={
                "feature_code": "ZONE-1F-SURPLUS-001", "name": "原成品待送区",
                "feature_kind": "zone", "subtype": "finished_wait_delivery",
                "points": [[1000, 5000], [3000, 5000], [3000, 6500], [1000, 6500]],
                "color": "#f59e0b", "source": "manual",
            },
        )
        assert semantic_zone.status_code == 200, semantic_zone.text
        semantic = client.patch(
            f"/api/features/{semantic_zone.json()['id']}",
            headers=EDIT_HEADERS,
            json={
                "version": semantic_zone.json()["version"],
                "name": "送货余数暂存区（墙边）",
                "subtype": "delivery_surplus",
                "color": "#f97316",
            },
        )
        assert semantic.status_code == 200, semantic.text
        assert semantic.json()["subtype"] == "delivery_surplus"
        assert semantic.json()["status"] == "candidate"
        renamed = client.patch(
            f"/api/features/{semantic.json()['id']}", headers=EDIT_HEADERS,
            json={"version": semantic.json()["version"], "feature_code": "ZONE-1F-SURPLUS-RENAMED"},
        )
        assert renamed.status_code == 200, renamed.text
        duplicate = client.patch(
            f"/api/features/{renamed.json()['id']}", headers=EDIT_HEADERS,
            json={"version": renamed.json()["version"], "feature_code": created[0]["feature_code"]},
        )
        assert duplicate.status_code == 409
        confirmed = client.post(
            f"/api/features/{created[1]['id']}/confirm?version={created[1]['version']}", headers=EDIT_HEADERS
        )
        assert confirmed.status_code == 200

    restarted_app = create_app(database_url=database_url, data_dir=tmp_path / "runtime", editor_token="test-token")
    with TestClient(restarted_app) as client:
        saved = demo_layout(client)["features"]
        assert {item["feature_code"] for item in saved}.issuperset({item["feature_code"] for item in created})
        door = next(item for item in saved if item["feature_code"] == "STRUCT-1F-DOOR-001")
        assert door["points"] == [[6000.0, 1000.0], [10500.0, 1000.0]]
        assert door["status"] == "confirmed"

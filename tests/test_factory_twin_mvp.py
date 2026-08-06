from __future__ import annotations

import base64
from pathlib import Path

import ezdxf
from fastapi.testclient import TestClient

from factory_twin.backend.app import create_app
from factory_twin.backend.dxf_parser import parse_layout_dxf


EDIT_HEADERS = {"X-Editor-Token": "test-token"}
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def build_test_dxf(path: Path, *, units: int = 4) -> Path:
    document = ezdxf.new("R2007")
    document.header["$INSUNITS"] = units
    modelspace = document.modelspace()
    modelspace.add_lwpolyline(
        [(0, 0), (20000, 0), (20000, 12000), (0, 12000)],
        close=True,
        dxfattribs={"layer": "外墙"},
    )
    modelspace.add_circle((15000, 3000), 300, dxfattribs={"layer": "01_COLUMN_柱子"})
    modelspace.add_circle((5000, 9000), 300, dxfattribs={"layer": "01_COLUMN_柱子"})
    modelspace.add_line((8500, 0), (11500, 0), dxfattribs={"layer": "doors"})
    modelspace.add_line((1000, 1000), (2000, 1000), dxfattribs={"layer": "辅助标注"})
    document.saveas(path)
    return path


def test_dxf_parser_classifies_locked_structure_and_numbers_columns(tmp_path: Path) -> None:
    parsed = parse_layout_dxf(build_test_dxf(tmp_path / "factory-mm.dxf"), floor_code="1F")

    assert parsed["source"]["input_units"] == "mm"
    assert parsed["bounds_mm"] == {
        "min_x": 0.0,
        "min_y": 0.0,
        "max_x": 20000.0,
        "max_y": 12000.0,
    }
    walls = [item for item in parsed["structures"] if item["kind"] == "exterior_wall"]
    columns = [item for item in parsed["structures"] if item["kind"] == "column"]
    doors = [item for item in parsed["structures"] if item["kind"] == "door"]
    unknown = [item for item in parsed["structures"] if item["kind"] == "unknown"]
    assert len(walls) == 1 and walls[0]["locked"] is True
    numbered_centers = {
        item["column_code"]: (item["geometry"]["x_mm"], item["geometry"]["y_mm"])
        for item in columns
    }
    assert numbered_centers == {
        "COL-1F-001": (5000.0, 9000.0),
        "COL-1F-002": (15000.0, 3000.0),
    }
    assert all(item["locked"] for item in columns)
    assert len(doors) == 1 and doors[0]["source_readonly"] is True
    assert len(unknown) == 1
    assert any("灰色参考线" in warning for warning in parsed["warnings"])


def test_dxf_parser_converts_metre_units_to_millimetres(tmp_path: Path) -> None:
    source = build_test_dxf(tmp_path / "factory-m.dxf", units=6)
    document = ezdxf.readfile(source)
    # The helper creates millimetre-looking values; scale the first wall to a 20m x 12m test.
    wall = next(entity for entity in document.modelspace() if entity.dxftype() == "LWPOLYLINE")
    wall.set_points([(0, 0), (20, 0), (20, 12), (0, 12)], format="xy")
    document.saveas(source)

    parsed = parse_layout_dxf(source)
    assert parsed["source"]["input_units"] == "m"
    assert parsed["bounds_mm"]["max_x"] >= 20000
    assert parsed["bounds_mm"]["max_y"] >= 12000


def test_dxf_parser_recognizes_windows_and_preserves_insert_opening_width(tmp_path: Path) -> None:
    source = tmp_path / "openings.dxf"
    document = ezdxf.new("R2007")
    document.header["$INSUNITS"] = 6
    window_block = document.blocks.new("WINDOW-2400")
    window_block.add_lwpolyline([(-1200, 0), (1200, 0)])
    door_block = document.blocks.new("DOOR-4500")
    door_block.add_lwpolyline([(0, 0), (0, 4500)])
    modelspace = document.modelspace()
    modelspace.add_blockref("WINDOW-2400", (2, 3), dxfattribs={"layer": "windows", "xscale": 0.001, "yscale": 0.001})
    modelspace.add_blockref("DOOR-4500", (5, 7), dxfattribs={"layer": "doors", "xscale": 0.001, "yscale": 0.001, "rotation": 90})
    document.saveas(source)

    parsed = parse_layout_dxf(source, floor_code="3F")
    window = next(item for item in parsed["structures"] if item["kind"] == "window")
    door = next(item for item in parsed["structures"] if item["kind"] == "door")
    assert window["geometry"]["opening_width_mm"] == 2400
    assert door["geometry"]["opening_width_mm"] == 4500
    assert len(window["geometry"]["points"]) == 2
    assert len(door["geometry"]["points"]) == 2


def test_layout_api_persists_coordinates_and_rejects_stale_updates(tmp_path: Path) -> None:
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path / "runtime",
        editor_token="test-token",
    )
    source = build_test_dxf(tmp_path / "factory.dxf")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["source_write"] is False
        assert client.post(
            "/api/layouts/import-dxf",
            files={"file": ("factory.dxf", source.read_bytes(), "application/dxf")},
            data={"name": "一楼真实布局", "floor_code": "1F"},
        ).status_code == 403

        response = client.post(
            "/api/layouts/import-dxf",
            headers=EDIT_HEADERS,
            files={"file": ("factory.dxf", source.read_bytes(), "application/dxf")},
            data={"name": "一楼真实布局", "floor_code": "1F"},
        )
        assert response.status_code == 200, response.text
        layout = response.json()
        duplicate = client.post(
            "/api/layouts/import-dxf",
            headers=EDIT_HEADERS,
            files={"file": ("factory.dxf", source.read_bytes(), "application/dxf")},
            data={"name": "重复导入", "floor_code": "1F"},
        ).json()
        assert duplicate["id"] == layout["id"]
        assert duplicate["name"] == "重复导入"
        assert duplicate["placements"] == []

        assets = client.get("/api/assets").json()
        placement_response = client.post(
            f"/api/layouts/{layout['id']}/placements",
            headers=EDIT_HEADERS,
            json={"template_id": assets[0]["id"], "x_mm": 3200, "y_mm": 4800},
        )
        assert placement_response.status_code == 200, placement_response.text
        placement = placement_response.json()
        assert placement["width_mm"] == assets[0]["default_width_mm"]

        invalid_rotation = client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={"version": placement["version"], "rotation_deg": 45},
        )
        assert invalid_rotation.status_code == 422

        updated_response = client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={
                "version": placement["version"],
                "x_mm": 3600,
                "y_mm": 5200,
                "width_mm": 7000,
                "rotation_deg": 90,
            },
        )
        assert updated_response.status_code == 200, updated_response.text
        updated = updated_response.json()
        assert (updated["x_mm"], updated["y_mm"], updated["width_mm"]) == (3600, 5200, 7000)
        assert updated["rotation_deg"] == 90
        assert updated["version"] == 2

        stale = client.patch(
            f"/api/placements/{placement['id']}",
            headers=EDIT_HEADERS,
            json={"version": 1, "x_mm": 1},
        )
        assert stale.status_code == 409
        persisted = client.get(f"/api/layouts/{layout['id']}").json()["placements"][0]
        assert persisted["x_mm"] == 3600
        assert persisted["version"] == 2


def test_png_asset_upload_is_validated_and_served(tmp_path: Path) -> None:
    app = create_app(
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path / "runtime",
        editor_token="test-token",
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/assets",
            headers=EDIT_HEADERS,
            data={
                "name": "透明底分纸机",
                "category": "分切",
                "render_type": "png",
                "color": "#2563eb",
                "default_width_mm": "3000",
                "default_depth_mm": "1800",
                "default_height_mm": "1900",
            },
            files={"image": ("slitter.png", PNG_1X1, "image/png")},
        )
        assert response.status_code == 200, response.text
        asset = response.json()
        assert asset["image_url"].startswith("/uploads/")
        image_response = client.get(asset["image_url"])
        assert image_response.status_code == 200
        assert image_response.content == PNG_1X1

        invalid = client.post(
            "/api/assets",
            headers=EDIT_HEADERS,
            data={
                "name": "伪 PNG",
                "category": "测试",
                "render_type": "png",
                "color": "#2563eb",
                "default_width_mm": "1000",
                "default_depth_mm": "1000",
                "default_height_mm": "1000",
            },
            files={"image": ("bad.png", b"not-a-png", "image/png")},
        )
        assert invalid.status_code == 422


def test_frontend_contract_contains_required_editor_controls() -> None:
    root = Path(__file__).resolve().parents[1] / "factory_twin" / "frontend" / "src"
    app_source = (root / "App.tsx").read_text(encoding="utf-8")
    canvas_source = (root / "EditorCanvas.tsx").read_text(encoding="utf-8")

    assert "二维 CAD" in app_source
    assert "2.5D 等距" in app_source
    assert "旋转 90°" in app_source
    assert "default_width_mm" in app_source
    assert 'setData("application/x-twin-asset"' in app_source
    assert "onMoveEquipment" in canvas_source
    assert "onMoveRack" in canvas_source
    assert "OrbitControls" in canvas_source
    assert "source_readonly" not in canvas_source or "structures" in canvas_source

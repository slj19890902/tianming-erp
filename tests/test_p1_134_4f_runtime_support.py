from __future__ import annotations

import json
from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api import warehouse as warehouse_api
from app.services import warehouse_twin_layout_editor as layout_editor
from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_floor,
)


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_TWIN_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
EDITOR_CANVAS_SOURCE = (
    ROOT / "factory_twin" / "frontend" / "src" / "EditorCanvas.tsx"
).read_text(encoding="utf-8")
WAREHOUSE_API_SOURCE = (ROOT / "app" / "api" / "warehouse.py").read_text(
    encoding="utf-8"
)
WAREHOUSE_TWIN_CSS = (
    ROOT / "factory_twin" / "frontend" / "src" / "warehouseTwin.css"
).read_text(encoding="utf-8")
WAREHOUSE_MOVEMENT_SOURCE = (
    ROOT / "app" / "services" / "warehouse_movement_batch.py"
).read_text(encoding="utf-8")
WAREHOUSE_DASHBOARD_SOURCE = (
    ROOT / "app" / "services" / "warehouse_twin_dashboard.py"
).read_text(encoding="utf-8")
ERP_SHELL_SOURCE = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _layout_asset(path: Path) -> Path:
    floors = {
        floor_code: {
            "floor_code": floor_code,
            "name": f"{floor_code}实测布局",
            "revision": f"revision-{floor_code.lower()}",
            "features": [],
            "structures": [],
            "racks": [],
            "placements": [],
        }
        for floor_code in ("1F", "3F", "4F")
    }
    path.write_text(
        json.dumps({"schema_version": 1, "generated_at": "test", "floors": floors}),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def uncalibrated_floor4_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation

    engine = create_sqlite_engine(tmp_path / "uncalibrated-floor4.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="floor4-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="四楼门禁测试管理员",
            must_change_password=False,
        )
        location = WarehouseLocation(
            location_code="4F-PLANNING-L01",
            location_name="四楼未标定规划位",
            warehouse_type="finished",
            warehouse_floor=4,
            area_code="PLANNING",
            storage_type="rack",
            placement_status="placed",
            is_active=True,
        )
        db.add_all([admin, location])
        db.commit()
        location_id = int(location.id)

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, location_id
    finally:
        engine.dispose()


def _login_floor4_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "floor4-admin", "password": "123456"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "get",
            "/api/warehouse/twin-operations/location-product-candidates",
            None,
        ),
        (
            "post",
            "/api/warehouse/twin-operations/staging-lots/999/place",
            {
                "expected_layout_version": 1,
                "expected_version": 1,
                "quantity": 1,
                "idempotency_key": "p1-134-floor4-stage-block",
                "confirmed": True,
            },
        ),
        (
            "post",
            "/api/warehouse/twin-operations/temporary-finished-inbound",
            {
                "expected_layout_version": 1,
                "customer_id": 999,
                "inventory_code": "P1-134-FLOOR4-BLOCK",
                "product_name": "四楼门禁测试纸箱",
                "quantity": 1,
                "stock_date": date(2026, 9, 1).isoformat(),
                "reason": "验证未标定四楼不得写入库存",
                "idempotency_key": "p1-134-floor4-temp-block",
                "confirmed": True,
            },
        ),
    ],
    ids=("candidate-read", "staging-placement", "temporary-inbound"),
)
def test_uncalibrated_floor4_target_is_rejected_before_inventory_use(
    uncalibrated_floor4_app,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    app, location_id = uncalibrated_floor4_app
    with TestClient(app) as client:
        _login_floor4_admin(client)
        if method == "get":
            response = client.get(path, params={"location_id": location_id})
        else:
            response = client.post(
                path,
                json={"location_id": location_id, **(payload or {})},
            )

    assert response.status_code == 409, response.text
    assert "目标货位不可用" in str(response.json().get("detail"))
    assert "正式" in str(response.json().get("detail"))


def test_runtime_loader_and_editor_accept_4f_but_keep_unknown_floors_closed(
    tmp_path: Path,
) -> None:
    asset = _layout_asset(tmp_path / "twin-layout.json")

    assert load_warehouse_twin_floor("4f", path=asset)["floor_code"] == "4F"
    assert layout_editor._normalize_floor_code("4f") == "4F"
    with pytest.raises(WarehouseTwinLayoutNotFoundError):
        load_warehouse_twin_floor("2F", path=asset)
    with pytest.raises(layout_editor.WarehouseTwinLayoutEditNotFoundError):
        layout_editor._normalize_floor_code("2F")


def test_reference_area_search_scans_4f_without_losing_1f_or_3f(monkeypatch) -> None:
    requested: list[str] = []

    def load_floor(floor_code: str) -> dict:
        requested.append(floor_code)
        return {
            "features": [
                {
                    "id": f"zone-{floor_code.lower()}",
                    "feature_code": f"ZONE-{floor_code}-PRINT",
                    "name": f"{floor_code}印刷版区",
                    "feature_kind": "zone",
                    "subtype": "printing_plate",
                    "erp_area_code": "PRINT",
                }
            ]
        }

    monkeypatch.setattr(warehouse_api, "load_warehouse_twin_floor", load_floor)
    monkeypatch.setattr(
        warehouse_api,
        "overlay_formal_area_bindings",
        lambda _db, *, floor_code, floor_layout, include_draft=False: floor_layout,
    )

    resources = warehouse_api._twin_reference_area_resources(object(), "印刷版")

    assert requested == ["1F", "3F", "4F"]
    assert [item["floor_code"] for item in resources] == ["1F", "3F", "4F"]


def test_warehouse_frontend_treats_4f_as_an_operational_map_floor() -> None:
    assert 'type WarehouseOperationalFloorCode = "1F" | "3F" | "4F"' in WAREHOUSE_TWIN_SOURCE
    assert 'switchWarehouseFloor("4F")' in WAREHOUSE_TWIN_SOURCE
    assert '<b>4F</b><span>成品仓库</span>' in WAREHOUSE_TWIN_SOURCE
    assert 'isWarehouseOperationalFloorCode(payload.floor_code)' in WAREHOUSE_TWIN_SOURCE
    assert 'disabled={!isWarehouseOperationalFloorCode(floor.floor_code)}' in WAREHOUSE_TWIN_SOURCE
    assert '["1F", "3F"].includes(floorCode)' in EDITOR_CANVAS_SOURCE
    assert 'floorCode === "4F"' in EDITOR_CANVAS_SOURCE
    assert 'calibration?.status === "aligned"' in EDITOR_CANVAS_SOURCE
    assert 'calibration?.applied === true' in EDITOR_CANVAS_SOURCE
    assert 'layout.alignment_status === "aligned"' in EDITOR_CANVAS_SOURCE
    assert "layout.alignment_applied === true" in EDITOR_CANVAS_SOURCE
    assert "已与 3F 货梯对齐" in WAREHOUSE_TWIN_SOURCE
    assert 'name: floor4Aligned ? "四楼实测成品仓库（已与3F货梯对齐）" : raw.name' in WAREHOUSE_TWIN_SOURCE
    assert "metadata: raw.metadata" in WAREHOUSE_TWIN_SOURCE
    assert "alignment_status: raw.alignment_status" in WAREHOUSE_TWIN_SOURCE


def test_simple_area_planner_collects_the_name_without_opening_advanced_tools() -> None:
    planner = WAREHOUSE_TWIN_SOURCE[
        WAREHOUSE_TWIN_SOURCE.index('className="twin-zone-simple-planner"') :
        WAREHOUSE_TWIN_SOURCE.index('className="twin-location-point-planner"')
    ]

    assert '<span>区域名称</span><input maxLength={100}' in planner
    assert "value={formalAreaNameDraft}" in planner
    assert "setFormalAreaNameDraft(event.target.value)" in planner
    assert 'placeholder="例如 4F 新振成品区"' in planner


def test_empty_area_one_step_confirmation_does_not_repeat_a_native_dialog() -> None:
    section = WAREHOUSE_TWIN_SOURCE[
        WAREHOUSE_TWIN_SOURCE.index("const confirmSelectedAreaOnce") :
        WAREHOUSE_TWIN_SOURCE.index("const saveLayoutFeatureGeometry")
    ]

    assert "window.confirm" not in section
    assert 'setLocationEditMessage("正在确认并启用区域…")' in section
    assert "confirmed: true" in section


def test_production_keeps_one_compact_pending_entry_and_refreshes_the_queue() -> None:
    desktop = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert desktop.count(">待生产 {{ productionPendingTotal }}</button>") == 1
    assert "productionTab='pending'; loadProductionPage(1)" in desktop
    assert '@click="loadProductionPage(pages.productionPending || 1)"' in desktop
    assert desktop.count('@click="batchConfirmProduction"') == 1
    assert "点顶部“确认入库”" in desktop
    assert 'if (this.productionTab === "pending") {' in desktop
    assert 'this.productionTab = "pending";' in desktop


def test_erp_shell_exposes_and_tracks_4f_as_a_warehouse_floor() -> None:
    assert "@click=\"selectWarehouseFloor('4F')\">4F 成品仓库" in ERP_SHELL_SOURCE
    assert (
        'warehouseTwinFloors: [{code:"1F",label:"生产车间"},'
        '{code:"3F",label:"成品仓库"},{code:"4F",label:"成品仓库"}]'
        in ERP_SHELL_SOURCE
    )
    assert "isWarehouseTwinFloorCode(floorCode)" in ERP_SHELL_SOURCE
    assert ERP_SHELL_SOURCE.count("this.isWarehouseTwinFloorCode(") >= 4
    assert '["1F", "3F", "4F"].includes(' not in ERP_SHELL_SOURCE


def test_warehouse_operations_and_map_resource_queries_allow_4f() -> None:
    assert 'if location.warehouse_floor not in {1, 3, 4}:' in WAREHOUSE_API_SOURCE
    assert 'for floor_code in ("1F", "3F", "4F"):' in WAREHOUSE_API_SOURCE
    assert WAREHOUSE_API_SOURCE.count('Literal["1F", "3F", "4F"]') >= 2
    assert "source.warehouse_floor not in {1, 3, 4}" in WAREHOUSE_MOVEMENT_SOURCE
    assert "dashboard_floor_numbers.append(4)" in WAREHOUSE_DASHBOARD_SOURCE


def test_narrow_planning_toolbar_keeps_operation_labels_on_one_line() -> None:
    operation_modes = WAREHOUSE_TWIN_CSS[
        WAREHOUSE_TWIN_CSS.index(".twin-operation-modes {") :
        WAREHOUSE_TWIN_CSS.index(".twin-operation-modes button.active")
    ]
    view_tools = WAREHOUSE_TWIN_CSS[
        WAREHOUSE_TWIN_CSS.index(".twin-toolbar-view-tools {") :
        WAREHOUSE_TWIN_CSS.index(".warehouse-twin-shell .twin-operation-modes,")
    ]

    assert "flex: 0 0 auto;" in operation_modes
    assert "white-space: nowrap;" in operation_modes
    assert "flex: 0 0 auto;" in view_tools

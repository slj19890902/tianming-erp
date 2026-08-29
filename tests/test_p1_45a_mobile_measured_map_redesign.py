from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_n036_delivery_pick import _create_task, _login, pick_app


ROOT = Path(__file__).resolve().parents[1]
PICK_HTML = (ROOT / "static" / "mobile_delivery_pick.html").read_text(
    encoding="utf-8"
)
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")


def _script(html: str) -> str:
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert match is not None
    return match.group(1)


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def _run_node(tmp_path: Path, name: str, source: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the mobile measured-map contract"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _add_measured_pick_location(
    pick_app,
    monkeypatch: pytest.MonkeyPatch,
    *,
    floor: int = 3,
    area_code: str = "C1",
    location_code: str = "F3-C1-08",
) -> tuple[int, int]:
    from app.models.order import OrderItem
    from app.models.user import User
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryReservation,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseLocation,
    )
    from app.services import location_candidates
    from app.services.warehouse_inventory import manual_finished_in

    current_revision = "p1-45a-current-map"
    if floor == 3:
        feature_id = f"zone-p1-45a-{area_code.lower()}"
        monkeypatch.setattr(
            location_candidates,
            "load_warehouse_twin_published_floor_identity",
            lambda floor_number: (
                {
                    "revision": current_revision,
                    "zones_by_id": {feature_id: area_code},
                    "zone_ids_by_area": {area_code: (feature_id,)},
                }
                if int(floor_number) == floor
                else None
            ),
        )
    else:
        feature_id = None

    _app, factory, ids, _operation_log = pick_app
    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        order_item = db.get(OrderItem, ids["order_items"][0])
        assert admin is not None and order_item is not None
        location = WarehouseLocation(
            location_code=location_code,
            location_name=f"{floor}楼 {area_code} 实测位置",
            warehouse_type="finished",
            warehouse_floor=floor,
            area_code=area_code,
            storage_type="rack",
            sort_order=8,
            placement_status="placed",
            source_version="TWIN_V1",
        )
        db.add(location)
        db.flush()
        db.add(
            Floor3LocationLayout(
                location_id=location.id,
                left_pct=Decimal("22"),
                top_pct=Decimal("31"),
                width_pct=Decimal("9"),
                height_pct=Decimal("8"),
                source_type="manual",
                created_by=admin.id,
            )
        )
        lot = manual_finished_in(
            db,
            customer_id=ids["customer"],
            product_id=order_item.product_id,
            location_id=location.id,
            quantity=100,
            stock_date=date(2026, 8, 12),
            source_type="manual",
            remarks=None,
            operator_id=admin.id,
            idempotency_key="p1-45a-measured-lot",
            expected_layout_version=1,
        )
        lot.quantity_available = 0
        lot.quantity_reserved = 100
        db.add(
            InventoryReservation(
                reservation_number="RSV-P1-45A-MAP",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=ids["order"],
                order_item_id=order_item.id,
                reserved_stock_quantity=100,
                credited_requirement_quantity=100,
                status="active",
                reserved_by=admin.id,
                reservation_group_key="p1-45a-map",
                idempotency_key="p1-45a-map-reservation",
            )
        )
        floor_row = WarehouseFloor(
            floor_code=f"{floor}F",
            floor_name=f"{floor}楼",
            floor_number=floor,
            construction_status="enabled",
        )
        db.add(floor_row)
        db.flush()
        area = WarehouseArea(
            floor_id=floor_row.id,
            area_code=area_code,
            area_name=f"{area_code}区",
            construction_status="enabled",
        )
        db.add(area)
        db.flush()
        if feature_id is not None:
            db.add(
                WarehouseAreaStoragePolicy(
                    area_id=area.id,
                    map_feature_id=feature_id,
                    allowed_inventory_types_json='["finished"]',
                    storage_layout="rack",
                    status="published",
                    published_map_revision=current_revision,
                )
            )
        db.commit()
        return int(location.id), int(lot.id)


def test_pick_task_measured_map_is_read_only_scoped_and_picker_accessible(
    pick_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, factory, ids, operation_log = pick_app
    location_id, _lot_id = _add_measured_pick_location(pick_app, monkeypatch)
    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])
        task_id = int(task["id"])
        _login(client, "delivery_picker")
        with factory() as db:
            before_logs = db.query(operation_log).count()
        floors = client.get(f"/api/delivery-picks/{task_id}/measured-map/floors")
        assert floors.status_code == 200, floors.text
        assert floors.headers["cache-control"] == "private, no-store"
        payload = floors.json()
        assert payload["read_only"] is True
        assert payload["task_id"] == task_id
        assert [(floor["floor_code"], area["area_code"])
                for floor in payload["floors"] for area in floor["areas"]] == [
            ("3F", "C1")
        ]

        area = client.get(
            f"/api/delivery-picks/{task_id}/measured-map/floors/3F",
            params={"area_code": "C1"},
        )
        assert area.status_code == 200, area.text
        data = area.json()
        assert data["read_only"] is True
        assert data["map_status"] == "ready"
        group = next(row for row in data["groups"] if row["location_id"] == location_id)
        assert group["geometry"] == {
            "left_pct": 22.0,
            "top_pct": 31.0,
            "width_pct": 9.0,
            "height_pct": 8.0,
            "z_index": 0,
        }
        assert group["lines"][0]["product_code"] == "PICK-001"
        assert group["lines"][0]["pick_quantity"] == 100
        assert group["updated_at"]
        assert client.get(
            f"/api/delivery-picks/{task_id}/measured-map/floors/1F",
            params={"area_code": "FG"},
        ).status_code == 404
        with factory() as db:
            assert db.query(operation_log).count() == before_logs


def test_measured_map_denies_another_picker_and_never_expands_task_scope(
    pick_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.security import hash_password
    from app.models.user import User

    app, factory, ids, _operation_log = pick_app
    _add_measured_pick_location(pick_app, monkeypatch)
    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])
        with factory() as db:
            db.add(
                User(
                    username="picker-other",
                    password_hash=hash_password("RolePass123!"),
                    role="delivery_picker",
                    real_name="其他拿货员",
                    must_change_password=False,
                )
            )
            db.commit()
        _login(client, "picker-other")
        hidden = client.get(
            f"/api/delivery-picks/{task['id']}/measured-map/floors"
        )
        assert hidden.status_code == 404
        assert "F3-C1-08" not in hidden.text


def test_task_location_without_measured_floor_fails_closed_to_text(
    pick_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _factory, ids, _operation_log = pick_app
    _add_measured_pick_location(
        pick_app,
        monkeypatch,
        floor=2,
        area_code="B2",
        location_code="F2-B2-08",
    )
    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])
        area = client.get(
            f"/api/delivery-picks/{task['id']}/measured-map/floors/2F",
            params={"area_code": "B2"},
        )
        assert area.status_code == 200, area.text
        payload = area.json()
        assert payload["map_status"] == "unmeasured"
        assert payload["map_status_text"] == "未建立实测地图"
        assert "不会生成假坐标" in payload["guidance"]
        assert payload["bounds_mm"] is None
        assert payload["features"] == []
        assert payload["groups"][0]["geometry"] is None
        assert payload["groups"][0]["location_code"] == "F2-B2-08"


def test_mobile_warehouse_keeps_measured_map_explicit_and_pick_page_has_no_fake_grid() -> None:
    set_page = MOBILE_HTML[
        MOBILE_HTML.index("function setPage(page") : MOBILE_HTML.index(
            "function showStatus", MOBILE_HTML.index("function setPage(page")
        )
    ]
    assert "openWarehouseMap();" not in set_page
    assert 'id="warehouseMapButton"' in MOBILE_HTML
    assert "浏览实测仓库地图（可选）" in MOBILE_HTML
    assert "返回仓库搜索" in MOBILE_HTML

    assert "/measured-map/floors" in PICK_HTML
    assert "地图定位此位置" in PICK_HTML
    assert "未建立实测地图" in PICK_HTML
    assert "系统不会生成假坐标或编号格子" in PICK_HTML
    assert "background-image:linear-gradient" not in PICK_HTML
    assert "background-size:10% 10%" not in PICK_HTML
    assert "task-map-features" in PICK_HTML
    assert "feature.points" in PICK_HTML
    assert "最近更新" in PICK_HTML
    assert "不会自动发货" in PICK_HTML


def test_task_map_latest_area_request_wins_and_old_area_cannot_overwrite(
    tmp_path: Path,
) -> None:
    source = _between(
        _script(PICK_HTML),
        "function currentMapFloor()",
        "function renderPickComponents(item)",
    )
    harness = f"""
class FakeAbortController {{ constructor(){{this.signal={{aborted:false}}}} abort(){{this.signal.aborted=true}} }}
global.AbortController=FakeAbortController;
const nodes={{mapFloorTabs:{{innerHTML:""}},mapAreaTabs:{{innerHTML:""}},mapGuidance:{{textContent:""}},taskMap:{{innerHTML:""}},mapLegend:{{innerHTML:""}},mapTitle:{{textContent:""}},mapOverlay:{{hidden:true}}}};
global.document={{getElementById:id=>nodes[id]}};
global.window={{scrollY:60,scrollTo(){{}}}};
global.requestAnimationFrame=callback=>callback();
const escapeHtml=value=>String(value??"");
let taskScrollY=0,activeMapGroupKey="",activeMapFloor="",activeMapArea="";
let mapFloors=[],mapData=null,mapGeneration=0,mapController=null;
let task={{id:91,location_groups:[{{key:"g2",warehouse_floor:2,area_code:"B2"}},{{key:"g3",warehouse_floor:3,area_code:"C1"}}]}};
function deferred(){{let resolve;const promise=new Promise(done=>resolve=done);return {{promise,resolve}}}}
const first=deferred(),second=deferred();let areaCalls=0;
async function request(url){{
  if(url.endsWith("/measured-map/floors"))return {{floors:[{{floor_code:"2F",floor_name:"二楼",floor_number:2,areas:[{{area_code:"B2",area_name:"B2"}}]}},{{floor_code:"3F",floor_name:"三楼",floor_number:3,areas:[{{area_code:"C1",area_name:"C1"}}]}}]}};
  areaCalls+=1;return areaCalls===1?first.promise:second.promise;
}}
{source}
(async()=>{{
  const opening=openTaskMap("g2");
  while(areaCalls<1)await Promise.resolve();
  const switching=showMapFloor("3F");
  second.resolve({{floor_code:"3F",floor_name:"三楼",area_code:"C1",area_name:"C1",map_status:"ready",guidance:"new",bounds_mm:null,features:[],groups:[{{key:"g3",location_code:"C1-L02",recommended_sequence:2,total_pick_quantity:8,geometry:{{left_pct:30,top_pct:30,width_pct:8,height_pct:8}},lines:[]}}]}});
  await switching;
  first.resolve({{floor_code:"2F",floor_name:"二楼",area_code:"B2",area_name:"B2",map_status:"ready",guidance:"old",bounds_mm:null,features:[],groups:[{{key:"g2",location_code:"B2-L01",recommended_sequence:1,total_pick_quantity:9,geometry:{{left_pct:10,top_pct:10,width_pct:8,height_pct:8}},lines:[]}}]}});
  await opening;
  const visibleKeys=(mapData.groups||[]).map(group=>group.key);
  if(mapData.floor_code!=="3F"||!visibleKeys.includes("g3")||visibleKeys.includes("g2"))throw new Error("old slow map request overwrote the selected floor");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(tmp_path, "p1-45a-map-latest-wins.js", harness)


def test_p1_45a_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for inline JavaScript validation"
    for name, source in (
        ("mobile-erp.js", _script(MOBILE_HTML)),
        ("mobile-delivery-pick.js", _script(PICK_HTML)),
    ):
        target = tmp_path / name
        target.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, result.stderr

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_n036_delivery_pick import _create_task, _login, pick_app


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_delivery_pick.html").read_text(encoding="utf-8")


def _script() -> str:
    match = re.search(r"<script>(.*?)</script>", MOBILE, re.DOTALL)
    assert match is not None
    return match.group(1)


def _between(start: str, end: str) -> str:
    source = _script()
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def _run_node(tmp_path: Path, name: str, source: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the mobile pre-delivery contract"
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


def test_mobile_task_list_reads_twenty_then_explicitly_loads_more(tmp_path: Path) -> None:
    source = _between("function renderTaskChooser()", "async function selectTask(id)")
    harness = f"""
class FakeAbortController {{ constructor(){{this.signal={{aborted:false}}}} abort(){{this.signal.aborted=true}} }}
global.AbortController=FakeAbortController;
const nodes={{taskChooser:{{hidden:false,innerHTML:""}},taskMeta:{{textContent:""}},taskDetail:{{hidden:true}},error:{{textContent:""}},loadMoreTasksButton:{{addEventListener(){{}}}}}};
global.document={{getElementById:id=>nodes[id]||null}};
let listGeneration=0,listController=null,detailGeneration=0,detailController=null;
const listPageSize=20;let listPage=0,listTotal=0,listRows=[],listHasMore=false,listLoading=false,listAsOf="";
const calls=[];
function rows(page){{return Array.from({{length:20}},(_,index)=>({{id:(page-1)*20+index+1,customer_name:"测试客户",delivery_number:`D-${{page}}-${{index}}`,status:"pushed",assignment_required:false}}))}}
async function request(url){{calls.push(url);const page=Number(new URL(`http://erp${{url}}`).searchParams.get("page"));return {{items:rows(page),total:41,page,as_of:"2026-08-12T09:00:00+08:00"}}}}
const escapeHtml=value=>String(value??"");
const taskStatusLabel=()=>"待拿货";
function handleAuthError(){{return false}}
function toast(){{}}
{source}
(async()=>{{
  if(!await loadTaskList())throw new Error("first page failed");
  if(calls.length!==1||!calls[0].includes("page=1")||!calls[0].includes("page_size=20"))throw new Error("first paint must request only page 1 with 20 rows");
  if(listRows.length!==20||!listHasMore)throw new Error("first page state is wrong");
  if(!await loadTaskList({{append:true}}))throw new Error("load more failed");
  if(calls.length!==2||!calls[1].includes("page=2"))throw new Error("load more must request only page 2");
  if(listRows.length!==40||listPage!==2||!listHasMore)throw new Error("loaded rows were not retained");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(tmp_path, "p1-21f4-task-pagination.js", harness)


def test_task_map_shows_only_involved_floors_and_preserves_task_scroll(
    tmp_path: Path,
) -> None:
    source = _between("function currentMapFloor()", "function renderPickComponents(item)")
    harness = f"""
class FakeAbortController {{ constructor(){{this.signal={{aborted:false}}}} abort(){{this.signal.aborted=true}} }}
global.AbortController=FakeAbortController;
const nodes={{mapFloorTabs:{{innerHTML:""}},mapAreaTabs:{{innerHTML:""}},mapGuidance:{{textContent:""}},taskMap:{{innerHTML:""}},mapLegend:{{innerHTML:""}},mapTitle:{{textContent:""}},mapOverlay:{{hidden:true}}}};
global.document={{getElementById:id=>nodes[id]}};
global.window={{scrollY:387,scrollTo(){{}}}};
global.requestAnimationFrame=callback=>callback();
const escapeHtml=value=>String(value??"");
function toast(message){{throw new Error(message)}}
let taskScrollY=0,activeMapGroupKey="",activeMapFloor="",activeMapArea="";
let mapFloors=[],mapData=null,mapGeneration=0,mapController=null;
let task={{id:91,location_groups:[
  {{key:"floor-2",warehouse_floor:2,area_code:"B2",location_code:"B2-L01",label:"二楼 B2区·A架·1层·1格",recommended_sequence:1,total_pick_quantity:12,map_status:"mapped",map_point:{{left_pct:10,top_pct:20,width_pct:8,height_pct:7,z_index:1}}}},
  {{key:"floor-3",warehouse_floor:3,area_code:"C1",location_code:"C1-L02",label:"三楼 C1区·B架·1层·2格",recommended_sequence:2,total_pick_quantity:8,map_status:"mapped",map_point:{{left_pct:30,top_pct:40,width_pct:8,height_pct:7,z_index:1}}}},
  {{key:"unrelated",warehouse_floor:1,location_code:"A1-L99",map_status:"text_only",map_point:null}}
]}};
const calls=[];
async function request(url){{
  calls.push(url);
  if(url.endsWith("/measured-map/floors"))return {{floors:[{{floor_code:"2F",floor_name:"二楼",floor_number:2,areas:[{{area_code:"B2",area_name:"B2区",map_status:"ready"}}]}},{{floor_code:"3F",floor_name:"三楼",floor_number:3,areas:[{{area_code:"C1",area_name:"C1区",map_status:"ready"}}]}}]}};
  const is3=url.includes("/3F?");
  return {{floor_code:is3?"3F":"2F",floor_name:is3?"三楼":"二楼",area_code:is3?"C1":"B2",area_name:is3?"C1区":"B2区",map_status:"ready",guidance:"实测地图",bounds_mm:{{min_x:0,min_y:0,max_x:100,max_y:100}},features:[],groups:[is3?{{key:"floor-3",location_code:"C1-L02",label:"三楼 C1区·B架·1层·2格",recommended_sequence:2,total_pick_quantity:8,geometry:{{left_pct:30,top_pct:40,width_pct:8,height_pct:7,z_index:1}},lines:[]}}:{{key:"floor-2",location_code:"B2-L01",label:"二楼 B2区·A架·1层·1格",recommended_sequence:1,total_pick_quantity:12,geometry:{{left_pct:10,top_pct:20,width_pct:8,height_pct:7,z_index:1}},lines:[]}}]}};
}}
{source}
(async()=>{{
await openTaskMap("floor-2");
if(taskScrollY!==387||nodes.mapOverlay.hidden)throw new Error("map open did not preserve task state");
if(!calls[0].includes("/api/delivery-picks/91/measured-map/floors"))throw new Error("task map did not use the measured task endpoint");
if(!nodes.mapFloorTabs.innerHTML.includes("二楼")||!nodes.mapFloorTabs.innerHTML.includes("三楼")||nodes.mapFloorTabs.innerHTML.includes("一楼"))throw new Error("floor tabs exposed the wrong floors");
if(!nodes.taskMap.innerHTML.includes("二楼 B2区·A架·1层·1格")||nodes.taskMap.innerHTML.includes("B2-L01")||nodes.taskMap.innerHTML.includes("三楼 C1区·B架·1层·2格"))throw new Error("map must render only the active involved floor with the readable address");
await showMapFloor("3F");
if(!nodes.taskMap.innerHTML.includes("三楼 C1区·B架·1层·2格")||nodes.taskMap.innerHTML.includes("C1-L02")||nodes.taskMap.innerHTML.includes("二楼 B2区·A架·1层·1格"))throw new Error("floor switch leaked another floor or an internal code");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(tmp_path, "p1-21f4-task-map.js", harness)


def test_pick_plan_maps_published_location_on_a_non_third_floor(pick_app) -> None:
    from app.models.order import OrderItem
    from app.models.user import User
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryReservation,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import manual_finished_in

    app, factory, ids, _operation_log = pick_app
    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        order_item = db.get(OrderItem, ids["order_items"][0])
        location = WarehouseLocation(
            location_code="F2-B2-01",
            location_name="二楼B2区01",
            warehouse_type="finished",
            warehouse_floor=2,
            area_code="B2",
            sort_order=5,
            placement_status="placed",
        )
        db.add(location)
        db.flush()
        db.add(
            Floor3LocationLayout(
                location_id=location.id,
                left_pct=Decimal("18"),
                top_pct=Decimal("24"),
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
            expected_layout_version=1,
            quantity=100,
            stock_date=date(2026, 8, 12),
            source_type="manual",
            remarks=None,
            operator_id=admin.id,
            idempotency_key="p1-21f4-floor2-lot",
        )
        lot.quantity_available = 0
        lot.quantity_reserved = 100
        db.add(
            InventoryReservation(
                reservation_number="RSV-P1-21F4-F2",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=ids["order"],
                order_item_id=order_item.id,
                reserved_stock_quantity=100,
                credited_requirement_quantity=100,
                status="active",
                reserved_by=admin.id,
                reservation_group_key="p1-21f4-floor2",
                idempotency_key="p1-21f4-floor2-reservation",
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])
        mapped = next(
            group
            for group in task["location_groups"]
            if group["location_code"] == "F2-B2-01"
        )
        assert mapped["warehouse_floor"] == 2
        assert mapped["area_code"] == "B2"
        assert mapped["map_status"] == "mapped"
        assert mapped["map_point"]["left_pct"] == 18.0


def test_pre_delivery_labels_and_state_boundary_are_plain_and_safe() -> None:
    for label in ("拿齐", "少拿", "未找到", "可发货打印", "继续加载 20 单"):
        assert label in MOBILE
    assert "不会自动发货" in MOBILE
    assert "/api/deliveries/" not in _between(
        "async function updateItem(itemId,status)", "async function loadTask()"
    )
    assert "/measured-map/floors" in MOBILE
    assert "/api/mobile/erp/warehouse/map/floors" not in MOBILE
    assert "/warehouse.html" not in MOBILE


def test_p1_21f4_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript syntax validation"
    target = tmp_path / "p1-21f4-mobile-pre-delivery.js"
    target.write_text(_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

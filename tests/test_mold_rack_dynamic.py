from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from test_mold_tool_workflow import mold_app, _login
from app.services.mold_cells import reconcile_mold_cells, projected_cells
from app.services import mold_location as location
from app.api import warehouse as warehouse_api
from app.models.mold_tool import MoldTool, MoldToolCustomer, MoldLocationMovement
from app.models.fixed_shelf import ShelfMutation


def rack():
    return reconcile_mold_cells({"id":str(uuid4()),"name":"模具A架","mold_rack_code":"A",
        "levels":3,"bays":4,"level_cell_counts":[4,4,4],"area_code":"ZONE-3F-MOLD","area_feature_id":"mold-zone"})


def test_numbering_ids_survive_geometry_growth_and_manual_mapping():
    old=rack()
    assert [c["alias"] for c in old["mold_cells"]]==[f"A{i}" for i in range(1,13)]
    change=deepcopy(old); change["x_mm"]=1234; change["level_cell_counts"]=[5,4,4]; change.pop("mold_cells")
    reconcile_mold_cells(change,old)
    assert {c["id"]:c["alias"] for c in old["mold_cells"]}.items() <= {c["id"]:c["alias"] for c in change["mold_cells"]}.items()
    assert change["mold_cells"][4]["alias"]=="A13"
    mapped=deepcopy(old)
    mapped["mold_cells"][0]["alias"],mapped["mold_cells"][1]["alias"]="A2","A1"
    reconcile_mold_cells(mapped,old)
    assert mapped["mold_cells"][0]["id"]==old["mold_cells"][0]["id"]
    assert mapped["mold_cells"][0]["alias"]=="A2"


@pytest.mark.parametrize("mode",["rebind","duplicate","replace","cross-rack","reuse-retired"])
def test_stable_physical_cells_cannot_be_rebound_or_replaced(mode):
    old=rack(); changed=deepcopy(old)
    if mode=="rebind": changed["mold_cells"][0]["grid"]=2
    elif mode=="duplicate": changed["mold_cells"][1]["alias"]="A1"
    elif mode=="replace": changed["mold_cells"].pop(0)
    elif mode=="cross-rack": changed["mold_cells"][0]["id"]=str(uuid4())
    else:
        old["retired_mold_cells"]=[{**old["mold_cells"][0],"id":str(uuid4())}]
        changed["mold_cells"][0]["id"]=old["retired_mold_cells"][0]["id"]
    with pytest.raises(ValueError): reconcile_mold_cells(changed,old)
    with pytest.raises(ValueError): reconcile_mold_cells(deepcopy(old))


@pytest.fixture
def dynamic_floor(monkeypatch):
    physical=rack()
    floor={"floor_code":"3F","revision":"mold-test","racks":[physical],"features":[]}
    monkeypatch.setattr(location,"published_mold_floors",lambda:[floor])
    monkeypatch.setattr(location,"load_warehouse_twin_floor",lambda code:floor)
    from app.api import warehouse
    monkeypatch.setattr(warehouse,"load_warehouse_twin_floor",lambda code:floor)
    monkeypatch.setattr(warehouse,"published_mold_floors",lambda:[floor])
    monkeypatch.setattr(warehouse,"overlay_formal_area_bindings",lambda db,**kw:kw["floor_layout"])
    return floor


def test_dynamic_cell_normalization_legacy_qr_and_short_label(dynamic_floor):
    cell=projected_cells(dynamic_floor["racks"][0])[0]
    assert location.normalize_mold_location_code(cell["location_code"].lower())==cell["location_code"]
    guide=location.describe_mold_location(cell["location_code"])
    assert guide["short_label"]=="A1" and guide["floor"]=="3F"
    assert guide["cell_id"]==cell["id"]
    assert location.mold_location_feature_codes(cell["location_code"],floor_layout={**dynamic_floor,"features":[{"feature_code":"ZONE-3F-MOLD"}]})==["ZONE-3F-MOLD"]
    assert location.normalize_mold_location_code("3F-M-R01-L1-V-P12")=="3F-M-R01-L1-V-P12"
    with pytest.raises(location.MoldLocationError): location.normalize_mold_location_code("MCELL-"+str(uuid4()))
    from app.api.warehouse import _label_rack_location
    assert _label_rack_location(cell["location_code"])=="A1"


def test_batch_versions_replay_noop_changed_payload_and_atomic_rollback(mold_app,dynamic_floor):
    _,factory=mold_app
    target=projected_cells(dynamic_floor["racks"][0])[0]["location_code"]
    with factory() as db:
        db.add_all([MoldTool(mold_code=f"BATCH-{i}",mold_name=f"模具{i}",rack_location="",created_by=1) for i in range(2)])
        db.commit()
        items=[{"mold_code":f"BATCH-{i}","expected_version":1} for i in range(2)]
        kwargs=dict(items=items,target_location=target,idempotency_key="batch-atomic-001",actor_id=1,source="api",note=None)
        result,replayed=location.confirm_mold_location_batch(db,**kwargs); db.commit()
        assert not replayed and [r.mold.location_version for r in result]==[2,2]
        assert db.scalar(select(func.count(MoldLocationMovement.id)))==2
        _,replayed=location.confirm_mold_location_batch(db,**kwargs)
        assert replayed
        with pytest.raises(location.MoldLocationError): location.confirm_mold_location_batch(db,**{**kwargs,"note":"changed"})
        noop={**kwargs,"items":[{"mold_code":"BATCH-0","expected_version":2}],"idempotency_key":"batch-noop-001"}
        result,_=location.confirm_mold_location_batch(db,**noop); db.commit()
        assert result[0].no_change
        assert location.confirm_mold_location_batch(db,**noop)[1]
        # First item would move, second stale version must roll back its movement and ledger.
        second=projected_cells(dynamic_floor["racks"][0])[1]["location_code"]
        failure={**kwargs,"items":[{"mold_code":"BATCH-0","expected_version":2},{"mold_code":"BATCH-1","expected_version":1}],
                 "target_location":second,"idempotency_key":"batch-failure-001"}
        with pytest.raises(location.MoldLocationError): location.confirm_mold_location_batch(db,**failure)
        db.rollback()
        assert all(m.rack_location==target and m.location_version==2 for m in db.scalars(select(MoldTool)).all())
        assert db.scalar(select(func.count(MoldLocationMovement.id)))==2
        assert db.scalar(select(func.count(ShelfMutation.idempotency_key)))==5


def test_occupied_cells_block_removal_even_inactive_and_keep_alias_edit(dynamic_floor,mold_app):
    _,factory=mold_app
    physical=dynamic_floor["racks"][0]; target=projected_cells(physical)[0]["location_code"]
    with factory() as db:
        db.add(MoldTool(mold_code="OCCUPIED",mold_name="停用但仍有实物",rack_location=target,is_active=False))
        db.commit()
        alias=deepcopy(dynamic_floor); alias["racks"][0]["mold_cells"][0]["alias"]="A99"
        assert location.mold_rack_layout_usage_blockers(db,alias)==[]
        removed=deepcopy(dynamic_floor); removed["racks"][0]["mold_cells"].pop(0)
        assert location.mold_rack_layout_usage_blockers(db,removed)
        assert location.mold_rack_layout_usage_blockers(db,{**dynamic_floor,"racks":[]})


def test_cell_rack_counts_preview_scope_batch_denial_and_qr_labels(mold_app,dynamic_floor):
    app,factory=mold_app
    from app.models.access_control import UserCustomerScope,UserPermissionOverride
    from app.models.customer import Customer
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseFloor
    from app.api import warehouse
    target=projected_cells(dynamic_floor["racks"][0])[0]
    with factory() as db:
        denied=Customer(customer_number=9912,customer_code="DENIED",name="拒绝客户",payment_term_days=30,credit_limit=1000)
        db.add(denied); db.flush()
        user=db.scalar(select(User).where(User.username=="sales")); user.customer_access_mode="selected"
        db.add(UserCustomerScope(user_id=user.id,customer_id=1))
        db.add(UserPermissionOverride(user_id=user.id,permission_code="warehouse.operate",is_allowed=True))
        db.add(WarehouseFloor(floor_code="3F",floor_number=3,floor_name="三楼"))
        molds=[MoldTool(mold_code=name,mold_name=name,rack_location=target["location_code"],created_by=1) for name in ("ALLOW","DENIED")]
        db.add_all(molds);db.flush()
        db.add_all([MoldToolCustomer(mold_tool_id=molds[0].id,customer_id=1),MoldToolCustomer(mold_tool_id=molds[1].id,customer_id=denied.id)])
        db.commit()
    with TestClient(app) as client:
        _login(client,"sales")
        cell=client.get("/api/warehouse/molds/cell",params={"cell_id":target["id"]})
        assert cell.status_code==200,cell.text
        assert cell.json()["total"]==1 and cell.json()["items"][0]["mold_code"]=="ALLOW"
        by_rack=client.get("/api/warehouse/molds/by-map-rack",params={"floor_code":"3F","rack_id":dynamic_floor["racks"][0]["id"]})
        assert by_rack.status_code==200,by_rack.text
        assert by_rack.json()["total"]==1
        preview=client.post("/api/warehouse/molds/location-movement/preview",json={"mold_code":"ALLOW","target_location":target["location_code"]})
        assert preview.status_code==200,preview.text
        assert preview.json()["co_located_count"]==0
        denied_move=client.post("/api/warehouse/molds/location-movement/batch",json={"items":[{"mold_code":"DENIED","expected_version":1}],"target_location":target["location_code"],"idempotency_key":"scope-deny-001"})
        assert denied_move.status_code in (403,404),denied_move.text
        labels=client.get("/api/warehouse/molds/location-labels",params={"floor_code":"3F","rack_id":dynamic_floor["racks"][0]["id"]})
        assert labels.status_code==200,labels.text
        assert labels.json()["total"]==13
        assert labels.json()["labels"][1]["label"]=="模具位 A1"
        assert labels.json()["labels"][1]["qr_code"].startswith("data:image/png;base64,")
        assert "MCELL-" not in labels.json()["labels"][1]["label"]


def test_archive_restore_dynamic_floor_preserves_body_and_version(mold_app,dynamic_floor):
    _,factory=mold_app
    from app.services.mold_archive import archive_mold_tool,restore_mold_tool,MOLD_ARCHIVE_REASONS
    target=projected_cells(dynamic_floor["racks"][0])[0]["location_code"]
    with factory() as db:
        mold=MoldTool(mold_code="RESTORE-DYNAMIC",mold_name="原物理模具",rack_location=target)
        db.add(mold);db.commit();identity=mold.id
        result=archive_mold_tool(db,mold_id=identity,expected_version=1,idempotency_key="archive-dynamic-001",actor_id=1,reason="unbound")
        db.commit()
        assert result.mold.location_version==2 and not result.mold.is_active
        restored=restore_mold_tool(db,mold_id=identity,target_location=target,expected_version=2,idempotency_key="restore-dynamic-001",actor_id=1)
        db.commit()
        assert restored.mold.id==identity and restored.mold.rack_location==target and restored.mold.location_version==3
        assert restore_mold_tool(db,mold_id=identity,target_location=target,expected_version=2,idempotency_key="restore-dynamic-001",actor_id=1).replayed


def test_locator_respects_narrow_assigned_customer_scope(mold_app,dynamic_floor):
    _,factory=mold_app
    from app.api.warehouse import _twin_mold_resources
    from app.models.customer import Customer
    from app.models.user import User
    with factory() as db:
        other=Customer(customer_number=9981,customer_code="OTHER",name="范围外客户",chinese_short_name="范围外",payment_term_days=30,credit_limit=1000)
        db.add(other);db.flush()
        molds=[MoldTool(mold_code=f"LOCATE-{i}",mold_name="旧名称",label_name="共享实体",identity_status="frozen",
            rack_location=projected_cells(dynamic_floor["racks"][0])[0]["location_code"]) for i in (1,2)]
        db.add_all(molds);db.flush()
        db.add_all([MoldToolCustomer(mold_tool_id=molds[0].id,customer_id=other.id,display_order=1),
                    MoldToolCustomer(mold_tool_id=molds[0].id,customer_id=1,display_order=2),
                    MoldToolCustomer(mold_tool_id=molds[1].id,customer_id=other.id,display_order=1)])
        db.commit()
        unrestricted=db.scalar(select(User).where(User.username=="admin"))
        # Locator receives the actual narrower assigned-delivery customer scope.
        result=_twin_mold_resources(db,unrestricted,"LOCATE",{1})
        assert len(result)==1 and result[0]["mold_id"]==molds[0].id
        assert "范围外" not in result[0]["title"]
        assert result[0]["rack_id"]==dynamic_floor["racks"][0]["id"]
        assert result[0]["cell_id"]==dynamic_floor["racks"][0]["mold_cells"][0]["id"]


def test_single_noop_key_cannot_be_reused_for_another_target(mold_app,dynamic_floor):
    _,factory=mold_app
    cells=projected_cells(dynamic_floor["racks"][0]); target=cells[0]["location_code"]
    with factory() as db:
        db.add(MoldTool(mold_code="SINGLE-NOOP",mold_name="同位模具",rack_location=target));db.commit()
        kwargs=dict(mold_code="SINGLE-NOOP",target_location=target,expected_version=1,
                    idempotency_key="single-noop-001",actor_id=1,source="api",note=None)
        assert location.confirm_mold_location_move(db,**kwargs).no_change
        db.commit()
        assert location.confirm_mold_location_move(db,**kwargs).replayed
        with pytest.raises(location.MoldLocationError): location.confirm_mold_location_move(db,**{**kwargs,"target_location":cells[1]["location_code"]})
        assert db.scalar(select(func.count(MoldLocationMovement.id)))==0


def test_inactive_physical_molds_still_count_in_cells(mold_app,dynamic_floor):
    app,factory=mold_app
    from app.models.warehouse_inventory import WarehouseFloor
    cell=projected_cells(dynamic_floor["racks"][0])[0]
    with factory() as db:
        db.add(WarehouseFloor(floor_code="3F",floor_number=3,floor_name="三楼"))
        db.add(MoldTool(mold_code="INACTIVE-BODY",mold_name="停用实物",is_active=False,rack_location=cell["location_code"]))
        db.commit()
    with TestClient(app) as client:
        _login(client,"admin")
        result=client.get("/api/warehouse/molds/cell",params={"cell_id":cell["id"]})
        assert result.status_code==200,result.text
        assert result.json()["total"]==1 and not result.json()["items"][0]["is_active"]


def test_drawing_search_uses_visible_product_identity(mold_app,dynamic_floor):
    app,factory=mold_app
    from app.models.product import Product
    with factory() as db:
        body=MoldTool(mold_code="DRAWING-ID",mold_name="图号实体",rack_location="")
        db.add(body);db.flush()
        db.add(Product(customer_id=1,product_code="ITEM-DRAW",customer_material_code="ITEM-DRAW",product_name="图号款",
            box_category="die_cut",customer_drawing_number="DWG-98765",customer_drawing_display="客户图号 DWG-98765 REV-B",mold_tool_id=body.id))
        db.commit()
    with TestClient(app) as client:
        _login(client,"admin")
        result=client.get("/api/warehouse/molds",params={"q":"DWG-98765"})
        assert result.status_code==200,result.text
        assert result.json()["total"]==1 and result.json()["items"][0]["products"][0]["customer_drawing_number"]=="DWG-98765"


def test_real_area_rack_publish_batch_and_shrink_gates(tmp_path,monkeypatch):
    import json
    from app.api import warehouse as api
    from app.services import warehouse_twin_layout_editor as editor
    from test_p1_47b_warehouse_area_planning import _isolate_layout_paths,_database,_request,_confirm_area_payload,_revision,_rack_values
    published,draft=_isolate_layout_paths(tmp_path,monkeypatch)
    runtime=editor.TWIN_LAYOUT_PATH
    def live(code):
        source=runtime if runtime.exists() else published
        floors=json.loads(source.read_text(encoding="utf-8"))["floors"]
        if code not in floors:
            from app.services.warehouse_twin_layout import WarehouseTwinLayoutNotFoundError
            raise WarehouseTwinLayoutNotFoundError(code)
        return floors[code]
    monkeypatch.setattr(api,"load_warehouse_twin_floor",live)
    monkeypatch.setattr(location,"load_warehouse_twin_floor",live)
    monkeypatch.setattr(api,"list_production_projection_mappings",lambda *a,**k:[])
    engine,factory=_database(tmp_path)
    try:
        with factory() as db:
            from app.models.user import User
            admin=db.scalar(select(User))
            area=api.confirm_twin_zone_area("3F","zone-f1",_confirm_area_payload(
                revision=_revision(published),operation_key="mold-area-real-001",usage="mold",storage_layout="rack",
                area_name="三楼模具区",capacity=5),_request(),db,admin)
            current=live("3F")
            values=_rack_values(levels=2,level_cell_counts=[2,2]); values.update(mold_rack_code="A",name="模具A架")
            created=api.create_twin_layout_rack("3F",api.TwinRackLayoutCreatePayload(
                **values,expected_revision=current["revision"],operation_key="mold-rack-real-001",area_feature_id="zone-f1"),_request(),db,admin)
            rack_id=created["item"]["id"]
            api.validate_twin_layout_draft("3F",api.TwinLayoutDraftValidatePayload(expected_revision=created["revision"]),_request(),db,admin)
            applied=api.publish_twin_layout_draft("3F",api.TwinLayoutDraftPublishPayload(
                expected_published_revision=current["revision"],expected_draft_revision=created["revision"],operation_key="mold-publish-real-001"),_request(),db,admin)
            assert applied["applied"]
            physical=live("3F")["racks"][0]; cells=projected_cells(physical)
            db.add(MoldTool(mold_code="REAL-BODY",mold_name="真实闭环夹具",rack_location="",created_by=admin.id));db.commit()
            put=api.confirm_mold_location_batch_movement(api.MoldLocationBatchPayload(
                items=[{"mold_code":"REAL-BODY","expected_version":1}],target_location=cells[-1]["location_code"],idempotency_key="real-putaway-001"),_request(),db,admin)
            assert put["total"]==1
            before_runtime=runtime.read_bytes(); before_draft=draft.read_bytes() if draft.exists() else None
            shrink={**values,"level_cell_counts":[2,1]}
            with pytest.raises(api.HTTPException) as err:
                api.update_twin_layout_rack("3F",rack_id,api.TwinRackLayoutUpdatePayload(**shrink,
                    expected_revision=live("3F")["revision"],expected_version=physical["version"],operation_key="real-shrink-block-001"),_request(),db,admin)
            assert err.value.status_code==409
            assert runtime.read_bytes()==before_runtime
            assert (draft.read_bytes() if draft.exists() else None)==before_draft
            assert db.scalar(select(MoldTool.rack_location))==cells[-1]["location_code"]
            # Move the same body out before reducing an empty physical cell.
            api.confirm_mold_location_batch_movement(api.MoldLocationBatchPayload(
                items=[{"mold_code":"REAL-BODY","expected_version":2}],target_location=cells[0]["location_code"],idempotency_key="real-putaway-002"),_request(),db,admin)
            reduced=api.update_twin_layout_rack("3F",rack_id,api.TwinRackLayoutUpdatePayload(**shrink,
                expected_revision=live("3F")["revision"],expected_version=physical["version"],operation_key="real-shrink-empty-001"),_request(),db,admin)
            assert len(reduced["item"]["mold_cells"])==3
            assert reduced["item"]["retired_mold_cells"][0]["id"]==cells[-1]["id"]
            # Neither a generic goods rack in a mold area nor changing its purpose is permitted.
            with pytest.raises(api.HTTPException):
                api.create_twin_layout_rack("3F",api.TwinRackLayoutCreatePayload(
                    **{k:v for k,v in _rack_values(levels=2,level_cell_counts=[2,2]).items() if k!="mold_rack_code"},expected_revision=reduced["revision"],operation_key="goods-in-mold-deny",area_feature_id="zone-f1"),_request(),db,admin)
    finally:
        engine.dispose()


def test_failed_publish_lock_forces_pending_move_to_resolve_restored_layout(mold_app,dynamic_floor):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.api import warehouse as api
    from app.models.warehouse_inventory import WarehouseFloor
    from test_p1_47b_warehouse_area_planning import _request
    app,factory=mold_app
    cell=projected_cells(dynamic_floor["racks"][0])[-1]
    with factory() as db:
        db.add(WarehouseFloor(floor_code="3F",floor_number=3,floor_name="三楼"))
        db.add(MoldTool(mold_code="LOCK-PENDING",mold_name="等待归位",rack_location=""));db.commit()
    started=Event()
    def pending_move():
        from app.models.user import User
        with factory() as db:
            user=db.scalar(select(User).where(User.username=="admin"))
            started.set()
            return api.confirm_mold_location_movement(api.MoldLocationConfirmPayload(
                mold_code="LOCK-PENDING",target_location=cell["location_code"],expected_version=1,
                idempotency_key="pending-transient-cell"),_request(),db,user)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with api.WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
            # Simulate transient runtime cells while publication has not committed.
            future=pool.submit(pending_move)
            assert started.wait(3)
            assert not future.done()
            # Fault rollback restores published cells before releasing the same lock.
            dynamic_floor["racks"][0]["mold_cells"].pop()
        with pytest.raises(api.HTTPException) as err: future.result(timeout=10)
        assert err.value.status_code==409
    with factory() as db:
        assert db.scalar(select(MoldTool.rack_location))==""
        assert db.scalar(select(func.count(MoldLocationMovement.id)))==0
        assert db.scalar(select(func.count(ShelfMutation.idempotency_key)))==0


@pytest.mark.parametrize("before,after",[("A","R01"),("R01","A"),("A","B")])
def test_rack_identity_cannot_change_across_old_and_new_address_families(before,after):
    old=rack();old["mold_rack_code"]=before
    new=deepcopy(old);new["mold_rack_code"]=after
    with pytest.raises(ValueError): reconcile_mold_cells(new,old)


def test_retired_cell_preserves_readable_history_but_cannot_receive_molds(dynamic_floor,mold_app):
    physical=dynamic_floor["racks"][0]; prior=deepcopy(physical)
    code=projected_cells(prior)[-1]["location_code"]
    changed={**deepcopy(prior),"level_cell_counts":[4,4,3]};changed.pop("mold_cells")
    reconcile_mold_cells(changed,prior)
    dynamic_floor["racks"]=[changed]
    guide=location.describe_mold_location(code)
    assert guide["kind"]=="retired_cell" and guide["alias"]=="A12" and "已停用" in guide["prompt"]
    with pytest.raises(location.MoldLocationError): location.normalize_mold_location_code(code)
    changed["mold_cells"][0]["alias"]="A12"
    with pytest.raises(ValueError): reconcile_mold_cells(changed,changed)
    app,_=mold_app
    with TestClient(app) as client:
        _login(client,"admin")
        rejected=client.post("/api/warehouse/molds/location-movement/preview",json={"mold_code":"UNKNOWN","target_location":code})
        assert rejected.status_code==409,rejected.text


def test_batch_audit_failure_rolls_back_every_position_movement_and_ledger(mold_app,dynamic_floor,monkeypatch):
    _,factory=mold_app
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseFloor
    from app.api import warehouse as api
    from test_p1_47b_warehouse_area_planning import _request
    target=projected_cells(dynamic_floor["racks"][0])[0]["location_code"]
    with factory() as db:
        db.add(WarehouseFloor(floor_code="3F",floor_number=3,floor_name="三楼"))
        db.add_all([MoldTool(mold_code=f"AUDIT-{i}",mold_name=f"原实体{i}",rack_location="") for i in (1,2)])
        db.commit()
        user=db.scalar(select(User).where(User.username=="admin"))
        def fail_audit(*args,**kwargs): raise RuntimeError("injected audit failure")
        monkeypatch.setattr(api,"_append_mold_location_move_log",fail_audit)
        with pytest.raises(RuntimeError):
            api.confirm_mold_location_batch_movement(api.MoldLocationBatchPayload(
                items=[{"mold_code":f"AUDIT-{i}","expected_version":1} for i in (1,2)],target_location=target,
                idempotency_key="audit-failure-batch-001"),_request(),db,user)
        assert all(m.rack_location=="" and m.location_version==1 for m in db.scalars(select(MoldTool)).all())
        assert db.scalar(select(func.count(MoldLocationMovement.id)))==0
        assert db.scalar(select(func.count(ShelfMutation.idempotency_key)))==0

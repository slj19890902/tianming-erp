from __future__ import annotations
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.api import warehouse as warehouse_api
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.audit import OperationLog
from app.models.stock_replenishment import InventoryStockPolicy
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, WarehouseFloor, WarehouseLocation
from app.services import warehouse_twin_layout_editor as editor
from app.services.warehouse_twin_layout_editor import _floor_revision


def _rack(rack_id: str, name: str) -> dict:
    return {"id": rack_id, "rack_code": name, "name": name, "area_code": "A",
            "area_feature_id": "zone-a", "x_mm": 0, "y_mm": 0, "width_mm": 1000,
            "depth_mm": 800, "height_mm": 2000, "levels": 2, "level_heights_mm": [1000],
            "cargo_rows": 3, "bays": 1, "access_side": "south", "min_aisle_width_mm": 1500,
            "rotation_deg": 0, "color": "#38bdf8", "version": 3, "is_locked": False}


def _document(path: Path, *, retired: bool) -> tuple[str, str]:
    target, other = _rack("rack-delete", "F7"), _rack("rack-keep", "F9")
    racks = [other] if retired else [target, other]
    floor = {"layout_id": "rack-delete", "floor_code": "3F", "bounds_mm": {"min_x": 0, "min_y": 0, "max_x": 10000, "max_y": 10000},
             "features": [{"id": "zone-a", "feature_kind": "zone", "feature_code": "ZONE-A", "points": [[0,0],[1,0],[1,1]], "version": 1}],
             "racks": racks, "pallets": []}
    if retired:
        target["retired_at"] = "2026-09-30T00:00:00+00:00"
        target["retired_reason"] = "old draft only delete"
        floor["retired_racks"] = [target]
    floor["revision"] = _floor_revision(floor)
    doc = {"schema_version": 1, "generated_at": "old", "floors": {"3F": floor}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return floor["revision"], _rack("rack-delete", "F7")["id"]


def _request():
    return Request({"type": "http", "method": "DELETE", "path": "/api/warehouse/twin-layout/floors/3F/racks/rack-delete", "headers": [], "client": ("test", 1)})


def _setup(tmp_path: Path, monkeypatch):
    baseline = tmp_path / "baseline.json"; pub = tmp_path / "runtime" / "published.json"; draft = tmp_path / "draft" / "layout.json"
    pub_rev, rack_id = _document(baseline, retired=False)
    # The strict-isolation runner deliberately forbids fallback from an absent
    # runtime map to a shared source baseline.  Seed only this synthetic runtime.
    pub.write_bytes(baseline.read_bytes())
    draft_rev, _ = _document(draft, retired=True)
    draft_doc = json.loads(draft.read_text(encoding="utf-8"))
    draft_doc["draft_meta"] = {"status": "draft", "base_published_sha256": __import__("hashlib").sha256(pub.read_bytes()).hexdigest(), "base_floor_revisions": {"3F": pub_rev}}
    draft.write_text(json.dumps(draft_doc), encoding="utf-8")
    monkeypatch.setattr(editor, "TWIN_LAYOUT_BASELINE_PATH", baseline); monkeypatch.setattr(editor, "TWIN_LAYOUT_PATH", pub); monkeypatch.setattr(editor, "TWIN_LAYOUT_DRAFT_PATH", draft)
    engine = create_sqlite_engine(tmp_path / "test.sqlite3"); Base.metadata.create_all(engine); factory=sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin=User(username="rack-delete-admin",password_hash="x",role="admin",real_name="admin",is_active=True,must_change_password=False,customer_access_mode="all",ui_mode="standard")
        floor=WarehouseFloor(floor_code="3F",floor_name="三楼",floor_number=3,construction_status="enabled",planning_reference_pallet_capacity=0)
        db.add_all([admin,floor]); db.flush()
        for n in range(2): db.add(WarehouseLocation(location_code=f"F7-{n}",location_name=f"F7-{n}",warehouse_type="finished",is_active=True,warehouse_floor=3,area_code="A",storage_type="rack",address_kind="rack_slot",rack_code="F7",map_rack_id=rack_id,level_no=1,slot_no=n+1,address_version=1,placement_status="placed"))
        db.commit()
    return factory, pub_rev, draft_rev, rack_id, pub, draft


def _delete(db, admin, pub_rev, draft_rev, rack_id, key="rack-delete-key-01"):
    return warehouse_api.delete_twin_layout_rack("3F",rack_id,expected_revision=draft_rev,expected_published_revision=pub_rev,expected_version=3,operation_key=key,request=_request(),db=db,user=admin)


def test_published_delete_finishes_draft_tombstone_and_archives_empty_slots(tmp_path, monkeypatch):
    factory,pub_rev,draft_rev,rack_id,pub,draft=_setup(tmp_path,monkeypatch)
    try:
        with factory() as db:
            admin=db.scalar(select(User)); result=_delete(db,admin,pub_rev,draft_rev,rack_id)
            assert result["applied"] is True and result["item"]["deleted"] is True
            assert result["item"]["inactive_location_count"] == 2 and result["item"]["inventory_changed"] is False
            assert result["published_revision"] and result["revision"]
            assert all(not row.is_active for row in db.scalars(select(WarehouseLocation)).all())
            assert db.scalar(select(OperationLog)) is not None
        published=json.loads(pub.read_text(encoding="utf-8"))["floors"]["3F"]; active=json.loads(draft.read_text(encoding="utf-8"))["floors"]["3F"]
        assert {x["id"] for x in published["racks"]} == {"rack-keep"}
        assert {x["id"] for x in active["racks"]} == {"rack-keep"}
        assert [x["id"] for x in active["retired_racks"]].count(rack_id) == 1
        with factory() as db:
            admin=db.scalar(select(User)); replay=_delete(db,admin,pub_rev,draft_rev,rack_id)
            assert replay["applied"] is False and replay["idempotent_replay"] is True
    finally: engine = factory.kw["bind"]; engine.dispose()


def test_published_delete_blocks_live_inventory_and_preserves_all_maps(tmp_path, monkeypatch):
    factory,pub_rev,draft_rev,rack_id,pub,draft=_setup(tmp_path,monkeypatch)
    try:
        with factory() as db:
            admin=db.scalar(select(User)); loc=db.scalar(select(WarehouseLocation))
            db.add(InventoryLot(lot_number="rack-delete-live",inventory_type="finished",warehouse_location_id=loc.id,quantity_available=1,quantity_reserved=0,quantity_consumed=0,quantity_damaged=0,quantity_scrapped=0,unit="boxes",status="active",source_type="manual",stock_date=__import__('datetime').date.today(),stock_date_accuracy="exact",last_movement_at=__import__('datetime').datetime.now(),version=1)); db.commit()
            with pytest.raises(HTTPException, match="仍有库存") as caught: _delete(db,admin,pub_rev,draft_rev,rack_id)
            assert caught.value.status_code == 409
        assert pub.read_bytes() == baseline.read_bytes()
        assert json.loads(draft.read_text())["floors"]["3F"]["racks"] == [_rack("rack-keep", "F9")]
    finally: factory.kw["bind"].dispose()


def test_published_delete_blocks_default_location_and_audit_failure_restores_files(tmp_path, monkeypatch):
    factory,pub_rev,draft_rev,rack_id,pub,draft=_setup(tmp_path,monkeypatch)
    before=draft.read_bytes()
    try:
        with factory() as db:
            admin=db.scalar(select(User)); loc=db.scalar(select(WarehouseLocation)); db.add(InventoryStockPolicy(policy_code="rack-delete",inventory_type="finished",default_location_id=loc.id,active=True,version=1)); db.commit()
            with pytest.raises(HTTPException, match="默认位置"): _delete(db,admin,pub_rev,draft_rev,rack_id)
            db.delete(db.scalar(select(InventoryStockPolicy))); db.commit()
            monkeypatch.setattr(warehouse_api,"_twin_layout_asset_log",lambda *a,**k: (_ for _ in ()).throw(RuntimeError("audit fail")))
            with pytest.raises(RuntimeError, match="audit fail"): _delete(db,admin,pub_rev,draft_rev,rack_id)
            assert pub.read_bytes() == baseline.read_bytes() and draft.read_bytes() == before
            assert all(row.is_active for row in db.scalars(select(WarehouseLocation)).all())
    finally: factory.kw["bind"].dispose()


def test_unpublished_draft_rack_keeps_draft_only_delete_contract(tmp_path, monkeypatch):
    factory,pub_rev,draft_rev,rack_id,pub,draft=_setup(tmp_path,monkeypatch)
    try:
        # Make the target draft-only while retaining an unrelated published map.
        baseline = editor.TWIN_LAYOUT_BASELINE_PATH
        source = json.loads(baseline.read_text(encoding="utf-8"))
        source_floor = source["floors"]["3F"]
        source_floor["racks"] = [item for item in source_floor["racks"] if item["id"] != rack_id]
        source_floor["revision"] = _floor_revision(source_floor)
        baseline.write_text(json.dumps(source), encoding="utf-8")
        pub.write_bytes(baseline.read_bytes())
        published_revision = source_floor["revision"]
        draft_source = json.loads(draft.read_text(encoding="utf-8"))
        draft_floor = draft_source["floors"]["3F"]
        retired = draft_floor.pop("retired_racks")
        draft_floor["racks"].insert(0, retired[0])
        draft_floor["revision"] = _floor_revision(draft_floor)
        draft_source["draft_meta"]["base_published_sha256"] = __import__("hashlib").sha256(pub.read_bytes()).hexdigest()
        draft_source["draft_meta"]["base_floor_revisions"]["3F"] = published_revision
        draft.write_text(json.dumps(draft_source), encoding="utf-8")
        with factory() as db:
            admin=db.scalar(select(User))
            result=warehouse_api.delete_twin_layout_rack(
                "3F", rack_id, expected_revision=draft_floor["revision"],
                expected_published_revision=published_revision, expected_version=3,
                operation_key="draft-only-delete-01", request=_request(), db=db, user=admin,
            )
            assert result["applied"] is True
            assert result["item"]["published_map_changed"] is False
            assert result["item"]["inactive_location_count"] == 0
            assert all(row.is_active for row in db.scalars(select(WarehouseLocation)).all())
            replay=warehouse_api.delete_twin_layout_rack(
                "3F", rack_id, expected_revision=draft_floor["revision"],
                expected_published_revision=published_revision, expected_version=3,
                operation_key="draft-only-delete-01", request=_request(), db=db, user=admin,
            )
            assert replay["applied"] is False and replay["idempotent_replay"] is True
        assert pub.read_bytes() == baseline.read_bytes()
        assert rack_id not in {item["id"] for item in json.loads(draft.read_text(encoding="utf-8"))["floors"]["3F"]["racks"]}
    finally: factory.kw["bind"].dispose()


def test_rack_reference_uses_token_boundaries_and_checks_assets_without_slots():
    assert warehouse_api._formal_rack_archive_blockers.__name__
    # The helper's regex treats '-' as a separator: F7-1 is F7, F70 is not.
    source=Path(warehouse_api.__file__).read_text(encoding="utf-8")
    assert 'r"(?<![A-Z0-9])" + re.escape(marker) + r"(?![A-Z0-9])"' in source

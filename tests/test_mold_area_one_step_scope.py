import json
from copy import deepcopy

from sqlalchemy import select

from app.api import warehouse as api
from app.models.user import User
from app.models.warehouse_inventory import WarehouseArea, WarehouseAreaStoragePolicy, WarehouseFloor
from app.services import warehouse_twin_layout_editor as editor
from test_p1_47b_warehouse_area_planning import _isolate_layout_paths, _database, _request, _confirm_area_payload


def test_single_area_save_preserves_unready_sibling_but_full_publish_stays_strict(tmp_path, monkeypatch):
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text(encoding='utf-8'))
    floor = document['floors']['3F']
    sibling = deepcopy(floor['features'][0])
    sibling.update(id='zone-old', feature_code='ZONE-3F-OLD', erp_area_code='OLD', name='旧区域',
                   allowed_inventory_types=['finished'], storage_layout='pallet_ground',
                   points=[[9000,0],[12000,0],[12000,3000],[9000,3000]])
    floor['features'].append(sibling)
    floor['revision'] = editor._floor_revision(floor)
    published.write_text(json.dumps(document), encoding='utf-8')
    runtime = editor.TWIN_LAYOUT_PATH
    def live(code):
        return json.loads((runtime if runtime.exists() else published).read_text(encoding='utf-8'))['floors'][code]
    monkeypatch.setattr(api, 'load_warehouse_twin_floor', live)
    monkeypatch.setattr(api, 'list_production_projection_mappings', lambda *a, **k: [])
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            admin = db.scalar(select(User))
            f = db.scalar(select(WarehouseFloor))
            old = WarehouseArea(floor_id=f.id, area_code='OLD', area_name='旧区域', planned_location_count=10,
                                construction_status='enabled')
            db.add(old); db.flush()
            policy = WarehouseAreaStoragePolicy(area_id=old.id, map_feature_id='zone-old',
                       allowed_inventory_types_json='["finished"]', storage_layout='pallet_ground', status='published',
                       published_map_revision=floor['revision'], version=1)
            db.add(policy); db.commit()
            before = (old.planned_location_count, old.construction_status, policy.version, policy.published_map_revision)
            # Global publication retains the original readiness safety gate.
            editor.load_warehouse_twin_layout_draft('3F')
            assert any('OLD' in message for message in api._formal_area_publish_blockers(db, '3F'))
            result = api.confirm_twin_zone_area('3F', 'zone-f1', _confirm_area_payload(
                revision=floor['revision'], operation_key='scope-mold-area-001', usage='mold',
                storage_layout='rack', capacity=1), _request(), db, admin)
            assert result['area']['construction_status'] == 'enabled'
            assert result['formal_area_count'] == 1
            db.refresh(old); db.refresh(policy)
            assert (old.planned_location_count, old.construction_status, policy.version, policy.published_map_revision) == before
            assert any('OLD' in message for message in api._formal_area_publish_blockers(db, '3F'))
    finally:
        engine.dispose()

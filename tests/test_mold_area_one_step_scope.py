import json
from copy import deepcopy
import pytest

from sqlalchemy import select

from app.api import warehouse as api
from app.models.user import User
from app.models.warehouse_inventory import WarehouseArea, WarehouseAreaStoragePolicy, WarehouseFloor
from app.services import warehouse_twin_layout_editor as editor
from test_p1_47b_warehouse_area_planning import _isolate_layout_paths, _database, _request, _confirm_area_payload, _rack_values


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
            # Normal operator sequence: add a mold rack, save/apply just that
            # rack, leave unrelated unready areas and layout drafts untouched.
            values = _rack_values(levels=3, level_cell_counts=[3,3,3])
            values.update(mold_rack_code='B', name='模具B架')
            created = api.create_twin_layout_rack('3F', api.TwinRackLayoutCreatePayload(
                **values, expected_revision=live('3F')['revision'], operation_key='scope-rack-new-001',
                area_feature_id='zone-f1'), _request(), db, admin)
            advanced = json.loads(draft.read_text(encoding='utf-8'))
            unrelated = next(f for f in advanced['floors']['3F']['features'] if f['id']=='zone-old')
            unrelated['name'] = '其他未应用的改名'
            advanced['floors']['3F']['revision'] = editor._floor_revision(advanced['floors']['3F'])
            editor._write_document(draft, advanced)
            payload = api.TwinZoneGeometryApplyPayload(
                expected_revision=advanced['floors']['3F']['revision'], expected_published_revision=live('3F')['revision'],
                expected_version=created['item']['version'], operation_key='scope-rack-apply-001')
            before_map, before_draft = runtime.read_bytes(), draft.read_bytes()
            original_publish = api.publish_floor_area_policies
            def fail_after_map(*args, **kwargs):
                raise RuntimeError('injected failure after map publish')
            monkeypatch.setattr(api, 'publish_floor_area_policies', fail_after_map)
            with pytest.raises(RuntimeError, match='injected'):
                api.apply_twin_rack_layout('3F', created['item']['id'], payload, _request(), db, admin)
            assert runtime.read_bytes()==before_map and draft.read_bytes()==before_draft
            monkeypatch.setattr(api, 'publish_floor_area_policies', original_publish)
            applied = api.apply_twin_rack_layout('3F', created['item']['id'], payload, _request(), db, admin)
            assert applied['applied'] and applied['scope']=='rack_layout'
            kept = next(r for r in live('3F')['racks'] if r['id']==created['item']['id'])
            assert kept['mold_cells']==created['item']['mold_cells'] and len(kept['mold_cells'])==9
            assert next(f for f in live('3F')['features'] if f['id']=='zone-old')['name']=='旧区域'
            remaining=json.loads(draft.read_text(encoding='utf-8'))['floors']['3F']
            assert next(f for f in remaining['features'] if f['id']=='zone-old')['name']=='其他未应用的改名'
            db.refresh(policy)
            assert (old.planned_location_count, old.construction_status, policy.version, policy.published_map_revision)==before
            replay=api.apply_twin_rack_layout('3F', created['item']['id'], payload, _request(), db, admin)
            assert replay['idempotent_replay'] and not replay['applied']
    finally:
        engine.dispose()

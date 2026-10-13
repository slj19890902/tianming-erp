"""Real mutation API regression: all published area bindings follow the floor."""
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.api import warehouse as api
from app.models.user import User
from app.models.warehouse_inventory import WarehouseArea, WarehouseAreaStoragePolicy, WarehouseLocation
from app.services import warehouse_twin_layout_editor as editor
from tests.test_f7f8_published_rack_delete import _setup, _delete, _request


def published_case(tmp_path, monkeypatch):
    factory, _, _, rack_id, runtime, draft = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(editor, 'TWIN_LAYOUT_BACKUP_DIR', tmp_path / 'backups')
    doc = json.loads(runtime.read_text())
    floor = doc['floors']['3F']
    floor['features'][0].update(erp_area_code='A', storage_layout='rack',
        formal_area_name='测试区', allowed_inventory_types=['finished'])
    floor['features'].append(dict(id='no-go-test', feature_kind='no_go',
        subtype='door_clearance', feature_code='NO-GO-TEST',
        points=[[9000,9000],[9100,9000],[9100,9100],[9000,9100]], version=1))
    floor['revision'] = editor._floor_revision(floor)
    runtime.write_text(json.dumps(doc))
    Path(editor.TWIN_LAYOUT_BASELINE_PATH).write_bytes(runtime.read_bytes())
    draft_doc = json.loads(draft.read_text())
    draft_doc['floors']['3F']['features'] = floor['features']
    draft_doc['floors']['3F']['revision'] = editor._floor_revision(draft_doc['floors']['3F'])
    draft_doc['draft_meta'].update(base_floor_revisions={'3F':floor['revision']},
        base_published_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest())
    draft.write_text(json.dumps(draft_doc))
    with factory() as db:
        area = db.scalar(select(WarehouseArea))
        area.storage_policy = WarehouseAreaStoragePolicy(map_feature_id='zone-a', storage_layout='rack',
            status='published', published_map_revision=floor['revision'],
            allowed_inventory_types_json='["finished"]', version=1)
        db.commit()
    return factory, floor['revision'], draft_doc['floors']['3F']['revision'], rack_id, runtime, draft


def mutate(case, db, kind):
    factory, old, draft_revision, rack_id, runtime, draft = case
    actor = db.scalar(select(User))
    if kind == 'rack':
        return _delete(db, actor, old, draft_revision, rack_id)
    return api.remove_twin_no_go_features('3F', api.TwinNoGoRemovalPayload(
        expected_revision=draft_revision, expected_published_revision=old,
        feature_ids=['no-go-test'], operation_key='remove-no-go-test'), _request(), db, actor)


@pytest.mark.parametrize('kind', ['rack', 'no_go'])
def test_mutation_keeps_published_policy_current_and_replay_stable(tmp_path, monkeypatch, kind):
    case = published_case(tmp_path, monkeypatch)
    factory, old, _, _, runtime, _ = case
    try:
        with factory() as db:
            result = mutate(case, db, kind)
            assert result['applied']
            area = db.scalar(select(WarehouseArea))
            current = json.loads(runtime.read_text())['floors']['3F']['revision']
            assert current != old
            assert area.storage_policy.published_map_revision == current
            version = area.storage_policy.version
            replay = mutate(case, db, kind)
            assert replay['applied'] is False
            assert area.storage_policy.version == version
    finally:
        factory.kw['bind'].dispose()


@pytest.mark.parametrize('kind', ['rack', 'no_go'])
@pytest.mark.parametrize('failure', ['binding', 'commit'])
def test_mutation_failure_restores_policy_locations_and_both_files(tmp_path, monkeypatch, kind, failure):
    from app.services import warehouse_map_publication as publication
    case = published_case(tmp_path, monkeypatch)
    factory, old, _, _, runtime, draft = case
    before = runtime.read_bytes(), draft.read_bytes()
    def fail(*args, **kwargs):
        raise RuntimeError('injected publication failure')
    try:
        with factory() as db:
            if failure == 'binding':
                monkeypatch.setattr(publication, 'assert_current_floor_bindings', fail)
            else:
                monkeypatch.setattr(db, 'commit', fail)
            with pytest.raises(RuntimeError, match='injected publication failure'):
                mutate(case, db, kind)
            assert (runtime.read_bytes(), draft.read_bytes()) == before
            assert db.scalar(select(WarehouseArea)).storage_policy.published_map_revision == old
            assert all(loc.is_active for loc in db.scalars(select(WarehouseLocation)))
    finally:
        factory.kw['bind'].dispose()

from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from tests.test_rack_map_application import rack_case
from scripts.admin import unify_warehouse_map_bindings as service
from app.models.audit import OperationLog
from app.services.warehouse_area_activation import WarehouseAreaActivationError


def scenario(case):
    db, _, area, old, new = case
    for floor in (old, new):
        floor['features'][0].pop('erp_area_code')
        floor['features'][0].pop('allowed_inventory_types')
        floor['racks'][0]['area_code'] = 'ZONE-C'
        floor['features'][0]['feature_code'] = 'ZONE-C'
        floor['racks'][0]['mold_cells'] = [{'id':'stable-cell', 'label':'A1'}]
    published = dict(floors={'3F':new})
    draft = deepcopy(published)
    draft['floors']['3F']['retired_features'] = [{'id':'old-history'}]
    draft['draft_meta'] = dict(base_floor_revisions={'3F':'new-map'}, base_published_sha256='original', receipts=['preserved'])
    sources = {('3F','old-map'):old}
    return published, draft, sources


def test_metadata_repair_preserves_geometry_history_and_identity_and_is_idempotent(rack_case):
    db, actor, area, _, _ = rack_case
    published, draft, sources = scenario(rack_case)
    original = deepcopy((published,draft,sources))
    plan, candidate, draft_candidate = service.prepare(db,published,draft,sources)
    assert (published,draft,sources) == original
    target = candidate['floors']['3F']
    assert target['features'][0]['erp_area_code'] == area.area_code
    assert target['racks'][0]['area_code'] == area.area_code
    assert target['racks'][0]['mold_cells'] == published['floors']['3F']['racks'][0]['mold_cells']
    for collection, allowed in [('features',set(service.BINDING_KEYS)|{'version'}),('racks',{'area_code','version'})]:
        a=published['floors']['3F'][collection][0]; b=target[collection][0]
        assert {k:v for k,v in a.items() if k not in allowed} == {k:v for k,v in b.items() if k not in allowed}
    assert draft_candidate['floors']['3F']['retired_features'] == draft['floors']['3F']['retired_features']
    assert draft_candidate['draft_meta']['receipts'] == draft['draft_meta']['receipts']
    assert service.apply_bindings(db,plan,candidate,actor,'test-repair') == 1
    db.commit()
    assert service.apply_bindings(db,plan,candidate,actor,'test-repair') == 0
    assert len(list(db.scalars(select(OperationLog)))) == 1
    log = db.scalar(select(OperationLog))
    audit_changes = json.loads(log.details)['metadata_changes']
    assert next(x for x in audit_changes if x['field']=='allowed_inventory_types')['after']==['finished']
    assert len(audit_changes)==sum(len(x['changes']) for x in plan['metadata_changes'])
    altered = deepcopy(plan); altered['schema'] = 2
    with pytest.raises(WarehouseAreaActivationError): service.apply_bindings(db,altered,candidate,actor,'test-repair')


@pytest.mark.parametrize('conflict',['geometry','identity','draft','missing-source','rack-owner','duplicate'])
def test_unification_rejects_unproven_or_conflicting_metadata(rack_case, conflict):
    db, _, _, _, _ = rack_case
    published,draft,sources = scenario(rack_case)
    current=published['floors']['3F']
    if conflict=='geometry': current['racks'][0]['levels']=9
    elif conflict=='identity':
        for floor in [current,sources[('3F','old-map')]]: floor['features'][0]['formal_area_id']=999
    elif conflict=='draft': draft['floors']['3F']['racks'][0]['levels']=9
    elif conflict=='missing-source': sources.clear()
    elif conflict=='rack-owner':
        for floor in [current,sources[('3F','old-map')]]: floor['racks'][0]['area_code']='OTHER'
    elif conflict=='duplicate': current['racks'].append(deepcopy(current['racks'][0]))
    with pytest.raises(WarehouseAreaActivationError): service.prepare(db,published,draft,sources)
    assert not list(db.scalars(select(OperationLog)))


def test_unification_audit_failure_rolls_back_and_stale_policy_is_rejected(rack_case,monkeypatch):
    db,actor,area,_,_=rack_case
    plan,candidate,_=service.prepare(db,*scenario(rack_case))
    def fail(*args,**kwargs): raise RuntimeError('audit unavailable')
    monkeypatch.setattr(service,'append_audit_event',fail)
    with pytest.raises(RuntimeError): service.apply_bindings(db,plan,candidate,actor,'rollback')
    db.rollback()
    assert area.storage_policy.version==4 and area.storage_policy.published_map_revision=='old-map'
    area.storage_policy.version=5; db.commit()
    with pytest.raises(WarehouseAreaActivationError): service.apply_bindings(db,plan,candidate,actor,'stale')

from copy import deepcopy
import json

import pytest
from sqlalchemy import select, func

from app.models.audit import OperationLog
from app.models.user import User
from app.models.receipt_putaway import ReceiptStagingArea
from app.models.warehouse_inventory import WarehouseFloor, WarehouseArea, WarehouseAreaStoragePolicy, WarehouseLocation, Floor3LocationLayout
from app.services import receipt_staging_map as service
from app.services.warehouse_area_activation import WarehouseAreaActivationError
from test_warehouse_inventory_foundation import db


@pytest.fixture
def staging(db):
    actor=User(username='map-admin',password_hash='isolated',role='admin',real_name='地图管理员')
    floor=WarehouseFloor(floor_code='1F',floor_name='一楼',floor_number=1,construction_status='enabled')
    db.add_all([actor,floor]); db.flush()
    features=[]
    for n in (1,2):
        area=WarehouseArea(floor_id=floor.id,area_code=f'RAW-00{n}',area_name=f'待入库{n}',construction_status='enabled')
        db.add(area);db.flush()
        db.add(ReceiptStagingArea(area_id=area.id))
        db.add(WarehouseAreaStoragePolicy(area_id=area.id,map_feature_id=f'zone{n}',allowed_inventory_types_json='["finished","semi_finished","raw_material"]',storage_layout='pallet_ground',status='published',published_map_revision='old',version=1))
        loc=WarehouseLocation(location_code=f'F1-R{n}',location_name='待入库',warehouse_type='shared',warehouse_floor=1,area_code=area.area_code,address_area_id=area.id,address_kind='functional',storage_type='temporary_aisle',placement_status='placed',source_version='CURRENT_MAP',is_active=True)
        db.add(loc);db.flush()
        db.add(Floor3LocationLayout(location_id=loc.id,left_pct=0,top_pct=0,width_pct=10,height_pct=10,layout_kind='logical_anchor',source_type='manual',version=1))
        features.append({'id':f'zone{n}','erp_area_code':area.area_code,'feature_kind':'zone','storage_layout':'pallet_ground','allowed_inventory_types':['finished'],'points':[[n*100,0],[n*100+90,0],[n*100+90,90],[n*100,90]]})
    db.commit()
    source={'floor_code':'1F','revision':'old','bounds_mm':{'min_x':0,'max_x':500},'features':features}
    target={**deepcopy(source),'revision':'new'}
    return db,actor,source,target


def test_carry_unchanged_functional_staging_preserves_usage_location_and_replay(staging):
    db,actor,source,target=staging
    before=[(x.id,x.location_code,x.address_version,x.floor3_layout.version) for x in db.scalars(select(WarehouseLocation))]
    plan=service.preview_staging_transition(db,source_floor=source,target_floor=target)
    result=service.apply_staging_transition(db,source_floor=source,target_floor=target,expected_plan=plan,actor=actor,operation_key='carry')
    db.commit()
    assert len(result['areas'])==2
    assert [(x.id,x.location_code,x.address_version,x.floor3_layout.version) for x in db.scalars(select(WarehouseLocation))]==before
    for policy in db.scalars(select(WarehouseAreaStoragePolicy)):
        assert policy.published_map_revision=='new' and policy.version==2
        assert json.loads(policy.allowed_inventory_types_json)==['finished','semi_finished','raw_material']
    assert service.apply_staging_transition(db,source_floor=source,target_floor=target,expected_plan=plan,actor=actor,operation_key='carry')==result
    assert db.scalar(select(func.count()).select_from(OperationLog))==1
    with pytest.raises(WarehouseAreaActivationError,match='其他内容'):
        service.apply_staging_transition(db,source_floor=source,target_floor=target,expected_plan={**plan,'source_revision':'forged'},actor=actor,operation_key='carry')


def test_changed_nearby_map_and_stale_location_are_rejected(staging):
    db,actor,source,target=staging
    changed=deepcopy(target)
    changed['features'].append({'id':'new-machine','points':[[100,0],[110,0],[110,10],[100,10]],'feature_kind':'equipment'})
    with pytest.raises(WarehouseAreaActivationError,match='邻近地图'):
        service.preview_staging_transition(db,source_floor=source,target_floor=changed)
    plan=service.preview_staging_transition(db,source_floor=source,target_floor=target)
    db.scalar(select(Floor3LocationLayout)).version+=1
    db.commit()
    with pytest.raises(WarehouseAreaActivationError,match='已变化'):
        service.apply_staging_transition(db,source_floor=source,target_floor=target,expected_plan=plan,actor=actor,operation_key='stale')
    db.rollback()
    assert all(p.version==1 for p in db.scalars(select(WarehouseAreaStoragePolicy)))


def test_audit_failure_rolls_back_all_policy_changes(staging,monkeypatch):
    db,actor,source,target=staging
    plan=service.preview_staging_transition(db,source_floor=source,target_floor=target)
    def fail(*args,**kwargs): raise RuntimeError('audit unavailable')
    monkeypatch.setattr(service,'append_audit_event',fail)
    with pytest.raises(RuntimeError,match='audit unavailable'):
        service.apply_staging_transition(db,source_floor=source,target_floor=target,expected_plan=plan,actor=actor,operation_key='fail')
    db.rollback()
    assert all(p.version==1 and p.published_map_revision=='old' for p in db.scalars(select(WarehouseAreaStoragePolicy)))
    assert db.scalar(select(func.count()).select_from(OperationLog))==0


def test_map_publication_includes_functional_staging_without_ground_plans(staging):
    db,actor,source,target=staging
    from app.services.warehouse_ground_map_application import record_map_applications
    record_map_applications(db,floor_layout=target,previous_floor_layout=source,actor=actor,operation_key='map-publish')
    assert all(p.published_map_revision=='new' for p in db.scalars(select(WarehouseAreaStoragePolicy)))

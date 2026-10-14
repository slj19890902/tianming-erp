"""Finite recovery of the 2026-10-13 first-floor publication bindings.

The geometry anchor is the real 063898 document, NOT the unavailable 12069
publication. Original publication/area/deletion audits supply a separate binding
proof. No source floor is rewritten or relabelled. No HTTP route is provided.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

from sqlalchemy import inspect, select
from sqlalchemy.orm import selectinload

from app.models import Base
from app.models.audit import OperationLog
from app.models.finance import FinanceIdempotencyRecord
from app.models.receipt_putaway import ReceiptStagingArea
from app.models.user import User
from app.models.warehouse_inventory import (
    WarehouseArea, WarehouseAreaStoragePolicy, WarehouseFloor,
    WarehouseGroundLayoutPlan, WarehouseLocation,
)
from app.services import warehouse_twin_layout_editor as editor
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.location_candidates import operational_location_issue
from app.services.receipt_staging_map import _nearby
from app.services.warehouse_area_activation import (
    WarehouseAreaActivationError, _advance_policy_version,
)
from app.services.warehouse_floor_claim import claim_warehouse_floor_projection
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError, validate_capacity_layout_slots_for_zone,
)
from app.services.warehouse_ground_map_application import (
    location_signature, record_map_applications,
)
from app.services.warehouse_ground_slots import published_ground_plan
from app.services.warehouse_map_publication import assert_current_floor_bindings

ACTION = 'warehouse.map.audit_recovery_20261014'
SOURCE_SHA = '6c6875b3af262a5a24ee5992adbf7d30ac1bd4db5dd5cb652558a285e0ae7e27'
SOURCE_REVISION = '063898adb53eb21e'
BINDING_SHA = 'e79feaba1ab0443a1ef7a337a10e7d3615a8536d21298a0af20161a390da521b'
BINDING_REVISION = '12069ae4137257d6'
CURRENT_SHA = 'cef9dfd43e8ef2b4d94abdd43c50e631d548e5f02a447041c996709832e1ab1b'
CURRENT_REVISION = 'e919e36e2f3ec786'
DRAFT_SHA = '08c87da8d4a6797c48e4874f9248416f59b930df906a9f3a9510729a23ece38f'
HISTORICAL_AUDIT_SHA = 'b6e753909d0af4cdee1c64fa518415124c0d8449e1f267329cd648df1db59243'
# (policy ID, area ID, original version). No caller-controlled trust pins.
POLICY_PINS = ((4,29,45),(5,30,47),(6,31,34),(7,32,32),(8,33,30),
    (9,34,29),(17,42,37),(18,43,27),(19,44,28),(20,45,23),(21,46,22),
    (22,47,21),(25,49,21),(26,50,21),(27,51,21),(28,52,21),(29,53,21),
    (30,54,21),(31,55,21),(141,144,17),(142,145,18),(143,146,18),(144,147,14))
STAGING_AREA_IDS = (29,30,42,44)
# Exact historical functional positions: ID, address version, full location_signature.
STAGING_LOCATION_PINS = {
    29: (
        (415, 2, 'd61d3bb11350783980e4fc3f75d30adb8fecc16d10b91d14893727e144b3892d'),
        (416, 2, '07b60a260b936a6517a738532e6cf46e49436d5f1faff607d3bc1231e1390c6a'),
        (417, 2, 'b68bd9c12d7b971cf24dd74fa97f2529c89fe9e168656c1d63e9db5c71f435f3'),
        (418, 2, '9d021ca7f05a136361dee45c6aa48a7e0e53086bad2336d0de9080236ce9b32c'),
        (419, 2, '39d299cfe0056ae33fb3103051d7890c20b6740b43e6e0d8b6a45bdaa9a803d6'),
        (420, 2, '5927d1cbe96db6bed1466de982e0ce655cee3114b571cc091494ec7c10bbd6a8'),
        (421, 2, '07aed6317d3635d97c90c3dfe246efc3fb3fbd0114dbbec1b32c59c51f94f9b7'),
        (422, 2, 'c6b3b5c41c72af3a3839833f927eadaa55c3861e75debb90a5120865a16b3314'),
    ),
    30: (
        (402, 2, '974f378091d2b4da7ce0efcfd43a5598be2151ca760b1b985cc41dd7986bdaeb'),
        (403, 2, '01cdd9d8eff9ff3c8d2959ca9b65973fb0a08850288695040f5d6eb671775f83'),
        (404, 2, '8bd7dce5fde5b8fff9777e2c9716bb281eae2122fd9becc57b380a0c9142998f'),
        (405, 2, 'd12dad1cabc1a8a2c4a432c2b6af102e98f2456d04b44d0ad5fe2bade3803e01'),
        (406, 2, '3ecc16b0799fb714d82b69af72572dfe426b2c15e6ae3aa944b5022a51c5f03e'),
        (407, 2, '990ac1b72a0035726531cf4b2661adb16d87867042378baafc2928c5f6389157'),
        (408, 2, 'a64584a60fc9cd2b8e4cc7e144b2afb2da7d0cdd7fe52dc0da022d895e8e885e'),
        (409, 2, '18d521833a2c0ceab577f9669ca39a8cdfa7713adc68f0424af3fffc8aefd182'),
        (410, 2, '9d0054cb4c2da9c32670adc09b8a8df8077c2904322ff3f319614d176a910707'),
        (411, 2, 'b59a6e8cbc7cd86c327806da4490b17fcb3703ac4b6d127917e33dadae7d21d0'),
        (412, 2, '220aad4c40b70c20d4d2adb03a0ece1c780b3e90bb5502bb8b4ed9071d83f00e'),
        (413, 2, '63dfd0dd1ad48ac599f1a8f58cef4280ef12d9bb9a53689828b75283ee3cb82b'),
    ),
    42: (
        (590, 2, '26c220032627fe4b65a2224dfc2f1c3274a6e30fbdcd1e142cc9f1b334651e62'),
        (591, 2, '70da994374527315d64461c915dbd9dc00a4eb65c7b315053c2d0c6ca23b1660'),
        (592, 2, 'c864c48b56433a1a564e42566f1c85ec72c5ae7c72114dfd11eadedc7624619d'),
        (593, 2, '336da2773a22618d6b2d711f704800666a4d933033c2b89719c11e35ed093621'),
        (594, 2, 'dc82ab40d1120f57f485538b7117d53ea01d52f4708f086fd27cce089338ba8e'),
        (595, 2, '25aa9eb942a65236d55a1dc7ba6a24886feca8f41cc30e1a2216a66d00355336'),
        (596, 2, '437efc64828725d42425a264bcfdce5430858dc6096725c2f3d3e536586be9bc'),
        (597, 2, '92b6bd7cee5526083e4641e740c73108f50216f19eccf2dfad2ee0acf7d92a1f'),
    ),
    44: (
        (604, 2, 'b97b96f6007e043547ec38e2d3212996ba0777136566d4de9770de29582fa49b'),
        (605, 2, 'dc66a8d48bf6c8110c0bf58b9295d8c58b48f9946aeddcd06b658635ef43099a'),
    ),
}
GROUND_PLAN_ID = 1
RETIRED_RACK_IDS = ('83b533e9-e643-43ee-a68c-b151e951808e',
                    'd00e4e04-8043-4a98-bc4d-f018406d5aff')
AUDIT_COLUMNS = ('id','action','resource','entity_type','entity_id','module_code',
                 'action_code','result','user_id','schema_version','details')


def _reject(message):
    raise WarehouseAreaActivationError('有限地图恢复：' + message, status_code=409)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      default=str, allow_nan=False)


def _hash(value):
    return sha256(_canonical(value).encode()).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject('重复 JSON 字段')
        result[key] = value
    return result


def _read(raw, expected):
    if not isinstance(raw, bytes) or len(raw) > 2 * 1024 * 1024 or sha256(raw).hexdigest() != expected:
        _reject('原始文件 SHA 不匹配')
    try:
        return json.loads(raw, object_pairs_hook=_unique,
            parse_constant=lambda _: _reject('非有限 JSON 数值'))
    except (ValueError, TypeError) as error:
        _reject('文件 JSON 无效：' + str(error))


def _index(rows):
    result = {}
    for row in rows:
        if not row.get('id') or row['id'] in result:
            _reject('缺失或重复对象 ID')
        result[row['id']] = row
    return result


def _actor(db, actor):
    if not actor or not actor.id:
        _reject('缺少操作人')
    saved = db.scalar(select(User).where(User.id == actor.id)
                      .execution_options(populate_existing=True))
    if not saved or not saved.is_active or saved.role != 'admin' or saved.customer_access_mode != 'all':
        _reject('需要有效的全客户管理员')
    return saved


def _clean(db):
    if db.new or db.dirty or db.deleted:
        _reject('请使用没有待写业务修改的事务')


def _documents(source_bytes):
    # Runtime is mandatory: no fallback to a bundled baseline, and no file write.
    source = _read(source_bytes, SOURCE_SHA)
    current = _read(editor.TWIN_LAYOUT_PATH.read_bytes(), CURRENT_SHA)
    _read(editor.TWIN_LAYOUT_DRAFT_PATH.read_bytes(), DRAFT_SHA)
    if source.get('schema_version') != 1 or current.get('schema_version') != 1 or set(source) != set(current):
        _reject('地图文档结构不一致')
    if set(source['floors']) != set(current['floors']):
        _reject('楼层集合变化')
    for key in source:
        if key not in {'generated_at', 'floors'} and source[key] != current[key]:
            _reject('文档元信息变化')
    for code in source['floors']:
        if code != '1F' and source['floors'][code] != current['floors'][code]:
            _reject('其他楼层变化')
    old, new = source['floors']['1F'], current['floors']['1F']
    for floor, revision in ((old, SOURCE_REVISION), (new, CURRENT_REVISION)):
        if (floor.get('floor_code') != '1F' or floor.get('revision') != revision
                or _hash({k:v for k,v in floor.items() if k != 'revision'})[:16] != revision):
            _reject('楼层版本不能由实际内容反算')
    variable = {'racks','retired_racks','revision','layout_edited_at','layout_edit_receipts'}
    if {k:v for k,v in old.items() if k not in variable} != {k:v for k,v in new.items() if k not in variable}:
        _reject('区域、边界或其他受保护几何变化')
    _index(old.get('features', [])); _index(new.get('features', []))
    _index(old.get('pallets', [])); _index(new.get('pallets', []))
    return old, new


def _audits(db):
    rows = []
    for row in db.scalars(select(OperationLog).where(OperationLog.id.between(41374,41428))
                           .order_by(OperationLog.id).execution_options(populate_existing=True)):
        item = {key:getattr(row,key) for key in AUDIT_COLUMNS}
        item['details'] = json.loads(item['details'] or '{}')
        rows.append(item)
    if [r['id'] for r in rows] != list(range(41374,41429)) or _hash(rows) != HISTORICAL_AUDIT_SHA:
        _reject('55 条原始发布与退役审计不完整或已变化')
    return {row['id']:row for row in rows}


def _geometry_and_audit_proof(old, new, audits, locations, molds):
    publication = audits[41400]['details']
    applied = audits[41401]['details']['result']
    original = audits[41374]['details']
    if (original['published_revision'] != SOURCE_REVISION or original['published_sha256'] != SOURCE_SHA
            or publication['backup_sha256'] != SOURCE_SHA
            or any(p['published_revision'] != BINDING_REVISION or p['published_sha256'] != BINDING_SHA
                   or p['inventory_changed'] is not False or p['mold_location_changed'] is not False
                   for p in (publication, applied))
            or applied['rack_id'] != RETIRED_RACK_IDS[1] or applied['scope'] != 'rack_layout'):
        _reject('发布审计未连接真实几何源与原绑定版本')
    for key in ('location_coordinate_adjustments','rack_cell_created_location_ids',
                'rack_cell_enabled_location_ids','rack_cell_disabled_location_ids',
                'rack_cell_updated_location_ids','rack_cell_bound_legacy_location_ids'):
        if publication.get(key) != []:
            _reject('原发布存在额外库位变化')
    old_racks, active = _index(old.get('racks',[])), _index(new.get('racks',[]))
    old_retired, retired = _index(old.get('retired_racks',[])), _index(new.get('retired_racks',[]))
    ids = set(RETIRED_RACK_IDS)
    if (set(old_racks)-set(active) != ids or set(active)-set(old_racks)
            or set(retired)-set(old_retired) != ids or set(old_retired)-set(retired)):
        _reject('当前图不是且仅是两架退役')
    if any(rack != old_racks[key] for key,rack in active.items()) or any(rack != retired[key] for key,rack in old_retired.items()):
        _reject('其他货架或旧退役记录变化')
    afterimage = audits[41376]['details']['rack']
    prior_rack = old_racks[RETIRED_RACK_IDS[1]]
    if ({k:v for k,v in afterimage.items() if k!='version'} != {k:v for k,v in prior_rack.items() if k!='version'}
            or afterimage.get('version') != prior_rack.get('version',0)+1):
        _reject('41376 不是仅版本增加的完整 afterimage')
    receipts = []
    for rack_id, audit_id in zip(RETIRED_RACK_IDS, (41406,41428)):
        rack = afterimage if rack_id == RETIRED_RACK_IDS[1] else old_racks[rack_id]
        tomb = retired[rack_id]
        event = audits[audit_id]['details']
        if ({k:v for k,v in tomb.items() if k not in {'retired_at','retired_reason'}} != rack
                or not tomb.get('retired_at') or not tomb.get('retired_reason')):
            _reject('退役对象未保留完整原身份和几何')
        if any(x['is_active'] and x.get('map_rack_id') == rack_id for x in locations):
            _reject('退役架仍有启用正式库位')
        cells = {'MCELL-'+c['id'].upper() for bucket in ('mold_cells','retired_mold_cells') for c in rack.get(bucket,[])}
        if any(m.get('rack_location') in cells for m in molds):
            _reject('退役格仍被模具本体使用')
        expected = dict(id=rack_id, rack_code=rack['rack_code'], area_code=rack['area_code'],
            deleted=True, inventory_changed=False, published_map_changed=True, inactive_location_count=0,
            request_fingerprint=event['request_fingerprint'])
        if any(event.get(k)!=v for k,v in expected.items()) or event.get('floor_code')!='1F':
            _reject('退役审计身份错误')
        matches = [r for r in new.get('layout_edit_receipts',[]) if r.get('operation_key')==event['operation_key']]
        if (len(matches)!=1 or matches[0].get('action')!='rack.delete.published'
                or matches[0].get('result')!=expected or not matches[0].get('applied_at')):
            _reject('当前图退役回执不匹配原审计')
        receipts.append(matches[0])
    if new.get('layout_edit_receipts',[]) != old.get('layout_edit_receipts',[])+receipts:
        _reject('回执历史存在截断或额外编辑')
    if audits[41428]['details']['published_revision'] != CURRENT_REVISION:
        _reject('最后退役未指向当前正式版本')


def _protected_tables(db):
    available = set(inspect(db.connection()).get_table_names())
    return [table for name,table in sorted(Base.metadata.tables.items())
            if name in available and (name.startswith(('warehouse_','inventory_','mold_'))
            or name in {'floor3_location_layouts','receipt_staging_areas','product_storage_preferences'})]


def _rows(db, table):
    return [dict(row) for row in db.execute(select(table).order_by(*table.primary_key.columns)).mappings()]


def _snapshot(db):
    result = {}
    for table in _protected_tables(db):
        rows = _rows(db,table)
        result[table.name] = {'count':len(rows), 'sha256':_hash(rows)}
    return result


def _policy_rows(db):
    return _rows(db, WarehouseAreaStoragePolicy.__table__)


def _policies(db, new, audits):
    areas = list(db.scalars(select(WarehouseArea).join(WarehouseFloor)
        .where(WarehouseFloor.floor_number==1, WarehouseArea.construction_status=='enabled')
        .options(selectinload(WarehouseArea.storage_policy),selectinload(WarehouseArea.floor))
        .execution_options(populate_existing=True)))
    bound = [a for a in areas if a.storage_policy and a.storage_policy.status=='published']
    expected = set(POLICY_PINS)
    if {(a.storage_policy.id,a.id,a.storage_policy.version) for a in bound} != expected or len(expected)!=23:
        _reject('23 项区域或策略 ID/版本不匹配')
    features = _index(new['features'])
    receipts = {audits[i]['entity_id']:audits[i]['details'] for i in range(41377,41399)}
    changes = []
    for area in sorted(bound,key=lambda a:a.storage_policy.id):
        policy = area.storage_policy; feature = features.get(policy.map_feature_id) or {}
        if (area.floor.construction_status!='enabled' or policy.draft_map_revision
                or policy.published_map_revision!=BINDING_REVISION
                or feature.get('feature_kind')!='zone' or feature.get('erp_area_code')!=area.area_code
                or feature.get('formal_area_name')!=area.area_name
                or feature.get('formal_area_id') not in (None,area.id)
                or feature.get('formal_floor_id') not in (None,area.floor_id)
                or feature.get('storage_layout')!=policy.storage_layout
                or sorted(feature.get('allowed_inventory_types',[]))!=sorted(json.loads(policy.allowed_inventory_types_json))):
            _reject('区域身份、用途或发布依据变化')
        if area.area_code == audits[41400]['details']['formal_areas'][0]['area_code']:
            if policy.map_feature_id != next(r for r in new['retired_racks'] if r['id']==RETIRED_RACK_IDS[1])['area_feature_id']:
                _reject('单独发布区域不匹配退役架')
        else:
            receipt = receipts.get(policy.id) or {}
            if any(receipt.get(k)!=v for k,v in dict(area_id=area.id,map_feature_id=policy.map_feature_id,
                map_revision=BINDING_REVISION,policy_version=policy.version,previous_map_revision=SOURCE_REVISION,
                previous_policy_version=policy.version-1,unchanged_area_and_assets=True).items()):
                _reject('区域缺少独立原发布承接回执')
        changes.append(dict(policy_id=policy.id,area_id=area.id,area_code=area.area_code,
            before_version=policy.version,after_version=policy.version+1,
            previous_map_revision=BINDING_REVISION,map_revision=CURRENT_REVISION))
    return changes


def _staging(db, old, new):
    """Independent 6c geometry proof, with the original 12069 binding stated honestly.

    Same functional-location/nearby-geometry checks as receipt_staging_map, without
    that API's requirement that geometry source revision equal policy revision.
    """
    areas = list(db.scalars(select(WarehouseArea).join(ReceiptStagingArea).join(WarehouseFloor)
        .where(WarehouseFloor.floor_number==1).order_by(WarehouseArea.id)
        .options(selectinload(WarehouseArea.storage_policy),selectinload(WarehouseArea.floor))))
    if tuple(a.id for a in areas) != STAGING_AREA_IDS:
        _reject('待入库区域集合变化')
    features = _index(new['features']); source_features = _index(old['features']); result=[]
    for area in areas:
        policy=area.storage_policy; feature=features.get(policy.map_feature_id)
        if (not feature or source_features.get(policy.map_feature_id)!=feature or feature.get('no_stacking')
                or _nearby(old,feature)!=_nearby(new,feature)):
            _reject('待入库区域或邻近边界变化')
        locations=list(db.scalars(select(WarehouseLocation).where(WarehouseLocation.warehouse_floor==1,
            WarehouseLocation.area_code==area.area_code,WarehouseLocation.is_active.is_(True))
            .order_by(WarehouseLocation.id).options(selectinload(WarehouseLocation.floor3_layout))
            .execution_options(populate_existing=True)))
        if not locations:
            _reject('待入库区域没有启用功能位')
        for loc in locations:
            layout=loc.floor3_layout
            if (loc.address_kind!='functional' or loc.address_area_id not in (None,area.id) or loc.source_version!='CURRENT_MAP'
                    or loc.placement_status!='placed' or not layout or layout.version<1):
                _reject('待入库区域不是原正式功能位')
            values=[float(getattr(layout,k)) for k in ('left_pct','top_pct','width_pct','height_pct')]
            if (not all(math.isfinite(x) and x>=0 for x in values)
                    or values[0]+values[2]>100.0001 or values[1]+values[3]>100.0001):
                _reject('待入库功能位几何越界')
        pinned=tuple((x.id,x.address_version,location_signature(x,x.floor3_layout)) for x in locations)
        if pinned != STAGING_LOCATION_PINS.get(area.id):
            _reject('待入库原30位置身份、地址版本或完整几何签名变化')
        result.append(dict(area_id=area.id, location_ids=[x.id for x in locations],
                           signatures={str(x.id):location_signature(x,x.floor3_layout) for x in locations}))
    return result


def _ground(db, audits, current_floor):
    plans=list(db.scalars(select(WarehouseGroundLayoutPlan).join(WarehouseArea).join(WarehouseFloor)
        .where(WarehouseFloor.floor_number==1,WarehouseGroundLayoutPlan.status=='published')
        .options(selectinload(WarehouseGroundLayoutPlan.slots))))
    if len(plans)!=1 or plans[0].id!=GROUND_PLAN_ID:
        _reject('正式地堆计划集合变化')
    plan=plans[0]; receipt=audits[41399]['details']; policy=plan.area.storage_policy
    signatures={str(s.location_id):location_signature(s.location,s.location.floor3_layout)
        for s in plan.slots if s.location.is_active and s.location.floor3_layout}
    if (receipt.get('plan_id')!=plan.id or receipt.get('plan_version')!=plan.version
            or receipt.get('original_map_revision')!=plan.published_map_revision
            or receipt.get('area_id')!=plan.area_id or receipt.get('policy_version')!=policy.version
            or receipt.get('map_feature_id')!=policy.map_feature_id or receipt.get('map_revision')!=BINDING_REVISION
            or not signatures or receipt.get('locations')!=signatures
            or any(s.location.is_active and not s.location.floor3_layout for s in plan.slots)):
        _reject('原地堆计划或精确库位签名变化')
    try:
        validate_capacity_layout_slots_for_zone(current_floor,feature_id=policy.map_feature_id,
            slots=[dict(location_id=s.location_id,layout_kind=s.location.floor3_layout.layout_kind,
                **{k:float(getattr(s.location.floor3_layout,k)) for k in
                   ('left_pct','top_pct','width_pct','height_pct')})
                for s in plan.slots if s.location.is_active],allow_spatial_conflicts=False)
    except Floor1CandidatePlanningError as error:
        _reject('当前地堆严格几何核验失败：'+str(error))
    return dict(plan_id=plan.id,plan_version=plan.version,area_code=plan.area.area_code,
                strict_current_geometry=True,
                floor_code=plan.area.floor.floor_code,locations=signatures,
                original_map_revision=plan.published_map_revision)


def _preview(db, actor, source_bytes):
    old,new=_documents(source_bytes); audits=_audits(db)
    table_by_name={t.name:t for t in _protected_tables(db)}
    locations=_rows(db,WarehouseLocation.__table__)
    molds=_rows(db,table_by_name['mold_tools'])
    _geometry_and_audit_proof(old,new,audits,locations,molds)
    changes=_policies(db,new,audits)
    staging=_staging(db,old,new); ground=_ground(db,audits,new)
    result=dict(schema_version=1,action=ACTION,actor_id=actor.id,actor_auth_version=actor.auth_version,
        geometry_source_sha256=SOURCE_SHA,geometry_source_revision=SOURCE_REVISION,
        audited_binding_sha256=BINDING_SHA,audited_binding_revision=BINDING_REVISION,
        exact_binding_source_available=False,runtime_sha256=CURRENT_SHA,draft_sha256=DRAFT_SHA,
        historical_audit_sha256=HISTORICAL_AUDIT_SHA,changes=changes,staging=staging,ground=ground,
        protected_snapshot=_snapshot(db),policy_rows=json.loads(_canonical(_policy_rows(db))))
    # Bound file bytes at both sides of the DB read, including preview-only calls.
    _documents(source_bytes)
    result['preview_sha256']=_hash(result)
    return result


def preview(db, *, actor, source_bytes):
    """Read-only plan. Its complete object must be supplied unchanged to apply."""
    _clean(db)
    with editor.WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        return _preview(db,_actor(db,actor),source_bytes)


def apply(db, *, actor, source_bytes, expected_preview, operation_key, commit=True):
    """Apply one reviewed plan atomically; commit=False leaves the outer commit to caller.

    The caller must not interleave map publication before its outer commit. The
    default owns commit while the shared map lock is held. Same-key replay returns
    the original full receipt and makes no database/file changes.
    """
    _clean(db)
    _read(source_bytes, SOURCE_SHA)
    if not isinstance(operation_key,str) or not operation_key.strip() or len(operation_key)>80:
        _reject('操作键无效')
    if not isinstance(expected_preview,dict) or expected_preview.get('preview_sha256') != _hash(
            {k:v for k,v in expected_preview.items() if k!='preview_sha256'}):
        _reject('预览内容校验失败')
    with editor.WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        try:
            with atomic_bom(db):
                actor=_actor(db,actor)
                request_hash=_hash(dict(action=ACTION,actor_id=actor.id,preview=expected_preview))
                key='map-audit-20261014:'+operation_key
                prior=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==key))
                if prior:
                    if (prior.action!=ACTION or prior.request_hash!=request_hash or prior.actor_user_id!=actor.id
                            or prior.resource_type!='map_binding_recovery' or prior.resource_id!=1):
                        _reject('同一操作键已经用于其他内容或操作人')
                    receipt=json.loads(prior.response_json)
                    if receipt.get('request_sha256')!=request_hash or receipt.get('receipt_sha256') != _hash(
                            {k:v for k,v in receipt.items() if k!='receipt_sha256'}):
                        _reject('完整幂等回执损坏')
                else:
                    if not claim_warehouse_floor_projection(db,floor_number=1):
                        _reject('不能锁定一楼投影')
                    db.expire_all()
                    actor=_actor(db,actor)
                    fresh=_preview(db,actor,source_bytes)
                    if fresh!=expected_preview:
                        _reject('预览已过期，请重新核对')
                    old,new=_documents(source_bytes)
                    for change in fresh['changes']:
                        policy=db.get(WarehouseAreaStoragePolicy,change['policy_id'])
                        _advance_policy_version(db,policy=policy,operator_id=actor.id,published_map_revision=CURRENT_REVISION)
                    # Independent staging proof above is preserved in the receipt.
                    # This existing call now validates actual current ground geometry;
                    # no fake 12069 source or spatial-conflict exemption is supplied.
                    ground_count=record_map_applications(db,previous_floor_layout=old,floor_layout=new,
                        actor=actor,operation_key=key)
                    if ground_count!=1:
                        _reject('必须写入唯一 FIN-001 实际地堆适用回执')
                    assert_current_floor_bindings(db,new)
                    plan=published_ground_plan(db,floor_code=fresh['ground']['floor_code'],
                        area_code=fresh['ground']['area_code'],required_inventory_type='finished')
                    if plan.id!=GROUND_PLAN_ID or plan.version!=fresh['ground']['plan_version']:
                        _reject('原地堆计划被替换')
                    for area in fresh['staging']:
                        for location_id in area['location_ids']:
                            issue=operational_location_issue(db,db.get(WarehouseLocation,location_id),
                                require_published=True,require_map_geometry=True)
                            if issue:
                                _reject('待入库功能位正式可用性失败：'+issue)
                    db.flush()
                    after=_snapshot(db)
                    policy_table=WarehouseAreaStoragePolicy.__tablename__
                    if {k:v for k,v in after.items() if k!=policy_table}!={k:v for k,v in fresh['protected_snapshot'].items() if k!=policy_table}:
                        _reject('受保护的区域、库位、地堆、实物或模具发生变化')
                    before_policies=deepcopy(fresh['policy_rows'])
                    changed={c['policy_id'] for c in fresh['changes']}
                    after_policies=json.loads(_canonical(_policy_rows(db)))
                    for rows in (before_policies,after_policies):
                        for row in rows:
                            if row['id'] in changed:
                                row.pop('updated_at',None);row.pop('updated_by',None)
                    for row in before_policies:
                        if row['id'] in changed:
                            row['version']+=1;row['published_map_revision']=CURRENT_REVISION
                    if before_policies!=after_policies:
                        _reject('策略变化超出23项版本及发布绑定')
                    _documents(source_bytes)
                    receipt=dict(schema_version=1,action=ACTION,request_sha256=request_hash,
                        operation_key=operation_key,preview=fresh,after_snapshot=after,
                        ground_map_application_count=ground_count,staging_verified_count=len(fresh['staging']),
                        inventory_changed=False,mold_location_changed=False,map_files_changed=False)
                    receipt['receipt_sha256']=_hash(receipt)
                    saved=FinanceIdempotencyRecord(idempotency_key=key,request_hash=request_hash,action=ACTION,
                        actor_user_id=actor.id,resource_type='map_binding_recovery',resource_id=1,
                        response_json=_canonical(receipt))
                    db.add(saved);db.flush()
                    details=dict(receipt_id=saved.id,receipt_sha256=receipt['receipt_sha256'],
                        preview_sha256=fresh['preview_sha256'],geometry_source_revision=SOURCE_REVISION,
                        audited_binding_revision=BINDING_REVISION,map_revision=CURRENT_REVISION,
                        exact_binding_source_available=False,policy_count=23,ground_count=1,staging_count=4)
                    logged=append_audit_event(db,actor=actor,event_category='business',result='success',source='web',
                        module_code='warehouse',action_code=ACTION,resource='WarehouseAreaStoragePolicy',
                        entity_type='map_binding_recovery',entity_id=1,request_id=request_hash,
                        description='按真实前版几何和独立发布/退役审计恢复23项绑定；未重建缺失原图',details=details)
                    if json.loads(logged.details or '{}')!=details:
                        _reject('恢复审计未完整保存')
                    db.flush()
                    if _snapshot(db) != after:
                        _reject('审计期间受保护快照发生变化')
                    _audits(db)
                    _documents(source_bytes)
            if commit:
                db.commit()
            return receipt
        except Exception:
            if commit:
                db.rollback()
            raise

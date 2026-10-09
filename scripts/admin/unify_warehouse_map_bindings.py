"""Reviewed metadata-only map unification; invoked by the backed-up maintenance job.

No CLI or implicit production writes. The caller owns the stopped-service lock,
verified backup, file compare-and-swap/rollback and SQL transaction. This module
prepares a deterministic candidate and applies only the reviewed area bindings.
"""
from copy import deepcopy
from hashlib import sha256
import json

from app.services.warehouse_map_publication import published_floor_areas, assert_current_floor_bindings
from app.services.warehouse_area_activation import _advance_policy_version, policy_inventory_types, WarehouseAreaActivationError
from app.services.warehouse_twin_layout_editor import _floor_revision
from app.services.audit_log import append_audit_event

ACTION = "warehouse.map.unification"
BINDING_KEYS = ("erp_area_code", "formal_area_name", "formal_area_id", "formal_floor_id", "storage_layout", "allowed_inventory_types")


def render(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise WarehouseAreaActivationError(message, status_code=409)


def area_snapshot(floor, feature_id):
    features = [f for f in floor.get("features", []) if f.get("id") == feature_id]
    require(len(features) == 1 and features[0].get("feature_kind") == "zone", "区域图形无法唯一核对")
    result = {"bounds": floor.get("bounds_mm"), "feature": features[0]}
    for key in ("racks", "pallets"):
        members = [x for x in floor.get(key, []) if x.get("area_feature_id") == feature_id]
        ids = [x.get("id") for x in members]
        require(all(ids) and len(ids) == len(set(ids)), "区域存在重复货架或栈板身份")
        require(all(sum(x.get('id') == ident for x in floor.get(key, [])) == 1 for ident in ids), "货架身份重复")
        result[key] = sorted(members, key=lambda x:x['id'])
    return result


def prepare(db, published, draft, sources):
    candidate = deepcopy(published); draft_candidate = deepcopy(draft)
    plan = {"schema": 1, "before_map": fingerprint(published), "before_draft": fingerprint(draft),
            "areas": [], "metadata_changes": [], "floor_revisions": {}}
    for code, current in published["floors"].items():
        areas = [a for a in published_floor_areas(db, code) if a.storage_policy and a.storage_policy.status == "published"]
        if not any(a.storage_policy.published_map_revision != current['revision'] for a in areas):
            assert_current_floor_bindings(db, current)
            continue
        target = candidate['floors'][code]
        active_ids = [a.storage_policy.map_feature_id for a in areas]
        require(len(set(active_ids)) == len(active_ids), "多个正式区域指向同一地图图形")
        for area in areas:
            p = area.storage_policy
            require(not p.draft_map_revision, f"{area.area_name}尚有策略草稿，不能自动覆盖")
            source = current if p.published_map_revision == current['revision'] else sources.get((code,p.published_map_revision))
            require(source is not None and source.get('revision') == p.published_map_revision, "缺少区域原发布依据")
            require(area_snapshot(source,p.map_feature_id) == area_snapshot(current,p.map_feature_id), f"{area.area_name}布局有真实变化")
            f = next(x for x in target['features'] if x.get('id') == p.map_feature_id)
            require(f.get('erp_area_code') in (None, '', area.area_code), "现有地图区域编码存在冲突")
            require(f.get('formal_area_id') in (None,area.id) and f.get('formal_floor_id') in (None,area.floor_id), "现有地图区域身份存在冲突")
            require(not any(x.get('erp_area_code') == area.area_code and x.get('id') != p.map_feature_id for x in target['features']), "正式区域在地图中重复")
            values = dict(erp_area_code=area.area_code,formal_area_name=area.area_name,formal_area_id=area.id,
                          formal_floor_id=area.floor_id,storage_layout=p.storage_layout,allowed_inventory_types=policy_inventory_types(p))
            changes = {k:{'before':f.get(k),'after':v} for k,v in values.items() if f.get(k) != v}
            if changes:
                plan['metadata_changes'].append(dict(floor=code,area_id=area.id,object='zone',id=f['id'],changes=changes))
                f.update(values); f['version'] = int(f.get('version') or 0) + 1
            for collection in ('racks','pallets'):
                for obj in target.get(collection,[]):
                    if obj.get('area_feature_id') != p.map_feature_id: continue
                    require(obj.get('area_code') in (None,'',area.area_code,f.get('feature_code')), "货架所属区域存在冲突")
                    if obj.get('area_code') != area.area_code:
                        plan['metadata_changes'].append(dict(floor=code,area_id=area.id,object=collection,id=obj['id'],changes={'area_code':{'before':obj.get('area_code'),'after':area.area_code}}))
                        obj['area_code'] = area.area_code; obj['version'] = int(obj.get('version') or 0) + 1
            plan['areas'].append(dict(floor=code,area_id=area.id,policy_id=p.id,policy_version=p.version,
                feature_id=p.map_feature_id,old_revision=p.published_map_revision,area_code=area.area_code))
        target['revision'] = _floor_revision(target)
        plan['floor_revisions'][code] = {'before':current['revision'],'after':target['revision']}
        if draft is not None:
            old_draft = draft['floors'][code]
            for key in ('features','racks','pallets'):
                require(old_draft.get(key,[]) == current.get(key,[]), "地图仍有未发布空间草稿，不能自动覆盖")
                draft_candidate['floors'][code][key] = deepcopy(target.get(key,[]))
            draft_candidate['floors'][code]['revision'] = _floor_revision(draft_candidate['floors'][code])
            draft_candidate['draft_meta']['base_floor_revisions'][code] = target['revision']
    for row in plan['areas']: row['new_revision'] = candidate['floors'][row['floor']]['revision']
    if draft_candidate is not None and plan['areas']:
        draft_candidate['draft_meta']['base_published_sha256'] = sha256(render(candidate)).hexdigest()
    plan['after_map'] = fingerprint(candidate); plan['after_draft'] = fingerprint(draft_candidate)
    return plan, candidate, draft_candidate


def apply_bindings(db, plan, candidate, actor, operation_key):
    from sqlalchemy import select
    from app.models.audit import OperationLog
    from app.models.warehouse_inventory import WarehouseArea
    require(actor and actor.id and actor.role == 'admin', '需要管理员执行地图统一')
    require(bool(operation_key) and fingerprint(candidate) == plan.get('after_map'), '地图统一操作身份或候选内容不一致')
    changed = 0
    for row in plan['areas']:
        area = db.get(WarehouseArea,row['area_id']); p = area.storage_policy if area else None
        request_id = sha256(f'{operation_key}:{row["policy_id"]}'.encode()).hexdigest()
        prior = db.scalar(select(OperationLog).where(OperationLog.action_code == ACTION,OperationLog.request_id == request_id))
        if prior:
            saved = json.loads(prior.details or '{}')
            require(saved.get('plan_sha256') == fingerprint(plan) and p and p.version == row['policy_version']+1
                and p.published_map_revision == row['new_revision'], '同一地图统一操作的内容或现状不一致')
            continue
        require(p and p.id == row['policy_id'] and p.version == row['policy_version']
            and p.published_map_revision == row['old_revision'] and p.status == 'published'
            and not p.draft_map_revision and area.construction_status == 'enabled'
            and p.map_feature_id == row['feature_id'], '区域状态或版本已变化，请重新核对')
        _advance_policy_version(db,policy=p,operator_id=actor.id,published_map_revision=row['new_revision'])
        # Flatten per-field before/after values so list-valued inventory types
        # remain below the bounded audit serializer's depth limit.
        changes = [dict(object=x['object'],id=x['id'],field=key,**delta)
            for x in plan['metadata_changes'] if x['area_id'] == row['area_id']
            for key,delta in x['changes'].items()]
        details = dict(row,after_policy_version=p.version,plan_sha256=fingerprint(plan),
            metadata_changes=changes)
        saved = append_audit_event(db,actor=actor,event_category='business',result='success',source='script',
            module_code='warehouse',action_code=ACTION,resource='WarehouseAreaStoragePolicy',
            entity_type='warehouse_area_storage_policy',entity_id=p.id,
            request_id=request_id,
            description='全地图统一正式版本；原位置、库存、模具和打印历史保留',details=details)
        require(json.loads(saved.details or '{}') == details, '地图统一审计未完整保存')
        changed += 1
    for floor in candidate['floors'].values(): assert_current_floor_bindings(db,floor)
    return changed

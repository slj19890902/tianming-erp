"""Versioned ground-location geometry inside the existing map draft.

Draft rectangles are absolute millimetres and may conflict. Only application
converts them to the operational zone-relative coordinates in one transaction.
No inventory quantity, location identity or address is created or renumbered.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import math

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.models.warehouse_inventory import (
    WarehouseArea, WarehouseAreaStoragePolicy, WarehouseFloor, WarehouseLocation,
    WarehouseGroundLayoutPlan, WarehouseGroundLayoutSlot,
)
from app.services.warehouse_ground_map_application import location_signature
from app.services.warehouse_floor1_candidate_planner import (
    validate_capacity_layout_slots_for_zone, _percent_round_trip_epsilon,
)
from app.services.warehouse_twin_layout_editor import WarehouseTwinLayoutEditConflictError

KEY = 'ground_location_draft'


def _fail(message):
    raise WarehouseTwinLayoutEditConflictError(message)


def _bounds(points):
    xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
    x, y, right, top = min(xs), min(ys), max(xs), max(ys)
    if not all(math.isfinite(v) for v in (x, y, right, top)) or right <= x or top <= y:
        _fail('区域坐标无效，不能保存货位调整')
    return x, y, right-x, top-y


def _rows(db, floor_code, feature_id):
    policy = db.scalar(select(WarehouseAreaStoragePolicy)
        .join(WarehouseArea).join(WarehouseFloor)
        .where(WarehouseFloor.floor_code == floor_code,
               WarehouseAreaStoragePolicy.map_feature_id == feature_id)
        .options(selectinload(WarehouseAreaStoragePolicy.area)))
    if not policy:
        return None, []
    rows = list(db.scalars(select(WarehouseLocation)
        .where(WarehouseLocation.warehouse_floor == int(floor_code[:-1]),
               func.upper(WarehouseLocation.area_code) == policy.area.area_code.upper(),
               WarehouseLocation.is_active.is_(True), WarehouseLocation.storage_type == 'ground')
        .options(selectinload(WarehouseLocation.floor3_layout))
        .order_by(WarehouseLocation.id)))
    if any(row.floor3_layout is None for row in rows):
        _fail('区域有货位缺少坐标，请先核对货位台账')
    return policy, rows


def pending_adjustment(feature, published):
    value = feature.get(KEY)
    if not value:
        return None
    if value.get('base_revision') == published.get('revision'):
        return value
    original = next((f for f in published.get('features', []) if f['id'] == feature['id']), {})
    # Applied metadata remains an audit trail in the map; it is not a new draft.
    if value == original.get(KEY):
        return None
    _fail('货位调整所依据的已应用地图已变化，请重读草稿核对')


def prepare_adjustment(db, *, floor_code, feature, published):
    existing = pending_adjustment(feature, published)
    if existing:
        verify_adjustment(db, floor_code=floor_code, feature=feature, published=published)
        return deepcopy(existing)
    policy, rows = _rows(db, floor_code, feature['id'])
    if not rows:
        return None
    if policy.status != 'published':
        _fail('区域用途草稿尚未应用，请先处理区域用途')
    original = next((f for f in published.get('features', []) if f['id'] == feature['id']), None)
    if not original:
        _fail('原区域坐标缺失，无法保留货位实际位置')
    x, y, w, h = _bounds(original['points'])
    slots = []
    for row in rows:
        layout = row.floor3_layout
        sw, sh = w*float(layout.width_pct)/100, h*float(layout.height_pct)/100
        slots.append(dict(location_id=row.id, source_signature=location_signature(row, layout),
            expected_version=layout.version, x_mm=x+w*float(layout.left_pct)/100,
            y_mm=y+h-h*float(layout.top_pct)/100-sh, width_mm=sw, depth_mm=sh,
            z_index=layout.z_index, layout_kind=layout.layout_kind))
    plans = list(db.scalars(select(WarehouseGroundLayoutPlan).where(
        WarehouseGroundLayoutPlan.area_id == policy.area_id,
        WarehouseGroundLayoutPlan.status == 'published')))
    return dict(base_revision=published['revision'], area_id=policy.area_id,
        policy_version=policy.version, source_points=deepcopy(original['points']),
        plan_versions={str(p.id): p.version for p in plans}, slots=slots)


def verify_adjustment(db, *, floor_code, feature, published):
    value = pending_adjustment(feature, published)
    if not value:
        return None
    policy, rows = _rows(db, floor_code, feature['id'])
    if not policy or policy.area_id != value['area_id'] or policy.version != value['policy_version']:
        _fail('区域设置已变化，货位调整未应用；请重读核对')
    actual = {r.id: location_signature(r, r.floor3_layout) for r in rows}
    slots = value['slots']
    expected = {s['location_id']: s['source_signature'] for s in slots}
    if len(expected) != len(slots) or expected != actual:
        _fail('货位集合或位置已被其他操作更新，调整草稿未应用')
    original = next((f for f in published.get('features', []) if f['id'] == feature['id']), {})
    if original.get('points') != value['source_points']:
        _fail('原区域坐标已变化，调整草稿未应用')
    versions = {str(p.id): p.version for p in db.scalars(select(WarehouseGroundLayoutPlan).where(
        WarehouseGroundLayoutPlan.area_id == policy.area_id, WarehouseGroundLayoutPlan.status == 'published'))}
    if versions != value['plan_versions']:
        _fail('原地堆排位已变化，调整草稿未应用')
    return value


def adjust_slot_positions(value, changes, *, floor_bounds):
    result = deepcopy(value)
    by_id = {s['location_id']: s for s in result['slots']}
    seen = set()
    for change in changes:
        identity = change['location_id']
        if identity in seen or identity not in by_id:
            _fail('提交包含重复货位或其他区域货位')
        seen.add(identity)
        slot = by_id[identity]
        if change['expected_version'] != slot['expected_version']:
            _fail('货位版本已变化，请重读后重试')
        x, y = float(change['x_mm']), float(change['y_mm'])
        if not math.isfinite(x) or not math.isfinite(y):
            _fail('货位坐标必须是有限数值')
        if (x < floor_bounds['min_x'] or y < floor_bounds['min_y'] or
            x+slot['width_mm'] > floor_bounds['max_x'] or y+slot['depth_mm'] > floor_bounds['max_y']):
            _fail('货位不能移到整层地图范围之外')
        # Width, height, identity and source-version fields are not editable here.
        slot.update(x_mm=round(x, 3), y_mm=round(y, 3))
    return result


def rebase_verified_adjustments(db, *, floor_code, remaining, published):
    """Rebase only source metadata after a scoped publish; retain operator points."""
    rebased = {}
    for feature_id, old in remaining.items():
        feature = next(f for f in published['features'] if f['id'] == feature_id)
        clean = deepcopy(feature)
        clean.pop(KEY, None)
        current = prepare_adjustment(db, floor_code=floor_code, feature=clean, published=published)
        if not current or {s['location_id']: s['source_signature'] for s in current['slots']} != {
            s['location_id']: s['source_signature'] for s in old['slots']
        }:
            _fail('其他区域的正式货位发生变化，未应用本次调整；原草稿保留')
        desired = {s['location_id']: s for s in old['slots']}
        for slot in current['slots']:
            for key in ('x_mm', 'y_mm', 'width_mm', 'depth_mm'):
                slot[key] = desired[slot['location_id']][key]
        rebased[feature_id] = current
    return rebased


def relative_slots(feature, value):
    x, y, w, h = _bounds(feature['points'])
    return [{**s, 'left_pct': round((s['x_mm']-x)/w*100, 4),
        'top_pct': round((y+h-s['y_mm']-s['depth_mm'])/h*100, 4),
        'width_pct': round(s['width_mm']/w*100, 4),
        'height_pct': round(s['depth_mm']/h*100, 4)} for s in value['slots']]


def validate_adjustments(db, *, floor_code, draft, published):
    """Saving is permissive; application is checked against the whole draft map."""
    result = {}
    for feature in draft.get('features', []):
        value = verify_adjustment(db, floor_code=floor_code, feature=feature, published=published)
        if not value:
            continue
        slots = relative_slots(feature, value)
        validate_capacity_layout_slots_for_zone(draft, feature_id=feature['id'], slots=slots)
        result[feature['id']] = (value, slots)
    return result


def apply_adjustments(db, *, floor_code, draft, published, new_revision):
    """Called inside the existing publish transaction, before policy validation."""
    adjustments = validate_adjustments(db, floor_code=floor_code, draft=draft, published=published)
    audit = []
    for feature_id, (value, slots) in adjustments.items():
        _, rows = _rows(db, floor_code, feature_id)
        by_id = {r.id: r for r in rows}
        plans = list(db.scalars(select(WarehouseGroundLayoutPlan)
            .where(WarehouseGroundLayoutPlan.id.in_([int(k) for k in value['plan_versions']]))
            .options(selectinload(WarehouseGroundLayoutPlan.slots))))
        plan_slots = {s.location_id: s for plan in plans for s in plan.slots}
        feature = next(f for f in draft['features'] if f['id'] == feature_id)
        tolerance = max(1.0, _percent_round_trip_epsilon(feature['points']))
        for slot in slots:
            row = by_id[slot['location_id']]
            layout = row.floor3_layout
            before = {k: float(getattr(layout, k)) for k in ('left_pct','top_pct','width_pct','height_pct')}
            for k in before:
                setattr(layout, k, Decimal(str(slot[k])))
            layout.version += 1
            layout.source_type = 'manual'
            ground = plan_slots.get(row.id)
            original_ground = None
            if ground:
                original_ground = {k: float(getattr(ground, k)) for k in ('x_mm','y_mm','width_mm','depth_mm')}
                if (abs(ground.width_mm-slot['width_mm']) > tolerance or
                    abs(ground.depth_mm-slot['depth_mm']) > tolerance):
                    _fail('货位实际占地尺寸发生变化，未应用调整')
            audit.append(dict(location_id=row.id, feature_id=feature_id,
                before=before, after={k:slot[k] for k in before},
                after_version=layout.version, source_signature=slot['source_signature'],
                source_map_revision=published['revision'], target_map_revision=new_revision,
                absolute={k:slot[k] for k in ('x_mm','y_mm','width_mm','depth_mm')},
                original_ground=original_ground))
    db.flush()
    return audit

"""Pure, fail-closed evidence preview; never opens a path or changes a database.

A successful result permits designing an owned-copy rehearsal only. It is not
a production mutation command. The caller supplies exact raw-byte pins from the
read-only capture and independently verifies location/ground/staging evidence.
"""
from __future__ import annotations

from hashlib import sha256
import json


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _read(raw, expected_hash):
    if not isinstance(raw, bytes) or sha256(raw).hexdigest() != expected_hash:
        raise ValueError('raw_document_sha_mismatch')
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_number')))


def _indexed(values):
    result = {}
    for value in values:
        key = value.get('id')
        if not key or key in result:
            raise ValueError('missing_or_duplicate_object_id')
        result[key] = value
    return result


def preview(*, source_bytes, current_bytes, pins, snapshot, retirement_events):
    """Require an exact source and exactly the two specified empty retirements.

    snapshot keys: areas, policies, locations, mold_tools. Its complete canonical
    SHA is pinned in the plan; production execution would re-read under locks.
    Event keys: rack_id, operation_key, request_fingerprint. No source image is
    synthesized from the target and no legacy revision is reassigned by this
    function. Even a complete map proof leaves transaction/ground checks pending.
    """
    result = dict(ready_for_owned_copy_rehearsal=False, eligible_for_formal_write=False,
                  proposed_policy_changes=[], blockers=[],
                  pending_checks=['fresh_snapshot_and_map_CAS_under_floor_lock',
                      'actual_ground_and_staging_services', 'actor_and_audit_transaction',
                      'all_23_bindings_and_location_stock_draft_invariants'])
    try:
        if sha256(_canonical(snapshot)).hexdigest() != pins['snapshot_sha256']:
            raise ValueError('snapshot_sha_mismatch')
        source = _read(source_bytes, pins['source_sha256'])
        target = _read(current_bytes, pins['current_sha256'])
        code = pins['floor_code']
        if source.get('schema_version') != 1 or target.get('schema_version') != 1:
            raise ValueError('unsupported_schema')
        if set(source) != set(target) or set(source['floors']) != set(target['floors']):
            raise ValueError('document_shape_changed')
        for key in source:
            if key not in ('floors', 'generated_at') and _canonical(source[key]) != _canonical(target[key]):
                raise ValueError('document_metadata_changed')
        for floor_code in source['floors']:
            if floor_code != code and _canonical(source['floors'][floor_code]) != _canonical(target['floors'][floor_code]):
                raise ValueError('other_floor_changed')
        old, new = source['floors'][code], target['floors'][code]
        for floor, revision in ((old, pins['source_revision']), (new, pins['current_revision'])):
            if floor.get('floor_code') != code or floor.get('revision') != revision:
                raise ValueError('floor_revision_mismatch')
            calculated = sha256(_canonical({k: v for k, v in floor.items() if k != 'revision'})).hexdigest()[:16]
            if calculated != revision:
                raise ValueError('floor_revision_not_bound_to_content')
        events = {event['rack_id']: event for event in retirement_events}
        if len(events) != 2 or len(retirement_events) != 2:
            raise ValueError('exactly_two_distinct_retirements_required')
        variable = {'racks', 'retired_racks', 'revision', 'layout_edited_at', 'layout_edit_receipts'}
        if _canonical({k: v for k, v in old.items() if k not in variable}) != _canonical(
                {k: v for k, v in new.items() if k not in variable}):
            raise ValueError('protected_floor_geometry_source_or_metadata_changed')
        old_racks, new_racks = _indexed(old.get('racks', [])), _indexed(new.get('racks', []))
        old_retired, new_retired = _indexed(old.get('retired_racks', [])), _indexed(new.get('retired_racks', []))
        if set(old_racks) - set(new_racks) != set(events) or set(new_racks) - set(old_racks):
            raise ValueError('unexpected_active_rack_set')
        if set(new_retired) - set(old_retired) != set(events) or set(old_retired) - set(new_retired):
            raise ValueError('unexpected_retired_rack_set')
        for key, rack in new_racks.items():
            if _canonical(rack) != _canonical(old_racks[key]):
                raise ValueError('remaining_rack_changed')
        for key, rack in old_retired.items():
            if _canonical(rack) != _canonical(new_retired[key]):
                raise ValueError('prior_tombstone_changed')
        for key, event in events.items():
            rack, tombstone = old_racks[key], new_retired[key]
            if not tombstone.get('retired_at') or not tombstone.get('retired_reason'):
                raise ValueError('retirement_metadata_missing')
            if _canonical(rack) != _canonical({k: v for k, v in tombstone.items()
                                              if k not in ('retired_at', 'retired_reason')}):
                raise ValueError('tombstone_not_original_rack')
            if any(r.get('is_active') and r.get('map_rack_id') == key for r in snapshot['locations']):
                raise ValueError('active_location_still_references_retired_rack')
            cells = {'MCELL-' + c['id'].upper() for bucket in ('mold_cells', 'retired_mold_cells')
                     for c in rack.get(bucket, [])}
            if any(m.get('rack_location') in cells for m in snapshot['mold_tools']):
                raise ValueError('current_mold_still_references_retired_cell')
            matches = [r for r in new.get('layout_edit_receipts', [])
                       if r.get('operation_key') == event['operation_key']]
            if len(matches) != 1:
                raise ValueError('retirement_receipt_missing_or_duplicated')
            receipt = matches[0]
            expected = dict(id=key, rack_code=rack['rack_code'], area_code=rack['area_code'],
                deleted=True, inventory_changed=False, published_map_changed=True,
                inactive_location_count=0, request_fingerprint=event['request_fingerprint'])
            if (receipt.get('action') != 'rack.delete.published' or not receipt.get('applied_at')
                    or _canonical(receipt.get('result')) != _canonical(expected)):
                raise ValueError('retirement_receipt_does_not_match_audit')
        prior = old.get('layout_edit_receipts', [])
        added = [r for r in new.get('layout_edit_receipts', [])
                 if r.get('operation_key') in {e['operation_key'] for e in events.values()}]
        if len(prior) > 98 or _canonical(new.get('layout_edit_receipts', [])) != _canonical(prior + added):
            raise ValueError('receipt_history_not_complete_or_other_edits')
        _indexed(old.get('features', [])); _indexed(new.get('features', []))
        for collection in ('pallets',):
            _indexed(old.get(collection, [])); _indexed(new.get(collection, []))
        areas, policies = _indexed(snapshot['areas']), _indexed(snapshot['policies'])
        if len(areas) != 23 or len(policies) != 23:
            raise ValueError('unexpected_policy_or_area_count')
        if len({p['area_id'] for p in policies.values()}) != 23:
            raise ValueError('duplicate_area_policy')
        changes = []
        for policy in policies.values():
            area = areas[policy['area_id']]
            features = [f for f in new.get('features', []) if f.get('id') == policy['map_feature_id']]
            if len(features) != 1:
                raise ValueError('feature_not_unique')
            feature = features[0]
            if (area['construction_status'] != 'enabled' or policy['status'] != 'published'
                    or policy.get('draft_map_revision') or policy['published_map_revision'] != old['revision']
                    or feature.get('feature_kind') != 'zone' or feature.get('erp_area_code') != area['area_code']
                    or feature.get('formal_area_name') != area['area_name']
                    or feature.get('formal_area_id') not in (None, area['id'])
                    or feature.get('formal_floor_id') not in (None, area['floor_id'])
                    or feature.get('storage_layout') != policy['storage_layout']
                    or sorted(feature.get('allowed_inventory_types') or []) != sorted(
                        json.loads(policy['allowed_inventory_types_json']))
                    or sum(f.get('erp_area_code') == area['area_code'] for f in new['features']) != 1):
                raise ValueError('area_policy_or_feature_contract_changed')
            changed_racks = [key for key in events if old_racks[key].get('area_feature_id') == feature['id']]
            changes.append(dict(area_id=area['id'], policy_id=policy['id'], feature_id=feature['id'],
                before_version=policy['version'], proposed_version=policy['version'] + 1,
                source_revision=old['revision'], target_revision=new['revision'],
                retired_rack_ids=changed_racks, strategy='explicit_retirement' if changed_racks else 'unchanged'))
        if sum(bool(c['retired_rack_ids']) for c in changes) != 2:
            raise ValueError('retirements_must_belong_to_two_bound_areas')
        result.update(ready_for_owned_copy_rehearsal=True, proposed_policy_changes=changes)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        result['blockers'].append(str(error))
    return result

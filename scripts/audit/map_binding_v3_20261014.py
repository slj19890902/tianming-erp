"""Bounded, read-only map binding evidence preview for the 2026-10-14 task.

No application imports, connections to production, repair SQL, map writes, or
paths dereferenced from evidence documents. The two inputs must be the saved
export and the explicitly retained managed baseline. Output is create-only.
An identity match in the current map is NOT authorization to advance a policy.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3


EVIDENCE_ROOT = Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014')
EXPORT = EVIDENCE_ROOT / 'current-business-readonly.json'
BASELINE = EVIDENCE_ROOT / 'managed-tasks/task-2a4eba8403ba40fba6eff95f2961e553/copies/carton_erp.sqlite3'
SOURCE_REVISION = '12069ae4137257d6'
TARGET_REVISION = 'e919e36e2f3ec786'
DELETED_RACKS = ('83b533e9-e643-43ee-a68c-b151e951808e',
                 'd00e4e04-8043-4a98-bc4d-f018406d5aff')
TABLES = ('warehouse_floors', 'warehouse_areas', 'warehouse_area_storage_policies',
          'warehouse_locations', 'floor3_location_layouts', 'warehouse_ground_layout_plans',
          'mold_tools', 'mold_location_movements', 'inventory_lots')


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def subset(value, keys):
    return {key: value.get(key) for key in keys.split()}


def single(values, description):
    if len(values) != 1:
        raise ValueError(f'{description}: expected exactly one, found {len(values)}')
    return values[0]


def in_bounds(feature, bounds):
    points = feature.get('points') or []
    return len(points) >= 3 and all(
        bounds['min_x'] <= p[0] <= bounds['max_x']
        and bounds['min_y'] <= p[1] <= bounds['max_y'] for p in points)


def physical_rack(rack, tables):
    result = subset(rack, 'id rack_code name mold_rack_code area_code area_feature_id '
                    'x_mm y_mm z_mm width_mm depth_mm height_mm rotation_deg levels '
                    'level_cell_counts version retired_at retired_reason restored_at')
    result['object_sha256'] = digest(rack)
    result['cells'] = []
    for state, key in [('current_in_object', 'mold_cells'), ('retired_in_object', 'retired_mold_cells')]:
        for cell in rack.get(key, []):
            encoded = 'MCELL-' + cell['id'].upper()
            tools = [m for m in tables['mold_tools'] if m.get('rack_location') == encoded]
            moves = [m for m in tables['mold_location_movements']
                     if encoded in (m.get('from_location'), m.get('to_location'))]
            result['cells'].append(dict(subset(cell, 'id level grid alias'), state=state,
                encoded_location=encoded, current_mold_ids=[m['id'] for m in tools],
                active_mold_ids=[m['id'] for m in tools if m.get('is_active')],
                historical_movement_ids=[m['id'] for m in moves]))
    result['formal_location_ids'] = [r['id'] for r in tables['warehouse_locations']
                                     if r.get('map_rack_id') == rack['id']]
    return result


def read_audit(db):
    rows = db.execute('SELECT id, action, action_code, created_at, details '
                      'FROM operation_logs WHERE id BETWEEN 41376 AND 41428 OR id = 41588')
    result = []
    for row in rows:
        details = json.loads(row['details'] or '{}')
        if row['id'] in (41376, 41400, 41401, 41406, 41428, 41588) or (
                row['action_code'] in ('warehouse.area.map_application',
                                      'warehouse.ground_layout.map_application')
                and details.get('map_revision') == SOURCE_REVISION):
            result.append(dict(subset(dict(row), 'id action action_code created_at'), details=details))
    return result


def audit(evidence_path, baseline_path):
    # This is a snapshot-specific evidence tool, not a general production reader.
    for actual, expected in ((evidence_path, EXPORT), (baseline_path, BASELINE)):
        if actual != expected or actual.is_symlink() or actual.resolve(strict=True) != expected:
            raise ValueError('Only the task-owned saved export and managed baseline are accepted')
    before = {str(p): file_hash(p) for p in (evidence_path, baseline_path)}
    evidence = json.loads(evidence_path.read_text())
    if not (evidence.get('query_only') is True and evidence.get('business_written') is False
            and evidence.get('service_controlled') is False and evidence.get('state_unchanged') is True):
        raise ValueError('Export is not labelled as an unchanged read-only capture')
    tables = evidence['tables']
    with closing(sqlite3.connect(baseline_path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        comparisons = []
        for table in TABLES:  # Only constant table names; retain hashes, not personal rows.
            saved = sorted(tables[table], key=lambda row: row['id'])
            columns = list(saved[0]) if saved else []
            copied = [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY id')]
            copied = [{k: row[k] for k in columns} for row in copied]
            comparisons.append(dict(table=table, export_count=len(saved), baseline_count=len(copied),
                export_sha256=digest(saved), baseline_sha256=digest(copied), equal=saved == copied))
        events = read_audit(db)
        query_only = db.execute('PRAGMA query_only').fetchone()[0] == 1
    if not all(item['equal'] for item in comparisons):
        raise ValueError('Saved export and managed baseline disagree on task-relevant facts')

    documents = evidence['files']
    runtime_path, runtime = single([(p, v) for p, v in documents.items()
                                   if '/layout_runtime/' in p], 'runtime document')
    draft_path, draft = single([(p, v) for p, v in documents.items()
                               if '/layout_drafts/' in p], 'draft document')
    floor = runtime['json']['floors']['1F']
    draft_floor = draft['json']['floors']['1F']
    if floor['revision'] != TARGET_REVISION:
        raise ValueError('Runtime evidence changed; this preview targets e919e36e2f3ec786')
    inventory = []
    for path, stored in documents.items():
        doc = stored['json']
        # Exact writer rendering checks the exported JSON against its original
        # byte SHA. A mismatch is evidence loss, not permission to recanonicalize.
        text = json.dumps(doc, ensure_ascii=False, indent=2) + '\n'
        encoding = next((name for name, rendered in (
            ('utf8_lf', text.encode()), ('utf8_crlf', text.replace('\n', '\r\n').encode()))
            if sha256(rendered).hexdigest() == stored['sha256']), None)
        inventory.append(dict(evidence_label=path, sha256=stored['sha256'], bytes=stored['bytes'],
            writer_bytes_match=encoding is not None, verified_writer_encoding=encoding,
            floor_revisions={k: f.get('revision') for k, f in doc.get('floors', {}).items()}))
    if not single([x for x in inventory if x['evidence_label'] == runtime_path], 'runtime checksum')['writer_bytes_match']:
        raise ValueError('Export cannot reproduce the recorded runtime bytes')
    sources = [x for x in inventory if x['floor_revisions'].get('1F') == SOURCE_REVISION]
    floor_by_id = {row['id']: row for row in tables['warehouse_floors']}
    area_by_id = {row['id']: row for row in tables['warehouse_areas']}
    layouts = {row['location_id']: row for row in tables['floor3_location_layouts']}
    affected = []
    for policy in tables['warehouse_area_storage_policies']:
        area = area_by_id[policy['area_id']]
        level = floor_by_id[area['floor_id']]
        if (area['construction_status'] != 'enabled' or level['construction_status'] != 'enabled'
                or policy['status'] != 'published'):
            continue
        code = str(level['floor_number']) + 'F'
        current = runtime['json']['floors'].get(code, {})
        if policy['published_map_revision'] == current.get('revision'):
            continue
        feature = single([f for f in current.get('features', [])
                          if f.get('id') == policy['map_feature_id']], 'bound feature')
        locations = [r for r in tables['warehouse_locations']
                     if r['warehouse_floor'] == level['floor_number'] and r['area_code'] == area['area_code']]
        conflicting_addresses = [r['id'] for r in locations if r.get('address_area_id') not in (None, area['id'])]
        location_ids = {r['id'] for r in locations}
        lots = [r for r in tables['inventory_lots'] if r['warehouse_location_id'] in location_ids]
        active_racks = [r for r in current.get('racks', []) if r.get('area_feature_id') == feature['id']]
        tombstones = [r for r in current.get('retired_racks', [])
                      if r.get('id') in DELETED_RACKS and r.get('area_feature_id') == feature['id']]
        feature_checks = dict(unique_area_code=sum(f.get('erp_area_code') == area['area_code']
                                 for f in current.get('features', [])) == 1,
            kind=feature.get('feature_kind') == 'zone', area_code=feature.get('erp_area_code') == area['area_code'],
            formal_name=feature.get('formal_area_name') == area['area_name'],
            formal_area=feature.get('formal_area_id') in (None, area['id']),
            formal_floor=feature.get('formal_floor_id') in (None, area['floor_id']),
            storage_layout=feature.get('storage_layout') == policy['storage_layout'],
            inventory_types=sorted(feature.get('allowed_inventory_types') or [])
                            == sorted(json.loads(policy['allowed_inventory_types_json'])),
            inside_measured_bounds=in_bounds(feature, current['bounds_mm']))
        affected.append(dict(area=subset(area, 'id floor_id area_code area_name construction_status'),
            policy=subset(policy, 'id version map_feature_id storage_layout status draft_map_revision published_map_revision'),
            runtime_revision=current.get('revision'), current_feature=feature,
            current_feature_sha256=digest(feature), current_identity_checks=feature_checks,
            source_document_matches=[x['evidence_label'] for x in sources],
            active_racks=[physical_rack(r, tables) for r in active_racks],
            recently_retired_racks=[physical_rack(r, tables) for r in tombstones],
            active_pallet_ids=[p['id'] for p in current.get('pallets', []) if p.get('area_feature_id') == feature['id']],
            location_count=len(locations), active_location_ids=[r['id'] for r in locations if r['is_active']],
            inactive_location_ids=[r['id'] for r in locations if not r['is_active']],
            conflicting_address_area_ids=conflicting_addresses,
            location_master_sha256=digest(locations), location_layout_sha256=digest(
                [layouts[r['id']] for r in locations if r['id'] in layouts]),
            ground_plans=[subset(p, 'id status published_map_revision draft_map_revision version target_slot_count')
                          for p in tables['warehouse_ground_layout_plans'] if p['area_id'] == area['id']],
            inventory_lot_count=len(lots), positive_available_lot_count=sum(r['quantity_available'] > 0 for r in lots),
            post_evidence_strategy='explicit_retirement' if tombstones else 'prove_unchanged_area_and_assets',
            eligible_for_repair=False, blocked_by=['exact_original_source_document_not_available'],
            proposed_business_changes=[]))
    affected.sort(key=lambda x: x['area']['id'])
    if len(affected) != 23 or any(x['policy']['published_map_revision'] != SOURCE_REVISION for x in affected):
        raise ValueError('Unexpected mismatch set; this task is fixed to 23 policies at 12069ae4137257d6')
    if sources:
        raise ValueError('Original source is now present; repeat a full source/target comparison before proposing repair')

    c_racks = []
    cell_catalog = {}
    for current_floor in runtime['json']['floors'].values():
        for rack in current_floor.get('racks', []):
            for cell in rack.get('mold_cells', []):
                cell_catalog.setdefault('MCELL-' + cell['id'].upper(), []).append(dict(
                    rack=subset(rack, 'id name area_code x_mm y_mm'),
                    cell=subset(cell, 'id level grid alias')))
    for rack_id in DELETED_RACKS:
        retired = single([r for r in floor.get('retired_racks', []) if r['id'] == rack_id], 'retired rack')
        pending = [r for r in draft_floor.get('racks', []) if r['id'] == rack_id]
        cell_addresses = {'MCELL-' + c['id'].upper() for key in ('mold_cells', 'retired_mold_cells')
                          for c in retired.get(key, [])}
        movements = [m for m in tables['mold_location_movements']
                     if m['from_location'] in cell_addresses or m['to_location'] in cell_addresses]
        related_tool_ids = {m['mold_tool_id'] for m in movements}
        related_tools = [m for m in tables['mold_tools'] if m['id'] in related_tool_ids]
        now_locations = sorted({m['rack_location'] for m in related_tools})
        c_racks.append(dict(runtime=physical_rack(retired, tables),
            draft_active=[physical_rack(r, tables) for r in pending],
            historical_movement_count=len(movements),
            last_three_movements=[subset(m, 'id mold_tool_id from_location to_location moved_at') for m in movements[-3:]],
            current_locations_of_historically_related_molds=[dict(encoded_location=loc,
                mold_tool_ids=[m['id'] for m in related_tools if m['rack_location'] == loc],
                current_map_cell_matches=cell_catalog.get(loc, [])) for loc in now_locations],
            draft_changed_fields={k: {'published_tombstone': retired.get(k), 'draft': pending[0].get(k)}
                                  for k in sorted(set(retired) | set(pending[0]))
                                  if retired.get(k) != pending[0].get(k) and k not in ('restoration_history',)
                                  } if pending else {},
            physical_move_confirmed=False, draft_is_not_formal_location_evidence=True))
    fin = []
    for item in affected:
        if item['area']['area_code'] not in ('FIN-001', 'FIN-002', 'FIN-003'):
            continue
        ids = set(item['active_location_ids'] + item['inactive_location_ids'])
        fin.append(dict(area_id=item['area']['id'], area_code=item['area']['area_code'],
            feature_id=item['policy']['map_feature_id'], points=item['current_feature']['points'],
            locations=[dict(subset(r, 'id location_code is_active address_area_id ground_row_no slot_no'),
                       projection=subset(layouts.get(r['id'], {}), 'left_pct top_pct width_pct height_pct layout_kind'))
                       for r in tables['warehouse_locations'] if r['id'] in ids]))
    after = {str(p): file_hash(p) for p in (evidence_path, baseline_path)}
    if before != after:
        raise ValueError('An input changed during read-only audit')
    selected_events = []
    for event in events:
        details = event['details']
        if 'rack' in details:
            rack = details['rack']
            details = dict(details, rack=dict(subset(rack, 'id name area_code area_feature_id version'),
                                             object_sha256=digest(rack)))
        selected_events.append(dict(event, details=details))
    result = dict(schema='tianming.map-binding-readonly-preview.v1', evidence_at=evidence['at'],
        no_production_access=True, business_written=False, service_controlled=False,
        input_sha256_before=before, input_sha256_after=after, inputs_unchanged=True,
        sqlite_query_only=query_only, sqlite_immutable=True, compared_tables=comparisons,
        evidence_limitations=dict(truncated=evidence.get('truncated'), complete_history=False,
            original_source_found=False, source_layout_id_not_proven=True,
            physical_site_confirmation_available=False, historical_paths_are_labels_only=True),
        document_count=len(documents), document_inventory=inventory,
        runtime=dict(evidence_label=runtime_path, sha256=runtime['sha256'],
                     floor=subset(floor, 'floor_code revision layout_id source_name source_sha256 source_updated_at bounds_mm layout_edited_at')),
        draft=dict(evidence_label=draft_path, sha256=draft['sha256'],
                   floor_revision=draft_floor['revision'], draft_meta=draft['json'].get('draft_meta')),
        mismatch_count=len(affected), eligible_for_repair_count=0, affected_areas=affected,
        strategy_counts=dict(Counter(x['post_evidence_strategy'] for x in affected)),
        c_e_rack_identity_review=c_racks, distinct_finished_areas=fin, audit_chain=selected_events,
        required_additional_evidence=[
            {'kind': 'exact_original_published_document', 'floor_revision': SOURCE_REVISION,
             'whole_document_sha256': 'e79feaba1ab0443a1ef7a337a10e7d3615a8536d21298a0af20161a390da521b',
             'source_audit_ids': [41400, 41401]},
            {'kind': 'prior_document_for_audited_chain_not_a_substitute_for_exact_source',
             'backup_name': 'twin_layout_v1.before_20261013_030237_891851_6c6875b3af26.json',
             'sha256': '6c6875b3af262a5a24ee5992adbf7d30ac1bd4db5dd5cb652558a285e0ae7e27'}],
        proposed_business_changes=[])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, default=EXPORT)
    parser.add_argument('--database-copy', type=Path, default=BASELINE)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.absolute()
    if (not output.is_relative_to(EVIDENCE_ROOT / 'map-binding-preview')
            or output.resolve(strict=False) != output or output.is_symlink()):
        raise ValueError('Output must be a new file in the task map-binding-preview directory')
    result = audit(args.evidence, args.database_copy)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if len(rendered.encode()) > 250_000:
        raise ValueError('Bounded preview exceeded 250 KB; reduce fields before writing')
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.resolve() != output.parent:
        raise ValueError('Output directory must not traverse a symlink')
    with output.open('x', encoding='utf-8') as stream:
        stream.write(rendered)
    print(json.dumps(dict(output=str(output), bytes=len(rendered.encode()),
        mismatches=result['mismatch_count'], eligible=result['eligible_for_repair_count'],
        inputs_unchanged=result['inputs_unchanged'], sha256=file_hash(output)), ensure_ascii=False))


if __name__ == '__main__':
    main()

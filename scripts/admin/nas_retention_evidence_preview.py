"""Read previously captured local metadata; no NAS traversal or apply option."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path


def preview(evidence: Path) -> dict:
    sources = {}
    values = {}
    for name in ('nas-sizes.json', 'nas-top-level.json', 'NAS容量与两份备份清理方案.txt'):
        raw = (evidence / name).read_bytes()
        sources[name] = {'path': str(evidence / name), 'sha256': hashlib.sha256(raw).hexdigest()}
        if name.endswith('.json'): values[name] = json.loads(raw)
    sizes = values['nas-sizes.json']
    root = Path(sizes['root'])
    entries = []
    by_category = defaultdict(lambda: {'files': 0, 'bytes': 0})
    for row in sizes['large']:
        path = Path(row['path'])
        relative = path.relative_to(root).as_posix()
        parts = relative.split('/')
        if len(parts) < 2 or '..' in parts:
            raise ValueError('evidence path outside BoxERP child scope')
        if parts[:2] in (['backups', 'tianming-backup-work'], ['backups', '.tianming-backup-work']):
            category = 'backup_work_payload_candidate'
        elif relative.endswith(('.pending', '.partial', '.tmp')):
            category = 'unfinished_payload_candidate'
        elif relative.endswith('.tmbackup'):
            category = 'encrypted_backup_candidate'
        elif parts[0] == 'desktop-assistant' and relative.endswith(('.zip', '.exe')):
            category = 'program_distribution_candidate'
        elif parts[0] in ('archives', 'development-archives'):
            category = 'archive_payload_unclassified'
        else:
            category = 'unclassified'
        by_category[category]['files'] += 1
        by_category[category]['bytes'] += row['bytes']
        entries.append({'path': relative, 'bytes': row['bytes'], 'mtime': row['mtime'],
                        'metadata_category': category, 'action': 'retain_blocked',
                        'reason': 'Metadata only: no authenticated identity, classification/closure or active-reference proof.'})
    # Names merely identify verification candidates, never recovery success/order.
    groups = defaultdict(list)
    for entry in entries:
        if entry['metadata_category'] == 'encrypted_backup_candidate':
            groups[Path(entry['path']).name].append(entry)
    candidates = [{'filename': name, 'locations': groups[name], 'status': 'unverified_name_candidate'}
                  for name in sorted(groups, reverse=True)[:2]]
    return {'type': 'tianming.nas-retention-evidence-preview.v1', 'captured_at': sizes['at'],
            'root': sizes['root'], 'sources': sources, 'status': 'blocked',
            'global_target_versions': 2, 'verified_keep_versions': [],
            'latest_named_candidate_groups': candidates,
            'missing_gates': ['Two distinct independently remote-read and completely recovered business points',
                             'Authenticated byte SHA and current file identity for exact full inventory',
                             'Fixed storage/active-backup contract and independent NAS identity',
                             'Current/previous/schema-authority/migration-target/recovery-source references',
                             'Formal material/drawing/knowledge/key references and absence evidence',
                             'No active uploads; stale jobs require authenticated owner/closure and no active references',
                             'Serial exclusive writer lease; reviewed plan and all evidence freshly rechecked'],
            'logical_scope_bytes': sum(v['bytes'] for v in sizes['summary'].values()),
            'metadata_subset_files': len(entries),
            'metadata_subset_bytes': sum(e['bytes'] for e in entries),
            'metadata_categories': dict(sorted(by_category.items())),
            'backups_direct_files': values['nas-top-level.json'],
            'authorized_delete_bytes': 0, 'delete': [], 'entries': sorted(entries, key=lambda e: e['path']),
            'limits': ['large contains only the previous scan large-file subset, not a complete deletion manifest',
                       'mtime and names are not verification or job closure evidence',
                       'Logical file bytes exclude NAS snapshots/recycle bin and root standalone business files',
                       'No NAS read, write, deletion, service control or credential operation performed']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Output must be local task evidence; never permit overwriting source captures.
    source = args.evidence_dir.resolve(strict=True)
    output = args.output.absolute()
    if output.exists() and output.is_symlink(): raise ValueError('output cannot be a link')
    output = output.resolve()
    if source == output or source in output.parents: raise ValueError('do not overwrite original evidence')
    if any(part in ('ZSPACE', 'BoxERP') for part in output.parts):
        raise ValueError('preview output must stay off NAS')
    result = preview(source)
    entries = result.pop('entries')
    # One exact path per line makes a large evidence subset easier to review.
    head = json.dumps(result, ensure_ascii=False, indent=2)
    rows = ',\n'.join('    ' + json.dumps(row, ensure_ascii=False) for row in entries)
    output.write_text(head[:-2] + ',\n  "entries": [\n' + rows + '\n  ]\n}\n', encoding='utf-8')


if __name__ == '__main__': main()

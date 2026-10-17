"""Conservative decisions shared by the fixed-scope Mac maintenance jobs."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat

TYPE = 'tianming.local-maintenance-policy.v1'
ROLES = ('current', 'previous', 'schema_authority', 'migration_target',
         'restored_release_sha256', 'restored_schema_authority_sha256')
IDENTITY = re.compile(r'[0-9a-f]{64}')

def fingerprint(path):
    s = Path(path).lstat()
    return [s.st_dev, s.st_ino, s.st_uid, s.st_gid, s.st_mode, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns]

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()

def stable_file(path, *, uid, device=None):
    path = Path(path)
    s = path.lstat()
    if (not stat.S_ISREG(s.st_mode) or s.st_uid != uid or s.st_nlink != 1
            or (device is not None and s.st_dev != device)):
        raise ValueError('File ownership, kind, link count or filesystem changed')
    return fingerprint(path)

def unlink_verified(path, expected, *, before_unlink=None):
    """Never follow a swapped link or delete a file changed after verification."""
    path = Path(path)
    if fingerprint(path) != expected:
        raise ValueError('Verified file changed')
    if before_unlink:
        before_unlink()
    if fingerprint(path) != expected:
        raise ValueError('Verified file changed before unlink')
    path.unlink()

def protected_programs(state, manifests, *, additional=(), point_programs=()):
    if not IDENTITY.fullmatch(state.get('current', '')):
        raise ValueError('Current program identity is missing')
    keep = {state[k] for k in ROLES if state.get(k)} | set(additional) | set(point_programs)
    if any(not isinstance(h, str) or not IDENTITY.fullmatch(h) for h in keep):
        raise ValueError('Invalid protected program role')
    pending = list(keep)
    seen = set()
    while pending:
        h = pending.pop()
        if h in seen:
            continue
        seen.add(h)
        manifest = manifests.get(h)
        if manifest is None:
            # A portable original can live outside published; its identity is
            # protected even when it has no local expansion.
            continue
        fallback = (manifest.get('migration') or {}).get('rollback_package_sha256')
        if fallback:
            if not isinstance(fallback, str) or not IDENTITY.fullmatch(fallback):
                raise ValueError('Invalid signed fallback identity')
            if fallback not in keep:
                keep.add(fallback)
                pending.append(fallback)
    return keep

def maintenance_admitted(state, *, running, healthy):
    operation = state.get('operation', '')
    return bool(running and healthy and state.get('current')
                and not state.get('manual_stop') and not state.get('onboarding_pending')
                and not state.get('backup_work_retained')
                and operation in ('update', 'rollback', 'migration_recovered'))

def receipt_valid(receipt, identity, git_sha):
    return (isinstance(receipt, dict)
            and receipt.get('type') == 'tianming.runtime-delivery.v1'
            and receipt.get('package_sha256') == identity
            and receipt.get('git_sha') == git_sha
            and receipt.get('runtime_verified') is True
            and receipt.get('technical_delivery_complete') is True)

def point_retirements(points, *, keep, new_verified):
    """Unknown/unverified points never enter automatic rotation."""
    if not new_verified or type(keep) is not int or keep < 1:
        return []
    candidates = [p for p in points if p.get('verified') is True
                  and isinstance(p.get('created'), str)]
    candidates.sort(key=lambda p: (p['created'], p['path']), reverse=True)
    return candidates[keep:]

def full_point_due(previous, now, interval_seconds, *, force=False):
    if force or previous is None:
        return True
    if type(interval_seconds) is not int or interval_seconds < 900:
        raise ValueError('Backup interval is invalid')
    return (now - previous).total_seconds() >= interval_seconds

def tree_fingerprint(root, *, uid, device, allow_links=()):
    root = Path(root)
    allowed = {str(Path(p)) for p in allow_links}
    rows = []
    allocated = 0
    count = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        for name in ['.', *sorted(dirs + names)]:
            p = Path(folder) if name == '.' else Path(folder) / name
            s = p.lstat()
            if s.st_dev != device or s.st_uid not in {0, uid} or name == '.git':
                raise ValueError('Tree boundary or ownership changed')
            if stat.S_ISLNK(s.st_mode):
                if str(p) not in allowed:
                    raise ValueError('Unknown tree link')
            elif not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)):
                raise ValueError('Special file in tree')
            if stat.S_ISREG(s.st_mode):
                allocated += s.st_blocks * 512
                count += 1
            rows.append([str(p.relative_to(root)), *fingerprint(p)])
    return {'sha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            'allocated': allocated, 'files': count}


def unsigned_equivalent(path, manifest):
    import zipfile
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest['files']) | {'manifest.json'}:
            return False
        if json.loads(z.read('manifest.json')) != manifest:
            return False
        for name, expected in manifest['files'].items():
            h = hashlib.sha256()
            with z.open(name) as f:
                for block in iter(lambda: f.read(1024**2), b''):
                    h.update(block)
            if h.hexdigest() != expected:
                return False
    return True


def monthly_programs(rows, *, limit=12):
    """One delivered original per month, separate from mandatory runtime roles."""
    selected = {}
    for row in sorted(rows, key=lambda r: (r['created'], r['identity']), reverse=True):
        month = row['created'][:7]
        if month not in selected and len(selected) < limit:
            selected[month] = row['identity']
    return set(selected.values())

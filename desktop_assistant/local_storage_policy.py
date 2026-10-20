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


def expanded_programs(state, *, additional=(), point_programs=()):
    """Live/recovery roles need expansions; transitive fallbacks need originals.

    An old signed fallback archive remains protected by protected_programs.
    Its reproducible expansion is not an additional running or restore role.
    """
    return protected_programs(state, {}, additional=additional,
                              point_programs=point_programs)


def rehearsal_payload_scope(report, *, delivered, ancestor, later_passed):
    """Keep reports; retire only terminal, provably superseded payloads."""
    if (report.get('scope')!='isolated_additive_rehearsal_only'
            or not isinstance(report.get('package_sha256'),str)
            or not IDENTITY.fullmatch(report['package_sha256'])):
        return None
    if report.get('status')=='passed':
        if (report.get('source_unchanged') is True
                and report.get('schema_fingerprint_version')==2
                and isinstance(report.get('result_schema'),str)
                and IDENTITY.fullmatch(report['result_schema'])
                and (delivered or ancestor)):
            return 'release-and-shared'
    # Only transient inconsistent copies superseded by a later successful run
    # of the exact same package; never general migration/structure failures.
    errors={'复制期间数据变化或附件不一致，请重新演练',
            '原系统数据已变化，本次演练结果不能用于更新'}
    if (report.get('status')=='failed' and report.get('error') in errors
            and delivered and later_passed):
        return 'shared-only'
    return None

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


def completed_delivery_proof(record, manifest, *, manifest_sha256):
    """A closed protected release report can attest delivery after wrapper failure.

    Callers must admit root ownership, immutable identity and bounded JSON first.
    Publication/admission alone, health alone and checkout notes are insufficient.
    """
    if not isinstance(record, dict) or not isinstance(manifest, dict):
        return False
    tx, service, closure = (record.get(k) for k in ('transaction','service_result','closure'))
    if not all(isinstance(v, dict) for v in (tx, service, closure)):
        return False
    identity, commit = tx.get('target'), manifest.get('git_sha')
    if not (isinstance(identity, str) and IDENTITY.fullmatch(identity)
            and isinstance(commit, str) and re.fullmatch('[0-9a-f]{40}',commit)):
        return False
    return (record.get('status') == 'technical_release_and_native_closure_complete'
            and record.get('healthy') is True and record.get('manual_stop') is False
            and record.get('backup_work_retained') is False
            and record.get('operation') == 'update'
            and tx.get('commit') == commit and service.get('commit') == commit
            and tx.get('manifest_sha256') == manifest_sha256
            and tx.get('version') == service.get('version') == manifest.get('version')
            and tx.get('revision') == service.get('revision') == manifest.get('revision')
            and service.get('package') == identity
            and isinstance(service.get('backup_sha256'), str)
            and bool(IDENTITY.fullmatch(service['backup_sha256']))
            and closure.get('status') in ('closed', 'already_closed')
            and (closure.get('status') != 'closed' or (
                closure.get('current') == identity and closure.get('backup_preserved') is True
                and closure.get('backup_sha256') == service['backup_sha256'])))


def maintenance_exit_code(action, result):
    # launchd retries a contended cleanup after ThrottleInterval. A successful
    # check retains its hourly interval, and backup keeps its own cadence.
    if action == 'cleanup' and result.get('status') == 'maintenance-busy':
        return 75
    return 1 if result.get('status') == 'failed' else 0


def managed_copy_identity(target, *, owner_uid, device):
    """Match the ordinary runner's seal without changing its privilege boundary."""
    from desktop_assistant.cleanup import inspect_tree, local_path
    target=local_path(Path(target));rows=[];size=0
    for path in (target,*sorted(inspect_tree(target))):
        st=path.lstat()
        if (st.st_uid != owner_uid or st.st_dev != device
                or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))
                or (stat.S_ISREG(st.st_mode) and st.st_nlink != 1)):
            raise ValueError('Managed test copy owner/type/device changed')
        rows.append([str(path.relative_to(target)),st.st_dev,st.st_ino,st.st_mode,
                     st.st_size,st.st_mtime_ns,st.st_ctime_ns])
        if path.is_file():size+=st.st_size
    return dict(fingerprint=hashlib.sha256(json.dumps(rows).encode()).hexdigest(),logical_bytes=size)


def sealed_test_record(record, job):
    job=Path(job)
    return (isinstance(record,dict) and bool(re.fullmatch('task-[a-f0-9]{32}',job.name))
            and record.get('schema')=='tianming.test-task.v1'
            and record.get('job')==str(job) and record.get('copies')==str(job/'copies')
            and record.get('status')=='passed' and record.get('returncode')==0
            and record.get('keep_copy') is False
            and record.get('manual_acceptance_pending') is False
            and isinstance(record.get('copy_identity'),dict)
            and isinstance(record['copy_identity'].get('fingerprint'),str)
            and bool(IDENTITY.fullmatch(record['copy_identity']['fingerprint'])))


def activation_program_dependencies(record, body, current):
    """Activation approval identities remain native runtime dependencies.

    mac_factory_runtime._configuration checks these historical approvals on
    every new Manager environment, even when none is the current program.
    Malformed authority must stop reclamation, never yield an empty set.
    """
    try:
        request = record['request']
        required = ({request['execution_package_sha256'], request['monitor_package_sha256'],
                     body['preparation']['restore']['restore_package_sha256']} if current else
                    {request['package_sha256']})
    except (KeyError, TypeError) as error:
        raise ValueError('Activation program dependencies are incomplete') from error
    if any(not isinstance(h, str) or not IDENTITY.fullmatch(h) for h in required):
        raise ValueError('Activation program identity is invalid')
    return required


def program_retention_sets(state, manifests, *, runtime_dependencies=(), archive_dependencies=(), point_programs=()):
    """Historical proof hashes preserve originals, not additional runtime trees."""
    originals=protected_programs(state,manifests,
        additional=set(runtime_dependencies)|set(archive_dependencies),point_programs=point_programs)
    expansions=expanded_programs(state,additional=runtime_dependencies,point_programs=point_programs)
    return originals,expansions

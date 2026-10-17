"""Fixed local maintenance worker, installed root-owned outside signed ERP trees."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile
# -I omits the script directory; only protected installed assets and the
# selected interpreter's signed migration tree are admitted.
sys.path.insert(0, str(Path(__file__).resolve().parent))
_runtime_tree = Path(sys.executable).absolute().parent.parent.parent
if str(_runtime_tree).startswith('/Library/TianmingERP/published/'):
    sys.path.insert(1, str(_runtime_tree))
try:
    import storage_policy as policy
except ModuleNotFoundError:
    if Path(__file__).resolve().parent==Path('/Library/TianmingERP-LocalMaintenance'):
        raise
    from desktop_assistant import local_storage_policy as policy
try:
    import file_points
except ModuleNotFoundError:
    if Path(__file__).resolve().parent==Path('/Library/TianmingERP-LocalMaintenance'):
        raise
    from desktop_assistant import incremental_file_points as file_points

BASE = Path('/Library/TianmingERP')
ROOT = BASE/'instance'
INSTALL = Path('/Library/TianmingERP-LocalMaintenance')
BUILDS = Path('/Users/kimiyf/ERP-Migration/builds')
GIT = Path('/Users/kimiyf/ERP-Migration/repository')
POINTS = Path('/Library/TianmingERP-OnlinePoints')


def read(path):
    return json.loads(Path(path).read_bytes())


def write(path, value, *, mode=0o600):
    path = Path(path)
    import uuid
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    # launchd's restrictive umask must not hide explicitly public metadata from
    # the dedicated service/developer children. Private records stay 0600.
    os.fchmod(fd, mode)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory_fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def config():
    p = INSTALL/'policy.json'
    s = p.lstat()
    if s.st_uid != 0 or s.st_mode & 0o022 or p.is_symlink():
        raise ValueError('Maintenance policy is not protected')
    v = read(p)
    if (v.get('type') != policy.TYPE or type(v.get('cleanup_enabled')) is not bool
            or type(v.get('backup_enabled')) is not bool
            or type(v.get('apply_cleanup')) is not bool
            or v.get('database_keep') != 3 or v.get('data_keep') != 1
            or v.get('database_interval_seconds') not in (900, 3600, 21600, 86400)
            or v.get('data_interval_seconds') not in (0, 21600, 86400)
            or v.get('monthly_program_keep') != 12
            or v.get('minimum_free_bytes') != 150 * 10**9):
        raise ValueError('Maintenance policy fields are invalid')
    if (policy.digest(BASE/'keychain-context.json')!=v.get('context_sha256')
            or policy.digest(BASE/'publisher-public.pem')!=v.get('publisher_key_sha256')):
        raise ValueError('Maintenance installation identity changed')
    return v


def health():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5) as r:
            return r.status == 200 and json.load(r).get('ok') is True
    except Exception:
        return False


def program(identity, public):
    from desktop_assistant.storage import signed_release_manifest
    p = BASE/'published'/identity/'package.zip'
    fp=policy.fingerprint(p)
    cache_path=INSTALL/'archive-fingerprints.json'
    cache=read(cache_path) if cache_path.is_file() else {}
    if cache.get(identity)!=fp:
        if policy.digest(p)!=identity: raise ValueError('Published original changed')
        cache[identity]=fp;write(cache_path,cache,mode=0o644)
    return p, signed_release_manifest(p, public)


def process_values():
    import psutil
    result = []
    for process in psutil.process_iter(['pid', 'name']):
        if process.pid == os.getpid():
            continue
        try:
            result.extend([process.exe(), process.cwd(), *process.cmdline()])
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            if any(x in (process.info['name'] or '').lower() for x in ('python', 'tianming')):
                raise ValueError('Opaque ERP process protects cleanup targets')
    return result


def delivery_receipts(manifests, current, *, now):
    """Only structured successful native release events close a candidate."""
    receipts = INSTALL/'deliveries'
    receipts.mkdir(mode=0o755, exist_ok=True)
    for folder in Path('/Library').glob('TianmingERP-*'):
        if folder == INSTALL or folder.is_symlink():
            continue
        try:
            logs = [p for p in folder.iterdir() if p.suffix == '.stdout' and p.is_file()
                    and not p.is_symlink() and p.stat().st_size < 8 * 1024**2]
        except PermissionError:
            continue
        for log in logs:
            for line in log.read_text(errors='replace').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get('phase') != 'technical_release_complete':
                    continue
                h = event.get('package') or event.get('package_sha256')
                if not isinstance(h,str) or h not in manifests:
                    continue
                target = receipts/(h+'.json')
                if not target.exists():
                    write(target, dict(type='tianming.runtime-delivery.v1', package_sha256=h,
                          git_sha=manifests[h]['git_sha'], runtime_verified=True,
                          technical_delivery_complete=True, recorded_at=now,
                          source=str(log), source_sha256=policy.digest(log)), mode=0o644)
    return {p.stem: read(p) for p in receipts.glob('*.json') if not p.is_symlink()}


def database_path_references():
    import sqlite3
    result = []
    database = ROOT/'shared/data/carton_erp.sqlite3'
    quote = lambda s: '"'+s.replace('"', '""')+'"'
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=10) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            columns = [r[1] for r in db.execute('PRAGMA table_info('+quote(table)+')')
                       if 'path' in r[1].lower() or r[1].lower() in ('filename', 'file_name', 'source_file')]
            for col in columns:
                for (value,) in db.execute('SELECT DISTINCT '+quote(col)+' FROM '+quote(table)
                                          +' WHERE '+quote(col)+' LIKE ?', ('/Library/TianmingERP/%',)):
                    if isinstance(value, str):
                        result.append(value)
        db.rollback()
    return result


def development_program_references():
    """Read shallow local venv metadata/links, never traverse mounted links."""
    result=set()
    pattern=r'/Library/TianmingERP/published/([0-9a-f]{64})'
    base=GIT.parent
    folders=[base]
    folders.extend(p for p in base.iterdir() if not p.is_symlink() and p.is_dir())
    if BUILDS.is_dir() and not BUILDS.is_symlink():
        folders.extend(p for p in BUILDS.iterdir() if not p.is_symlink() and p.is_dir())
    for folder in folders:
        config_paths=[folder/'pyvenv.cfg']
        for p in folder.iterdir():
            if p.is_symlink():
                target=os.path.normpath(str(p.parent/os.readlink(p)))
                result.update(re.findall(pattern,target))
            elif p.is_dir():
                config_paths.append(p/'pyvenv.cfg')
        for p in config_paths:
            try:s=p.lstat()
            except FileNotFoundError:continue
            if p.is_symlink() or not stat.S_ISREG(s.st_mode) or s.st_uid not in (0,501) or s.st_size>16384:
                raise ValueError('Unknown development runtime metadata protects cleanup')
            result.update(re.findall(pattern,p.read_text()))
    return result


def completed_points():
    p = POINTS
    if not p.is_dir():
        return []
    result = []
    for receipt in p.glob('*.json'):
        try:
            value = read(receipt)
            point = p/value['name']
            if (point.parent != p or point.is_symlink() or not point.is_file()
                    or value.get('type') != 'tianming.automatic-online-point.v1'
                    or value.get('verified') is not True
                    or policy.digest(point) != value['sha256']):
                continue
            result.append(dict(value, path=str(point), receipt=str(receipt)))
        except (ValueError, KeyError, OSError):
            continue
    return result


def worker_child(action):
    return subprocess.run([sys.executable, '-I', '-B', str(INSTALL/'native.py'), action],
             user=499, group=499, extra_groups=[499], cwd='/', stdin=subprocess.DEVNULL,
             capture_output=True, timeout=1200,
             env={'HOME':str(BASE/'home'), 'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
                  'LANG':'en_US.UTF-8', 'ERP_HOME_REHEARSAL':'1'})


def backup_probe(manager):
    from desktop_assistant import local_backup_only as local
    local.enabled(manager)
    _, context, _ = local._authority()
    state = manager.state
    index = state['last_backup_local']
    receipt = local._owned_read(local._receipt_path(ROOT, index['sha256']), context)
    matches = []
    for job in (ROOT/'staging').iterdir():
        if re.fullmatch('local-(?:code-update|backup)-[a-f0-9]{32}', job.name):
            p = job/'verified.json'
            if p.is_file():
                v = local._owned_read(p, context)
                if v.get('backup_sha256') == index['sha256']:
                    matches.append(v)
    if len(matches) != 1:
        raise ValueError('Latest complete recovery lacks its original proof')
    point = local._check_proof(manager, matches[0], context)
    if (receipt['sha256'] != index['sha256'] or str(point) != state['last_backup']
            or receipt['authenticated_recovery'] != matches[0]['authenticated_recovery']):
        raise ValueError('Complete recovery publication changed')
    paths, hashes = local._active_references(manager, context)
    settings=config();backups=[]
    protected_hashes={state.get('restored_backup_sha256'),state.get('migration_backup_sha256'),index['sha256'],*hashes}
    bindings=read(INSTALL/'program-bindings.json') if (INSTALL/'program-bindings.json').is_file() else {}
    for p in (ROOT/'backups').glob('*.tmbackup'):
        fp=policy.stable_file(p,uid=499);h=policy.digest(p)
        protected=h in protected_hashes or str(p) in paths or str(p)==state.get('update_backup')
        recognized=False
        rp=local._receipt_path(ROOT,h)
        if rp.is_file():
            v=local._owned_read(rp,context)
            recognized=(v.get('type')==local.RECEIPT and v.get('sha256')==h
                and v.get('authenticated_recovery',{}).get('cipher_sha256')==h)
        if h in settings.get('legacy_backup_programs',{}):
            recognized=settings['legacy_backup_programs'][h]['name']==p.name
        programs=settings.get('legacy_backup_programs',{}).get(h,{}).get('programs')
        if programs is None and h in bindings and bindings[h].get('identity')==fp:
            programs=bindings[h]['programs']
        if programs is None and protected:
            # Authenticate the complete encrypted source, then stream only its
            # embedded program members. No business content goes to evidence.
            import tempfile
            from desktop_assistant.storage import decrypt_file
            from desktop_assistant.credential_store import unprotect_backup
            check_point_directory()
            password=unprotect_backup(read(ROOT/'preferences.json')['protected_password'])
            try:
                with tempfile.TemporaryDirectory(prefix='program-binding-',dir=POINTS) as temporary:
                    raw=Path(temporary)/'recovery.zip'
                    if decrypt_file(p,raw,password)!=h:raise ValueError('Cipher authentication changed')
                    with zipfile.ZipFile(raw) as z:
                        if z.getinfo('manifest.json').file_size>8*1024**2:raise ValueError('Oversize recovery manifest')
                        m=json.loads(z.read('manifest.json'))
                        if m.get('type')!='tianming.recovery.v1':raise ValueError('Unknown recovery format')
                        programs=[]
                        import hashlib
                        for name in ('release.zip','compatibility.zip','restored-release.zip','restored-schema-authority.zip'):
                            expected=m['files'].get(name)
                            if expected is not None:
                                digest=hashlib.sha256()
                                with z.open(name) as member:
                                    for chunk in iter(lambda:member.read(4*1024**2),b''):digest.update(chunk)
                                if digest.hexdigest()!=expected or not policy.IDENTITY.fullmatch(expected):
                                    raise ValueError('Embedded signed-program identity mismatch')
                                programs.append(expected)
                        if not programs:raise ValueError('Recovery has no program binding')
                    if policy.fingerprint(p)!=fp or policy.digest(p)!=h:raise ValueError('Recovery changed')
            finally:password=None
        backups.append(dict(path=str(p),sha256=h,identity=fp,protected=protected,
                            recognized=recognized,programs=programs))
    return dict(verified=True, path=str(point), sha256=index['sha256'],
                active_paths=paths, active_hashes=hashes,backups=backups,
                state_sha256=policy.digest(ROOT/'state.json'))


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def check_point_directory():
    if (POINTS.is_symlink() or not POINTS.is_dir() or POINTS.stat().st_uid!=499
            or stat.S_IMODE(POINTS.stat().st_mode)!=0o700):
        raise ValueError('Protected online point directory missing')


def service_action(action):
    if os.geteuid() != 499:
        raise ValueError('Dedicated service identity required')
    from desktop_assistant.manager import Manager
    from desktop_assistant.credential_store import unprotect_backup
    manager = Manager(ROOT, (BASE/'publisher-public.pem').read_bytes())
    if action == 'service-proof':
        return backup_probe(manager)
    if action == 'service-verify-pair':
        import tempfile
        check_point_directory()
        with manager.lock():
            point=max((p for p in completed_points() if p['kind']=='database'),key=lambda p:p['created'])
            password=unprotect_backup(read(ROOT/'preferences.json')['protected_password'])
            try:
                with tempfile.TemporaryDirectory(prefix='isolated-proof-',dir=POINTS) as temporary:
                    answer=file_points.restore_isolated(point,POINTS,password,Path(temporary)/'shared',
                        ROOT/'packages'/(point['program_sha256']+'.zip'),manager.public_key)
                    # No customer contents, DB counts or paths enter public proof.
                    answer.pop('database',None)
                    return dict(answer,point_name=point['name'],point_sha256=point['sha256'],
                                program_sha256=point['program_sha256'])
            finally:password=None
    settings = config()
    if not settings['backup_enabled']:
        return {'status':'frequency-pending'}
    from desktop_assistant import data_recovery
    from desktop_assistant.operation_lock import BUSY
    try:
        with manager.lock():
            state = manager.state
            if state.get('current')!=_runtime_tree.parent.name:
                return {'status':'runtime-changed'}
            if not policy.maintenance_admitted(state, running=manager._process(), healthy=health()):
                return {'status':'maintenance-attention'}
            output_dir = POINTS
            if output_dir.is_symlink():
                raise ValueError('Point directory is a link')
            check_point_directory()
            now = utc_now()
            results = []
            password = None
            try:
                for kind, interval, keep in (('database', settings['database_interval_seconds'], 3),
                                              ('data', settings['data_interval_seconds'], 1)):
                    if interval == 0:
                        continue
                    points = [p for p in completed_points() if p['kind'] == kind]
                    latest = max((dt.datetime.fromisoformat(p['created']) for p in points), default=None)
                    if not policy.full_point_due(latest, now, interval):
                        results.append({'kind':kind, 'status':'up-to-date'})
                        continue
                    if shutil.disk_usage(ROOT).free < settings['minimum_free_bytes']+(10 if kind=='data' else 1)*1024**3:
                        results.append({'kind':kind, 'status':'insufficient-workspace'})
                        continue
                    if password is None:
                        password = unprotect_backup(read(ROOT/'preferences.json')['protected_password'])
                    import uuid
                    suffix = '.tmdb' if kind == 'database' else '.tmdata'
                    name = now.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+suffix
                    output = output_dir/name
                    try:
                        attachment_set=None;incremental=None
                        if kind=='database':
                            attachment_set,before,incremental=file_points.capture(ROOT/'shared',POINTS,password)
                        result = data_recovery.create(ROOT/'shared', ROOT/'packages'/(state['current']+'.zip'),
                               output, password, manager.public_key, database_only=kind=='database',
                               status_path=output_dir/'last-attempt.json')
                        if manager.state != state or not result.get('restored_verified'):
                            raise ValueError('State changed during verified point creation')
                        if kind=='database' and file_points.fingerprint(ROOT/'shared')!=before:
                            raise ValueError('Business files changed while database point was created')
                        receipt = dict(type='tianming.automatic-online-point.v1', name=name,
                              kind=kind, created=now.isoformat(), sha256=result['backup_sha256'],
                              bytes=result['backup_bytes'], program_sha256=result['program_sha256'],
                              format=result['format'], verified=True, program_embedded=False)
                        if attachment_set:receipt['attachment_set']=attachment_set
                        write(output.with_suffix(output.suffix+'.json'), receipt)
                        # Rotation begins only after durable verified receipt.
                        retired = []
                        for old in policy.point_retirements([p for p in completed_points() if p['kind']==kind],
                                                            keep=keep, new_verified=True):
                            old_path = Path(old['path'])
                            fp = policy.stable_file(old_path, uid=499)
                            if policy.digest(old_path) != old['sha256']:
                                continue
                            policy.unlink_verified(old_path, fp)
                            Path(old['receipt']).unlink()
                            retired.append(old_path.name)
                        results.append({'kind':kind, 'status':'verified', 'name':name,
                                        'bytes':result['backup_bytes'], 'retired':retired,
                                        'incremental':incremental})
                        if kind=='database':
                            all_points=completed_points()
                            declared=list(POINTS.glob('*.tmdb.json'))+list(POINTS.glob('*.tmdata.json'))
                            if len(declared)!=len(all_points):
                                raise ValueError('Unverified point protects all incremental objects')
                            file_points.garbage_collect(POINTS,all_points,password)
                    except Exception as error:
                        # A new unverified output is never a rotation candidate.
                        if output.exists() and not output.with_suffix(output.suffix+'.json').exists():
                            output.unlink()
                        results.append({'kind':kind, 'status':'failed', 'error_type':type(error).__name__})
            finally:
                password = None
            report = dict(at=now.isoformat(), status='failed' if any(x['status'] in ('failed','insufficient-workspace') for x in results)
                          else 'checked', results=results, service_stopped=False,
                          format_note='tmdb is paired with authenticated tma+objects; tmdata needs its bound signed program; neither is legacy tmbackup')
            write(POINTS/'backup-status.json', report)
            return report
    except ValueError as error:
        if str(error) == BUSY:
            return {'status':'maintenance-busy'}
        raise


def user_cleanup():
    if os.geteuid() != 501:
        raise ValueError('Developer identity required')
    import fcntl
    from desktop_assistant.cleanup import assert_idle
    lock = BUILDS/'.local-maintenance.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    result = {'deleted':[], 'retained':[]}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = read(INSTALL/'build-plan.json')
        for row in plan:
            p = Path(row['path'])
            try:
                if p.parent.parent != BUILDS or p.name not in ('release.signed.zip', 'release.unsigned.zip', 'release.signed.zip.pending'):
                    raise ValueError('Build path outside fixed scope')
                target = Path(row['retained'])
                if not target.is_file() or policy.digest(target) != target.parent.name:
                    raise ValueError('Retained original unavailable')
                fp = policy.stable_file(p, uid=501)
                if policy.digest(p) != row['sha256']:
                    raise ValueError('Build changed')
                if subprocess.run(['git','-C',str(GIT),'cat-file','-e',row['git_sha']+'^{commit}'],
                                  capture_output=True).returncode:
                    raise ValueError('Source commit is unavailable')
                assert_idle(p, allow_unrelated_inaccessible_file=True)
                if config()['apply_cleanup']:
                    policy.unlink_verified(p, fp)
                    result['deleted'].append(str(p))
                else:
                    result.setdefault('planned',[]).append(str(p))
            except (ValueError, OSError) as error:
                result['retained'].append({'path':str(p), 'reason':type(error).__name__})
    finally:
        os.close(fd)
    return result


def close_readback_locked(manager):
    """Reuse the signed runtime's receipt-bound, restart-free owner closure."""
    if os.geteuid()!=0 or manager.state.get('current')!=_runtime_tree.parent.name:
        raise ValueError('Current protected administrator runtime required')
    from desktop_assistant import retained_recovery_cleanup as closure,local_backup_only as local
    if not manager._process() or not health():raise ValueError('Healthy live process required')
    return closure.close_locked(manager,{'uid':499,'gid':499},manager.state['current'],
        verify_cipher=local._check_proof,healthy=health)


def close_readback():
    from desktop_assistant.manager import Manager
    from desktop_assistant.operation_lock import operation_lock
    from desktop_assistant import mac_release_publish as publication
    with operation_lock(ROOT/'control/operation.lock',existing_only=True),publication.lock():
        manager=Manager(ROOT,(BASE/'publisher-public.pem').read_bytes())
        return close_readback_locked(manager)


def root_cleanup():
    if os.geteuid() != 0:
        raise ValueError('Root maintenance identity required')
    settings = config()
    if not settings['cleanup_enabled']:
        return {'status':'disabled'}
    from desktop_assistant.manager import Manager
    from desktop_assistant.operation_lock import operation_lock, BUSY
    from desktop_assistant import mac_release_publish as publication
    from desktop_assistant.cleanup import assert_idle, remove_owned_tree
    from desktop_assistant.retention import _release_files
    now = utc_now().isoformat()
    report = dict(at=now, status='started', removed=[], planned=[], retained=[], applied=settings['apply_cleanup'], free_before=shutil.disk_usage(ROOT).free)
    def record():
        write(INSTALL/'cleanup-status.json', report, mode=0o644)
    record()
    try:
        with operation_lock(ROOT/'control/operation.lock', existing_only=True), publication.lock():
            manager = Manager(ROOT, (BASE/'publisher-public.pem').read_bytes())
            state = manager.state
            if state.get('current')!=_runtime_tree.parent.name:
                return {'status':'runtime-changed'}
            if settings['apply_cleanup'] and state.get('backup_work_retained'):
                closure=close_readback_locked(manager)
                report['readback_closure']=closure
                if closure.get('status')=='closed':
                    report['removed'].append({'path':closure['path'],'kind':'verified-disposable-readback',
                                              'bytes':closure['deleted_bytes']})
                state=manager.state
            if not policy.maintenance_admitted(state, running=manager._process(), healthy=health()):
                report['status']='maintenance-attention';record();return report
            raw = (ROOT/'state.json').read_bytes()
            pid = manager._process()[1]['pid']
            proof_run = worker_child('service-proof')
            if proof_run.returncode:
                raise ValueError('Complete recovery verification failed')
            proof = json.loads(proof_run.stdout)
            if proof['state_sha256'] != policy.digest(ROOT/'state.json'):
                raise ValueError('State changed during proof verification')
            report['backup_proof_verified'] = True
            bindings={r['sha256']:dict(identity=r['identity'],programs=r['programs'])
                      for r in proof['backups'] if r.get('programs')}
            write(INSTALL/'program-bindings.json',bindings,mode=0o644)
            def unchanged():
                if ((ROOT/'state.json').read_bytes() != raw or not health()
                        or manager._process()[1]['pid'] != pid):
                    raise ValueError('Live runtime changed; cleanup stops')
            manifests = {}
            archives = {}
            for p in (BASE/'published').iterdir():
                if policy.IDENTITY.fullmatch(p.name) and (p/'package.zip').is_file():
                    archive, manifest = program(p.name, manager.public_key)
                    archives[p.name], manifests[p.name] = archive, manifest
            receipts = delivery_receipts(manifests, state['current'], now=now)
            processes = process_values()
            extra = set(settings.get('protected_helper_packages', []))
            extra.update(development_program_references())
            for name in ('system-service.json','factory-activation.json','current-mac-publication.json'):
                p=BASE/name
                if p.is_file():extra.update(re.findall('[0-9a-f]{64}',p.read_text()))
            for value in processes:
                extra.update(re.findall(r'/Library/TianmingERP/published/([0-9a-f]{64})', value))
            unknown_backup=False
            for row in proof['backups']:
                if not row['protected'] and row['recognized']:
                    p=Path(row['path']);unchanged();assert_idle(p,allow_unrelated_inaccessible_file=True)
                    if policy.digest(p)!=row['sha256']:raise ValueError('Predecessor cipher changed')
                    if settings['apply_cleanup']: policy.unlink_verified(p,row['identity'])
                    report['removed' if settings['apply_cleanup'] else 'planned'].append({'path':str(p),'kind':'authenticated-backup-predecessor'})
                elif row['protected']:
                    old=settings.get('legacy_backup_programs',{}).get(row['sha256'])
                    if old:extra.update(old['programs'])
                    elif row.get('programs'):extra.update(row['programs'])
                    elif row['path']!=proof['path']:unknown_backup=True
                elif not row['recognized']:
                    unknown_backup=True
            if unknown_backup:
                # An unclassified complete point can bind an older native program.
                extra.update(manifests)
                report['retained'].append({'kind':'unknown-backup-program-binding','reason':'protected-all-programs'})
            points = completed_points()
            keep = policy.protected_programs(state, manifests, additional=extra,
                                             point_programs=[p['program_sha256'] for p in points])
            expanded_keep = policy.expanded_programs(state, additional=extra,
                                             point_programs=[p['program_sha256'] for p in points])
            for folder in BUILDS.iterdir():
                if folder.is_symlink() or not folder.is_dir():
                    continue
                candidates=[p for p in folder.glob('*.zip*') if p.is_file() and not p.is_symlink()]
                if not candidates:
                    continue
                try:
                    with zipfile.ZipFile(candidates[0]) as z: m=json.loads(z.read('manifest.json'))
                    delivered=any(v.get('git_sha')==m.get('git_sha') for v in receipts.values())
                    if not delivered:
                        candidate_ids={h for h,manifest in manifests.items() if manifest==m}
                        keep.update(candidate_ids);expanded_keep.update(candidate_ids)
                        for name in ('build-result.json','signing-request.json'):
                            p=folder/name
                            if p.is_file() and p.stat().st_size<1024**2:
                                refs_in_candidate = re.findall('[0-9a-f]{64}',p.read_text())
                                keep.update(refs_in_candidate)
                                expanded_keep.update(refs_in_candidate)
                except (OSError,ValueError,zipfile.BadZipFile):
                    # Unreadable candidate: no original archives are retired this run.
                    keep.update(manifests)
                    expanded_keep.update(manifests)
            refs = database_path_references()+proof['active_paths']
            controls = bytearray(raw)
            for name in ('system-service.json','factory-activation.json','current-mac-publication.json'):
                p=BASE/name
                if p.is_file():
                    controls.extend(p.read_bytes())
            def idle(path):
                text=str(path)
                if (text.encode() in controls or any(text in v for v in processes)
                        or any(v==text or v.startswith(text+'/') for v in refs)):
                    raise ValueError('Declared runtime, backup or business dependency')
                assert_idle(path, allow_unrelated_inaccessible_file=path.is_file())
            # Completed developer artifacts: root only publishes a plan, user UID unlinks.
            build_plan=[]
            for folder in BUILDS.iterdir():
                if folder.is_symlink() or not folder.is_dir():
                    continue
                for name in ('release.signed.zip','release.unsigned.zip','release.signed.zip.pending'):
                    p=folder/name
                    if not p.is_file() or p.is_symlink() or time.time()-p.stat().st_mtime < 3600:
                        continue
                    try:
                        idle(p)
                        fp=policy.stable_file(p,uid=501)
                        with zipfile.ZipFile(p) as z:
                            manifest=json.loads(z.read('manifest.json'))
                        matches=[h for h,m in manifests.items() if m==manifest
                                 and policy.receipt_valid(receipts.get(h),h,m['git_sha'])]
                        if not matches:
                            continue
                        h=matches[0];digest=policy.digest(p)
                        if name!='release.unsigned.zip' and digest!=h:
                            continue
                        if name=='release.unsigned.zip':
                            if not policy.unsigned_equivalent(p, manifest):
                                continue
                        if policy.fingerprint(p)!=fp:
                            continue
                        build_plan.append(dict(path=str(p),sha256=digest,git_sha=manifest['git_sha'],retained=str(archives[h])))
                    except (OSError,ValueError,zipfile.BadZipFile):
                        continue
            write(INSTALL/'build-plan.json',build_plan,mode=0o644)
            child=subprocess.run([sys.executable,'-I','-B',str(INSTALL/'native.py'),'user-cleanup'],
                  user=501,group=20,extra_groups=[20],cwd='/',capture_output=True,timeout=600,
                  env={'HOME':'/Users/kimiyf','PATH':'/usr/bin:/bin:/usr/sbin:/sbin'})
            report['developer_cleanup']=json.loads(child.stdout) if child.returncode==0 else {'status':'failed'}
            unchanged()
            # Successful isolated migrations only, and only after that program was delivered.
            for job in (ROOT/'staging').glob('migration-*'):
                rp=job/'report.json'
                if not rp.is_file() or job.is_symlink():
                    continue
                try:
                    r=read(rp);h=r.get('package_sha256')
                    if (r.get('status')!='passed' or h not in manifests
                            or not policy.receipt_valid(receipts.get(h),h,manifests[h]['git_sha'])
                            or time.time()-rp.stat().st_mtime<3600):
                        continue
                    code,shared=job/'release',job/'shared'
                    if not code.exists() and not shared.exists():
                        continue
                    idle(code);idle(shared)
                    links=_release_files(job,code,manifests[h])
                    before=policy.tree_fingerprint(code,uid=499,device=ROOT.stat().st_dev,allow_links=links)
                    data=policy.tree_fingerprint(shared,uid=499,device=ROOT.stat().st_dev)
                    unchanged();idle(code);idle(shared)
                    if (policy.tree_fingerprint(code,uid=499,device=ROOT.stat().st_dev,allow_links=links)!=before
                            or policy.tree_fingerprint(shared,uid=499,device=ROOT.stat().st_dev)!=data):
                        raise ValueError('Migration copy changed')
                    if not settings['apply_cleanup']:
                        report['planned'].append({'path':str(job),'kind':'passed-migration-payload'});continue
                    for link in links:
                        link.unlink()
                    remove_owned_tree(code,job);remove_owned_tree(shared,job)
                    report['removed'].append({'path':str(job),'kind':'passed-migration-payload'})
                except (ValueError,OSError,KeyError) as e:
                    report['retained'].append({'path':str(job),'reason':type(e).__name__})
                record()
            # Additional terminal rehearsals: v2 schema evidence is self-contained.
            jobs=[]
            for job in (ROOT/'staging').glob('migration-*'):
                p=job/'report.json'
                if not job.is_symlink() and p.is_file() and not p.is_symlink():
                    jobs.append((job,read(p),p.stat().st_mtime_ns))
            source_cache={}
            def source_known(commit,*,ancestor=False):
                key=(commit,ancestor)
                if key not in source_cache:
                    args=(['merge-base','--is-ancestor',commit,manifests[state['current']]['git_sha']]
                          if ancestor else ['cat-file','-e',commit+'^{commit}'])
                    answer=subprocess.run(['/usr/bin/git','-C',str(GIT),*args],user=501,group=20,extra_groups=[20],
                                          cwd='/',capture_output=True,timeout=15,
                                          env={'HOME':'/Users/kimiyf','PATH':'/usr/bin:/bin:/usr/sbin:/sbin'})
                    source_cache[key]=answer.returncode==0
                return source_cache[key]
            for job,r,mtime in jobs:
                h=r.get('package_sha256');receipt=receipts.get(h,{})
                delivered=policy.receipt_valid(receipt,h,receipt.get('git_sha'))
                later=any(other.get('package_sha256')==h and other_mtime>mtime
                          and policy.rehearsal_payload_scope(other,delivered=True,ancestor=False,later_passed=False)=='release-and-shared'
                          for _,other,other_mtime in jobs)
                ancestor=(h in manifests and source_known(manifests[h]['git_sha'],ancestor=True))
                scope=policy.rehearsal_payload_scope(r,delivered=delivered,ancestor=ancestor,later_passed=later)
                names=('release','shared') if scope=='release-and-shared' else ('shared',) if scope=='shared-only' else ()
                if not any((job/name).exists() for name in names):continue
                try:
                    if not re.fullmatch('migration-[0-9a-f]{32}',job.name):raise ValueError('Unknown rehearsal path')
                    assert_idle(job)
                    for name in names:
                        target=job/name
                        if not target.exists():continue
                        if name=='release' and h not in manifests:raise ValueError('Rehearsal original unavailable')
                        links=_release_files(job,target,manifests[h]) if name=='release' else []
                        idle(target)
                        before=policy.tree_fingerprint(target,uid=499,device=ROOT.stat().st_dev,allow_links=links)
                        unchanged();idle(target)
                        if policy.tree_fingerprint(target,uid=499,device=ROOT.stat().st_dev,allow_links=links)!=before:
                            raise ValueError('Terminal rehearsal payload changed')
                        if settings['apply_cleanup']:
                            for link in links:link.unlink()
                            removed=remove_owned_tree(target,job)
                        else:removed=before['allocated']
                        report['removed' if settings['apply_cleanup'] else 'planned'].append(
                            {'path':str(target),'kind':'terminal-rehearsal-'+scope,'bytes':removed})
                except (ValueError,OSError,KeyError) as e:
                    report['retained'].append({'path':str(job),'reason':type(e).__name__})
                record()
            # Old expansion sets only. Exact signed originals remain available.
            registry=read(BASE/'published.json')
            candidates=set(registry['packages'])
            candidates.update(p.name for p in (ROOT/'releases').iterdir() if policy.IDENTITY.fullmatch(p.name))
            candidates.update(p.stem for p in (ROOT/'packages').glob('*.zip') if policy.IDENTITY.fullmatch(p.stem))
            candidates.update(h for h in manifests if (BASE/'published'/h/'server').exists() or (BASE/'published'/h/'migration').exists())
            report['protected_originals'] = sorted(keep)
            report['protected_expansions'] = sorted(expanded_keep)
            for h in sorted(candidates-expanded_keep):
                if h not in manifests:
                    continue
                try:
                    if not source_known(manifests[h]['git_sha']):raise ValueError('Source commit unavailable')
                    parent=BASE/'published'/h;manifest=manifests[h]
                    with archives[h].open('rb') as f:
                        manifest_raw,_,_=publication.inspect(f)
                    trees=[kind for kind in ('server','migration') if (parent/kind).exists()]
                    if h in registry['packages'] and len(trees)!=2:
                        raise ValueError('Registered runtime slot is incomplete')
                    for kind in trees:
                        publication.check_tree(parent/kind,manifest,manifest_raw,server=kind=='server')
                        idle(parent/kind)
                    cache=ROOT/'releases'/h
                    links=[]
                    if cache.exists():
                        idle(cache);links=_release_files(ROOT,cache,manifest)
                    installed=ROOT/'packages'/(h+'.zip')
                    if installed.exists():
                        if policy.digest(installed)!=h:
                            raise ValueError('Installed archive changed')
                        idle(installed)
                    unchanged()
                    if read(BASE/'published.json')!=registry:
                        raise ValueError('Registry changed')
                    if not settings['apply_cleanup']:
                        report['planned'].append({'path':str(parent),'kind':'old-expansions-original-kept'});continue
                    if h in registry['packages']:
                        registry={**registry,'packages':[v for v in registry['packages'] if v!=h]}
                        write(BASE/'published.json',registry,mode=0o644);publication.sync_directory(BASE)
                    for kind in trees:
                        tree=parent/kind
                        if kind=='server':
                            for relative,target in publication.MANAGED.items():
                                link=tree/relative
                                if not link.is_symlink() or link.resolve()!=ROOT/'shared'/target:
                                    raise ValueError('Managed data link changed')
                                link.unlink()
                        remove_owned_tree(tree,parent)
                    if cache.exists():
                        for link in links:
                            link.unlink()
                        remove_owned_tree(cache,ROOT/'releases')
                    if installed.exists():
                        installed.unlink()
                    report['removed'].append({'path':str(parent),'kind':'old-expansions-original-kept'})
                except (ValueError,OSError,KeyError) as e:
                    report['retained'].append({'path':str(BASE/'published'/h),'reason':type(e).__name__})
                record()
            # A direct signed stage ZIP needs both a delivered original and no path dependencies.
            for stage in Path('/Library').glob('TianmingERP-*'):
                if stage==INSTALL or stage.is_symlink():
                    continue
                for name in ('package.zip','release.signed.zip','release.signed.zip.pending','release.unsigned.zip'):
                    p=stage/name
                    if not p.is_file() or p.is_symlink() or time.time()-p.stat().st_mtime<3600:
                        continue
                    try:
                        fp=policy.stable_file(p,uid=0);h=policy.digest(p)
                        if name=='release.unsigned.zip':
                            with zipfile.ZipFile(p) as z: candidate=json.loads(z.read('manifest.json'))
                            matches=[v for v,m in manifests.items() if m==candidate]
                            if not matches or not policy.unsigned_equivalent(p,candidate): continue
                            h=matches[0]
                        if h not in manifests or not policy.receipt_valid(receipts.get(h),h,manifests[h]['git_sha']):
                            continue
                        idle(p);unchanged()
                        if settings['apply_cleanup']: policy.unlink_verified(p,fp)
                        report['removed' if settings['apply_cleanup'] else 'planned'].append({'path':str(p),'kind':'identical-stage-archive'})
                    except (ValueError,OSError) as e:
                        report['retained'].append({'path':str(p),'reason':type(e).__name__})
            # Unique originals rotate only after all registered/cache roles are gone.
            monthly=policy.monthly_programs([
                dict(identity=h,created=dt.datetime.fromtimestamp(archives[h].stat().st_mtime,dt.timezone.utc).isoformat())
                for h in receipts if h in archives],limit=settings['monthly_program_keep'])
            for h in sorted(set(receipts)-keep-monthly):
                if h not in archives or h in registry['packages']:
                    continue
                p=archives[h]
                try:
                    if (p.parent/'server').exists() or (p.parent/'migration').exists():
                        continue
                    if (ROOT/'releases'/h).exists() or (ROOT/'packages'/(h+'.zip')).exists():
                        continue
                    # Existing developer aliases are actual path dependencies.
                    if any(q.is_symlink() and q.resolve()==p for q in BUILDS.glob('*/release.signed.zip')):
                        continue
                    idle(p);fp=policy.stable_file(p,uid=0)
                    if policy.digest(p)!=h: raise ValueError('Original changed')
                    unchanged()
                    if settings['apply_cleanup']:
                        policy.unlink_verified(p,fp)
                        if not any(p.parent.iterdir()): p.parent.rmdir()
                    report['removed' if settings['apply_cleanup'] else 'planned'].append({'path':str(p),'kind':'closed-unreferenced-original'})
                except (ValueError,OSError) as e:
                    report['retained'].append({'path':str(p),'reason':type(e).__name__})
            unchanged();report.update(status='completed',free_after=shutil.disk_usage(ROOT).free,
                                     low_space=shutil.disk_usage(ROOT).free<settings['minimum_free_bytes'])
            record()
            return report
    except ValueError as e:
        if str(e)==BUSY:
            report['status']='maintenance-busy';record();return report
        raise


def main():
    if (Path(__file__).resolve() != INSTALL/'native.py' or not sys.flags.isolated
            or not sys.dont_write_bytecode or len(sys.argv)!=2):
        raise ValueError('Fixed isolated maintenance entry required')
    action=sys.argv[1]
    if action in ('service-proof','service-backup','service-verify-pair'):
        result=service_action(action)
    elif action=='user-cleanup':
        result=user_cleanup()
    elif action=='cleanup':
        result=root_cleanup()
    elif action=='close-readback':
        result=close_readback()
        write(INSTALL/'readback-closure.json',result,mode=0o644)
    elif action=='verify-pair':
        if os.geteuid()!=0:raise ValueError('Root admission required')
        run=worker_child('service-verify-pair')
        result=json.loads(run.stdout) if run.returncode==0 else {'status':'failed','error_type':'PairedRestoreError'}
        result['at']=utc_now().isoformat()
        write(INSTALL/'paired-restore-proof.json',result,mode=0o644)
    elif action=='backup':
        if os.geteuid()!=0:
            raise ValueError('Root admission required')
        run=worker_child('service-backup')
        try: result=json.loads(run.stdout)
        except ValueError: result={'status':'failed','error_type':'WorkerOutputError'}
        if run.returncode:result['status']='failed'
        previous=read(INSTALL/'backup-status.json') if (INSTALL/'backup-status.json').is_file() else {}
        points=completed_points()
        result.update(at=utc_now().isoformat(),
             consecutive_failures=previous.get('consecutive_failures',0)+1 if result.get('status')=='failed' else 0,
             last_database_success=max((p['created'] for p in points if p['kind']=='database'),default=None),
             last_data_success=max((p['created'] for p in points if p['kind']=='data'),default=None))
        write(INSTALL/'backup-status.json',result,mode=0o644)
    else:
        raise ValueError('Unknown fixed maintenance action')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    if result.get('status')=='failed':raise SystemExit(1)


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        # No native error text or credential reference is written to public logs.
        failure={'status':'failed','error_type':type(error).__name__,'at':utc_now().isoformat()}
        if os.geteuid()==0 and Path(__file__).resolve()==INSTALL/'native.py' and len(sys.argv)==2:
            action=sys.argv[1]
            if action in ('cleanup','backup','verify-pair','close-readback'):
                try:write(INSTALL/(action+'-status.json'),failure,mode=0o644)
                except OSError:pass
        print(json.dumps(failure),flush=True)
        raise SystemExit(1)

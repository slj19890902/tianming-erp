"""Administrator-only installer for fixed local maintenance jobs; no ERP restart."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

BASE=Path('/Library/TianmingERP')
ROOT=BASE/'instance'
DEST=Path('/Library/TianmingERP-LocalMaintenance')
POINTS=Path('/Library/TianmingERP-OnlinePoints')
SOURCE=Path('/Users/kimiyf/ERP-Migration/storage-autoreclaim-20261017')
SOURCES={'launcher.py':SOURCE/'scripts/local_maintenance/launcher.py',
         'native.py':SOURCE/'scripts/local_maintenance/native.py',
         'file_points.py':SOURCE/'desktop_assistant/incremental_file_points.py',
         'storage_policy.py':SOURCE/'desktop_assistant/local_storage_policy.py'}
# Replaced by the reviewed stage preparation, never supplied as executable input.
PINS = None


def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(4*1024**2),b''):
            h.update(b)
    return h.hexdigest()


def protected(p,*,directory=False):
    s=Path(p).lstat()
    if s.st_uid!=0 or s.st_mode&0o022 or Path(p).is_symlink():
        raise ValueError('Unprotected maintenance installation')
    if directory and not stat.S_ISDIR(s.st_mode):
        raise ValueError('Expected maintenance directory')


def save(p,v,mode=0o644):
    import uuid
    p=Path(p);q=p.with_name(p.name+'.'+uuid.uuid4().hex+'.pending')
    with q.open('x') as f:
        os.fchmod(f.fileno(),mode);json.dump(v,f,sort_keys=True,separators=(',',':'));f.flush();os.fsync(f.fileno())
    os.replace(q,p)


def plist_for(action):
    label='cn.tianming.erp.local-maintenance' if action=='cleanup' else 'cn.tianming.erp.online-backup'
    return dict(Label=label,ProgramArguments=['/usr/bin/python3','-I','-S','-B',str(DEST/'launcher.py'),action],
                UserName='root',WorkingDirectory='/',RunAtLoad=True,
                StartInterval=3600 if action=='cleanup' else 900,ThrottleInterval=300,
                **({'KeepAlive': {'SuccessfulExit': False}} if action=='cleanup' else {}),
                ProcessType='Background',LowPriorityIO=True,Nice=10,Umask=0o027,
                StandardOutPath=str(DEST/(action+'.stdout.log')),
                StandardErrorPath=str(DEST/(action+'.stderr.log')))


def stage():
    for name,p in SOURCES.items():
        if digest(p)!=PINS[name]:
            raise ValueError('Reviewed source changed')
    if not DEST.exists():DEST.mkdir(mode=0o755)
    for p in (DEST,*DEST.parents):protected(p,directory=True)
    loaded=subprocess.run(['/bin/launchctl','print','system/cn.tianming.erp.local-maintenance'],capture_output=True)
    if loaded.returncode==0:
        raise ValueError('Existing maintenance must be reviewed before replacing its code')
    for name,p in SOURCES.items():
        dst=DEST/name
        if dst.exists():
            old=json.loads((DEST/'assets.json').read_bytes())
            if digest(dst)!=old[name]:raise ValueError('Existing asset changed')
        temporary=DEST/(name+'.installing')
        with temporary.open('xb') as f:os.fchmod(f.fileno(),0o644);f.write(p.read_bytes());f.flush();os.fsync(f.fileno())
        os.replace(temporary,dst)
    save(DEST/'assets.json',PINS)
    if not POINTS.exists():
        POINTS.mkdir(mode=0o700);os.chown(POINTS,499,499)
    s=POINTS.lstat()
    if POINTS.is_symlink() or s.st_uid!=499 or stat.S_IMODE(s.st_mode)!=0o700:
        raise ValueError('Online points directory identity changed')
    hist=Path('/Library/TianmingERP-LabelRelease-v646-20261016/registry-dependencies.stdout')
    if digest(hist)!='9bb9bba16c928828447427250b72698531715d8766f9fad5765c9055cb9fbeee':
        raise ValueError('Historical source proof changed')
    history=json.loads(hist.read_bytes());assert history['status']=='passed'
    legacy={}
    for p in (ROOT/'backups').glob('*.tmbackup'):
        row=history['backups'].get(p.name)
        if row and digest(p)==row['sha256']:
            legacy[row['sha256']]={'name':p.name,'programs':row['programs']}
    helper=Path('/Library/TianmingERP-Closure-v644-20261016/active-helper-package-dependencies.json')
    if digest(helper)!='b8c04124835580f3d0094556f2932bd261c2427a17c5b93ab01c93413eff2cca':
        raise ValueError('Native helper binding changed')
    extra=json.loads(helper.read_bytes())
    for name,row in extra['known_active_immutable_helper_refs'].items():
        if digest(name)!=row['sha256']:raise ValueError('Native helper code changed')
    keep=set(extra['additional_protected'])
    for p in Path('/Library/LaunchDaemons').glob('cn.tianming.erp*.plist'):
        v=plistlib.loads(p.read_bytes())
        for arg in v.get('ProgramArguments',[]):keep.update(re.findall(r'/Library/TianmingERP/published/([0-9a-f]{64})',arg))
    cfg=dict(type='tianming.local-maintenance-policy.v1',cleanup_enabled=True,apply_cleanup=False,
       backup_enabled=False,database_interval_seconds=900,data_interval_seconds=0,
       database_keep=3,data_keep=1,monthly_program_keep=12,minimum_free_bytes=200*10**9,
       context_sha256=digest(BASE/'keychain-context.json'),publisher_key_sha256=digest(BASE/'publisher-public.pem'),
       protected_helper_packages=sorted(keep),legacy_backup_programs=legacy,
       backup_frequency_status='owner-selected-pre-release-only-not-yet-enabled')
    save(DEST/'policy.json',cfg)
    print(json.dumps(dict(staged=True,dry_run=True,legacy_sources=len(legacy),backup_enabled=False)))


def bootstrap(action):
    definition=plist_for(action);path=Path('/Library/LaunchDaemons')/(definition['Label']+'.plist')
    raw=plistlib.dumps(definition,fmt=plistlib.FMT_XML,sort_keys=True)
    if path.exists() and path.read_bytes()!=raw:raise ValueError('Conflicting task already exists')
    if not path.exists():
        with path.open('xb') as f:os.fchmod(f.fileno(),0o644);f.write(raw);f.flush();os.fsync(f.fileno())
    protected(path)
    result=subprocess.run(['/bin/launchctl','print','system/'+definition['Label']],capture_output=True)
    if result.returncode:
        subprocess.run(['/bin/launchctl','bootstrap','system',str(path)],check=True,capture_output=True)
    return definition['Label']


def activate_cleanup():
    cfg=json.loads((DEST/'policy.json').read_bytes())
    report=json.loads((DEST/'cleanup-status.json').read_bytes())
    if report.get('status')!='completed' or report.get('applied') is not False:
        raise ValueError('Successful dry run required')
    cfg['apply_cleanup']=True;save(DEST/'policy.json',cfg)
    label=bootstrap('cleanup')
    print(json.dumps(dict(enabled=True,label=label,interval_seconds=3600)))


def configure_backup(selection):
    if selection not in ('daily','six-hour','pre-release-only'):
        raise ValueError('Owner backup frequency selection required')
    cfg=json.loads((DEST/'policy.json').read_bytes())
    cfg.update(backup_enabled=True,database_interval_seconds=900,
       data_interval_seconds={'daily':86400,'six-hour':21600,'pre-release-only':0}[selection],
       backup_frequency_status='owner-selected-'+selection)
    save(DEST/'policy.json',cfg)
    print(json.dumps(dict(configured=True,not_yet_scheduled=True,selection=selection)))


def activate_backup():
    cfg=json.loads((DEST/'policy.json').read_bytes())
    status=json.loads((POINTS/'backup-status.json').read_bytes())
    kinds={'database'}|({'data'} if cfg['data_interval_seconds'] else set())
    verified={x['kind'] for x in status.get('results',[]) if x.get('status') in ('verified','up-to-date')}
    if not cfg['backup_enabled'] or not kinds<=verified or status.get('status')=='failed':
        raise ValueError('Actual verified online point required before scheduling')
    proof=json.loads((DEST/'paired-restore-proof.json').read_bytes())
    if proof.get('verified') is not True or proof.get('service_started') is not False:
        raise ValueError('Actual isolated paired restore required before scheduling')
    name=proof.get('point_name','')
    if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.tmdb',name):
        raise ValueError('Isolated restore point identity invalid')
    receipt=json.loads((POINTS/(name+'.json')).read_bytes())
    if (receipt.get('verified') is not True or not receipt.get('attachment_set')
            or digest(POINTS/name)!=proof.get('point_sha256')
            or receipt.get('sha256')!=proof['point_sha256']
            or receipt.get('program_sha256')!=proof.get('program_sha256')):
        raise ValueError('Isolated paired point changed before activation')
    # Native supported pause gate disables only the incompatible old timer.
    context=digest(BASE/'keychain-context.json')
    pause={'type':'tianming.automatic-backup-pause.v1','context_sha256':context,'paused':True}
    path=BASE/'automatic-backup-paused.json'
    if path.exists() and json.loads(path.read_bytes())!=pause:raise ValueError('Existing pause belongs to another context')
    save(path,pause)
    label=bootstrap('backup')
    print(json.dumps(dict(enabled=True,label=label,check_interval_seconds=900,legacy_timer_paused=True,erp_restarted=False)))


if __name__=='__main__':
    if os.geteuid()!=0 or not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode:
        raise SystemExit('Administrator isolated installer required')
    if sys.argv[1:]==['stage']:stage()
    elif sys.argv[1:]==['activate-cleanup']:activate_cleanup()
    elif len(sys.argv)==3 and sys.argv[1]=='configure-backup':configure_backup(sys.argv[2])
    elif sys.argv[1:]==['activate-backup']:activate_backup()
    else:raise SystemExit('Unknown bounded installation action')

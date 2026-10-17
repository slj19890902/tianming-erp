"""Root-owned launchd entry; resolve only an approved, protected live runtime."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import zipfile

BASE=Path('/Library/TianmingERP')
INSTALL=Path('/Library/TianmingERP-LocalMaintenance')


def require_protected(p, *, directory=False):
    p=Path(p)
    s=p.lstat()
    if (p.is_symlink() or s.st_uid!=0 or s.st_mode&0o022
            or (directory and not stat.S_ISDIR(s.st_mode))
            or (not directory and (not stat.S_ISREG(s.st_mode) or s.st_nlink!=1))):
        raise ValueError('Unprotected maintenance dependency')


def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(4*1024**2),b''):
            h.update(b)
    return h.hexdigest()


def main():
    if (os.geteuid()!=0 or Path(__file__).absolute()!=INSTALL/'launcher.py'
            or not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode
            or len(sys.argv)!=2 or sys.argv[1] not in ('cleanup','backup','verify-pair')):
        raise ValueError('Fixed launchd maintenance entry required')
    for p in (INSTALL,*INSTALL.parents):
        require_protected(p,directory=True)
    for name in ('assets.json','policy.json','launcher.py','native.py','storage_policy.py','file_points.py'):
        require_protected(INSTALL/name)
    assets=json.loads((INSTALL/'assets.json').read_bytes())
    for name in ('launcher.py','native.py','storage_policy.py','file_points.py'):
        if digest(INSTALL/name)!=assets[name]:
            raise ValueError('Installed maintenance code changed')
    # Ordinary maintenance logs only. Business audit logs are outside this scope.
    for suffix in ('stdout.log','stderr.log'):
        log=INSTALL/(sys.argv[1]+'.'+suffix)
        if log.exists():
            require_protected(log)
            if log.stat().st_size>=8*1024**2:
                oldest=log.with_name(log.name+'.3')
                if oldest.exists():require_protected(oldest);oldest.unlink()
                for number in (2,1):
                    source=log.with_name(log.name+'.'+str(number))
                    if source.exists():require_protected(source);source.replace(log.with_name(log.name+'.'+str(number+1)))
                log.replace(log.with_name(log.name+'.1'))
    state=json.loads((BASE/'instance/state.json').read_bytes())
    identity=state.get('current')
    if not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{64}',identity):
        raise ValueError('Invalid live identity')
    require_protected(BASE/'published.json')
    registry=json.loads((BASE/'published.json').read_bytes())
    if identity not in registry['packages']:
        raise ValueError('Live runtime is not approved')
    parent=BASE/'published'/identity
    archive=parent/'package.zip'
    require_protected(archive)
    if digest(archive)!=identity:
        raise ValueError('Live original changed')
    with zipfile.ZipFile(archive) as z:
        raw=z.read('manifest.json')
    manifest=json.loads(raw)
    if manifest.get('runtime_platform')!='macos-arm64':
        raise ValueError('Maintenance runtime platform mismatch')
    tree=parent/'migration'
    # Reuse the deployment's strict protected-tree validator after admitting
    # root-owned package bytes. Never load a checkout or user site-packages.
    actual=set()
    for folder,dirs,names in os.walk(tree,followlinks=False):
        require_protected(folder,directory=True)
        for name in dirs:
            require_protected(Path(folder)/name,directory=True)
        for name in names:
            p=Path(folder)/name;require_protected(p)
            rel=p.relative_to(tree).as_posix();actual.add(rel)
            expected=hashlib.sha256(raw).hexdigest() if rel=='manifest.json' else manifest['files'].get(rel)
            if digest(p)!=expected:
                raise ValueError('Signed maintenance runtime tree changed')
    if actual!=set(manifest['files'])|{'manifest.json'}:
        raise ValueError('Signed runtime members missing')
    executable=tree/'runtime/bin/python3.12'
    require_protected(executable)
    os.execve(str(executable),[str(executable),'-I','-B',str(INSTALL/'native.py'),sys.argv[1]],
              {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':'/Library/TianmingERP/home',
               'LANG':'en_US.UTF-8','ERP_HOME_REHEARSAL':'1'})


if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'status':'admission-failed','error_type':type(e).__name__}),flush=True)
        raise SystemExit(1)

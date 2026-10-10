"""User-login backup scheduling, separate from the web-service supervisor."""
import hashlib
import os
import plistlib
import subprocess

from desktop_assistant.mac_service import (
    LAUNCHCTL, _exclusive_or_equal, _launch_agents, label_for, local_root, verified_entry_locked,
)
from desktop_assistant.storage import read_json, write_json


def definition(root, python, script, public, fingerprint):
    return {'Label':label_for(root)+'.backup',
        'ProgramArguments':[str(python),'-I','-B',str(script),'--root',str(root),
            '--public-key',str(public),'--publisher-sha256',fingerprint],
        'WorkingDirectory':str(root),'StartCalendarInterval':{'Minute':0},
        'RunAtLoad':False,'KeepAlive':False,'Umask':0o077,'ProcessType':'Background',
        'EnvironmentVariables':{'ERP_HOME_REHEARSAL':'1'},
        'StandardOutPath':str(root/'control/nightly.stdout.log'),
        'StandardErrorPath':str(root/'control/nightly.stderr.log')}


def register_locked(manager):
    """Called while Save Backup Settings already holds the maintenance lock."""
    arguments = verified_entry_locked(manager, 'desktop_assistant/nightly_entry.py')
    root = arguments[0]
    spec = definition(*arguments)
    content = plistlib.dumps(spec, sort_keys=True)
    digest = hashlib.sha256(content).hexdigest()
    source = root/'control'/(spec['Label']+'.plist')
    receipt = root/'control/nightly-registration.json'
    target = _launch_agents()/source.name
    domain = f'gui/{os.getuid()}'
    exists = subprocess.run([LAUNCHCTL,'print',domain+'/'+spec['Label']],capture_output=True).returncode == 0
    if exists:
        # Changing a password does not change the job definition. Only a prior
        # successful registration by this installation may be reused.
        if (not source.is_file() or not target.is_file() or source.is_symlink() or target.is_symlink()
                or not receipt.is_file() or receipt.is_symlink()
                or any(p.stat().st_uid != os.getuid() for p in (source,target,receipt))
                or source.read_bytes()!=content or target.read_bytes()!=content
                or read_json(receipt)!={'label':spec['Label'],'sha256':digest,'uid':os.getuid()}):
            raise ValueError('同名备份任务归属或版本不一致，拒绝接管')
        return {'registered':True,'reused':True,'requires_user_login':True}
    _exclusive_or_equal(source, content)
    _exclusive_or_equal(target, content)
    result = subprocess.run([LAUNCHCTL,'bootstrap',domain,str(target)],capture_output=True)
    if result.returncode:
        raise ValueError('Mac备份计划注册未确认；原备份设置保留')
    write_json(receipt,{'label':spec['Label'],'sha256':digest,'uid':os.getuid()})
    return {'registered':True,'reused':False,'requires_user_login':True}


def unregister_locked(manager):
    """Caller holds maintenance lock, so no running backup can be interrupted."""
    root=local_root(manager.root)
    label=label_for(root)+'.backup'
    source=root/'control'/(label+'.plist')
    target=_launch_agents()/source.name
    receipt=root/'control/nightly-registration.json'
    if any(not p.is_file() or p.is_symlink() or p.stat().st_uid!=os.getuid()
           for p in (source,target,receipt)):
        raise ValueError('无法确认备份任务归属，未停用')
    content=source.read_bytes();digest=hashlib.sha256(content).hexdigest()
    spec=plistlib.loads(content)
    if (target.read_bytes()!=content or spec.get('Label')!=label or spec.get('WorkingDirectory')!=str(root)
            or read_json(receipt)!={'label':label,'sha256':digest,'uid':os.getuid()}):
        raise ValueError('备份任务归属或指纹不一致，未停用')
    result=subprocess.run([LAUNCHCTL,'bootout',f'gui/{os.getuid()}',str(target)],capture_output=True)
    if result.returncode:
        raise ValueError('备份任务停用未确认，保留配置供核查')
    history=root/'control/nightly-history'
    history.mkdir(exist_ok=True,mode=0o700)
    _exclusive_or_equal(history/(digest+'.plist'),content)
    _exclusive_or_equal(history/(digest+'.json'),receipt.read_bytes())
    target.unlink();source.unlink();receipt.unlink()
    return {'disabled':True,'label':label}

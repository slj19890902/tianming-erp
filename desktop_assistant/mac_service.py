"""Verified launchd user-agent preparation. No root daemon or factory activation."""
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

from desktop_assistant.release_request import public_identity
from desktop_assistant.runtime_platform import runtime_python
from desktop_assistant.storage import sha, signed_release_manifest, safe_name, write_json

LAUNCHCTL = '/bin/launchctl'


def local_root(root):
    if sys.platform != 'darwin' or os.getuid() == 0:
        raise ValueError('后台预演要求Mac普通用户会话，不以root运行')
    import pwd
    root = Path(root).absolute()
    if not root.is_dir() or any(p.is_symlink() or p.is_junction() for p in (root, *root.parents)):
        raise ValueError('服务根目录必须存在且不能经过链接')
    root = root.resolve()
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if (not root.is_relative_to(home) or root.stat().st_dev != home.stat().st_dev
            or root.stat().st_uid != os.getuid()):
        raise ValueError('服务根目录必须属于当前用户并位于本机用户磁盘')
    return root


def label_for(root):
    return 'cn.tianming.erp.' + hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:20]


def definition(root, python, script, public, fingerprint):
    """Pure definition builder. Only prepare() authenticates its input code."""
    return {
        'Label':label_for(root), 'ProgramArguments':[str(python), '-I', str(script),
            '--root',str(root),'--public-key',str(public),'--publisher-sha256',fingerprint],
        'WorkingDirectory':str(root), 'RunAtLoad':True,
        'KeepAlive':{'SuccessfulExit':False}, 'ThrottleInterval':30,
        'ExitTimeOut':90, 'AbandonProcessGroup':True, 'Umask':0o077,
        # HTTP ERP requests are user work, not XPC transactions. Avoid imposing
        # background CPU/I/O throttling on the inherited web-server process.
        'ProcessType':'Interactive', 'EnvironmentVariables':{'ERP_HOME_REHEARSAL':'1'},
        'StandardOutPath':str(root/'control/service.stdout.log'),
        'StandardErrorPath':str(root/'control/service.stderr.log'),
    }


def _exclusive_or_equal(path, content):
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('服务文件不能是链接')
    if path.exists():
        if path.stat().st_uid != os.getuid() or path.read_bytes() != content:
            raise ValueError('服务文件已存在且不同，拒绝覆盖')
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def prepare(manager):
    root = local_root(manager.root)
    with manager.lock():
        identity = manager.state.get('current')
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise ValueError('未恢复的安装不能注册服务')
        package = root / 'packages' / (identity + '.zip')
        if sha(package) != identity:
            raise ValueError('服务发布包身份不一致')
        manifest = signed_release_manifest(package, manager.public_key)
        if manifest.get('runtime_platform') != 'macos-arm64':
            raise ValueError('Windows包不能注册为Mac后台服务')
        release = root / 'releases' / identity
        python = runtime_python(release, manifest, runnable=True)
        # Check all shipped code/runtime files even after managed data links were
        # added. No mutable cached manifest chooses the supervisor or interpreter.
        for name, expected in manifest['files'].items():
            safe_name(name)
            path = release / name
            if path.resolve() != release.resolve() / name or not path.is_file() or sha(path) != expected:
                raise ValueError('后台服务发布文件校验失败')
        script = release / 'desktop_assistant/service_supervisor.py'
        if 'desktop_assistant/service_supervisor.py' not in manifest['files']:
            raise ValueError('已签名包缺少服务监护入口')
        public = root / 'control/service-public.pem'
        _exclusive_or_equal(public, manager.public_key)
        spec = definition(root, python, script, public, public_identity(manager.public_key)[1])
        path = root / 'control' / (spec['Label'] + '.plist')
        _exclusive_or_equal(path, plistlib.dumps(spec, sort_keys=True))
        return path


def _launch_agents():
    import pwd
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    path = home / 'Library/LaunchAgents'
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('LaunchAgents目录不能经过链接')
    path.mkdir(exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise ValueError('LaunchAgents不属于当前用户')
    return path


def install(manager):
    source = prepare(manager)
    spec = plistlib.loads(source.read_bytes())
    domain = f'gui/{os.getuid()}'
    if subprocess.run([LAUNCHCTL, 'print', domain+'/'+spec['Label']], capture_output=True).returncode == 0:
        raise ValueError('同名用户服务已注册；先核验并停用，不能覆盖或接管')
    target = _launch_agents() / source.name
    _exclusive_or_equal(target, source.read_bytes())
    result = subprocess.run([LAUNCHCTL, 'bootstrap', domain, str(target)], capture_output=True)
    if result.returncode:
        raise ValueError('用户服务注册未确认成功，保留配置供核查；未接管其他服务')
    return {'label':spec['Label'], 'registered':True, 'health_verified':False,
            'mode':'home-rehearsal', 'requires_user_login':True}


def disable(manager):
    root = local_root(manager.root)
    label = label_for(root)
    source = root / 'control' / (label+'.plist')
    target = _launch_agents() / source.name
    # The retained ownership copy proves which exact plist this installation
    # created. No wildcard bootout or deletion of unrelated login items.
    if (source.is_symlink() or target.is_symlink() or not source.is_file() or not target.is_file()
            or target.stat().st_uid != os.getuid() or source.read_bytes() != target.read_bytes()):
        raise ValueError('不能确认用户服务配置归属，未停用或删除')
    spec = plistlib.loads(source.read_bytes())
    arguments = spec.get('ProgramArguments', [])
    if (spec.get('Label') != label or spec.get('WorkingDirectory') != str(root)
            or arguments.count('--root') != 1
            or arguments[arguments.index('--root')+1:arguments.index('--root')+2] != [str(root)]):
        raise ValueError('服务标签或安装根目录不匹配，未停用')
    with manager.lock():
        state = manager.state
        state['manual_stop'] = True
        write_json(root/'state.json', state)
    result = subprocess.run([LAUNCHCTL, 'bootout', f'gui/{os.getuid()}', str(target)], capture_output=True)
    if result.returncode:
        raise ValueError('用户服务停用未确认，保留配置供核查')
    if manager._process():
        raise ValueError('服务进程仍在运行或维护中，保留配置，不强杀')
    history = root / 'control/service-history'
    history.mkdir(exist_ok=True, mode=0o700)
    archived = history / (sha(source)+'.plist')
    _exclusive_or_equal(archived, source.read_bytes())
    target.unlink()
    source.unlink()  # Only this tool's verified ownership copy, now archived.
    return {'label':label,'disabled':True}

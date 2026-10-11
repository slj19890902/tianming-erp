"""Only synthetic files in a new attempt directory; never read real backups.

This is an installation proof, not an ERP backup receipt or boot/session proof.
All remote files are retained, including on failure, for administrator review.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from app.core import mac_service_keychain as keychain
from desktop_assistant import mac_archive_contract as cfg
from desktop_assistant import mac_archive_install as setup
from desktop_assistant import mac_backup_storage as storage
from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_published_runtime as published
from desktop_assistant import mac_release_publish as publication
from desktop_assistant import mac_mount as mounts

SIZE = 131072


def authenticated(request, script, executable):
    if (not isinstance(request, dict) or request.get('phase') not in {'identity', 'probe', 'readback'}
            or set(request) != {'phase', 'challenge', 'plan', 'plan_sha256'} |
                ({'expected'} if request.get('phase') == 'readback' else set())
            or not isinstance(request['challenge'], str) or not re.fullmatch('[0-9a-f]{32}', request['challenge'])):
        raise ValueError('归档探针请求无效')
    if sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode or not keychain.selected():
        raise ValueError('归档探针必须使用隔离专用身份')
    context = keychain.load_context()
    if boot._actual() != boot._expected(context['uid'], context['gid']):
        raise ValueError('归档探针实际内核身份不符')
    raw = boot.canonical(request['plan']); plan = setup.header(raw, request['plan_sha256'])
    meta = setup.metadata(plan['package_sha256']); setup.binding(plan, meta)
    if context != meta['context']: raise ValueError('归档探针实际身份与root计划不同')
    cfg.validate(plan['contract'], cfg.digest(publication.read_record(keychain.CONTEXT)))
    tree, manifest = published.resolve(setup.BASE/'instance', plan['package_sha256'], meta['public'], kind='migration')
    setup.capability(manifest)
    if (Path(script).resolve() != tree/setup.ENTRY or Path(__file__).resolve() != tree/setup.MODULE
            or Path(executable).resolve() != tree/'runtime/bin/python3.12'):
        raise ValueError('归档探针不属于正在执行的root批准程序')
    if plan['contract']['server_address'] in mounts.local_addresses():
        raise ValueError('NAS地址属于本机，不能批准个人代理')
    if request['phase'] != 'identity' and publication.read_record(setup.pending(plan['attempt_id'])) != raw:
        raise ValueError('归档探针没有本机root批准的固定计划')
    return context, plan


def anchor(plan, context):
    value = plan['contract']
    return storage.autofs_location(value, cfg.digest(boot.canonical(value)), context)


def check_fd(fd, context, mount, *, directory):
    info = os.fstat(fd)
    if (not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)
            or info.st_uid != context['uid'] or info.st_gid != context['gid']
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1) or mounts.fd_identity(fd) != mount['kernel']):
        raise ValueError('归档探针对象身份或权限不符')
    keychain._no_acl(fd)
    return info


@contextmanager
def directory(plan, context, expected, *, create=False):
    parent = os.open(cfg.DESTINATION, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        p = check_fd(parent, context, expected['mount'], directory=True)
        name = '.tianming-install-'+plan['attempt_id']
        if create:
            os.mkdir(name, 0o700, dir_fd=parent)  # Exclusive; never adopt an existing attempt.
            os.fsync(parent)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            initial = check_fd(fd, context, expected['mount'], directory=True)
            def recheck():
                current = check_fd(fd, context, expected['mount'], directory=True)
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                root = cfg.DESTINATION.stat(follow_symlinks=False)
                if (any((v.st_dev, v.st_ino) != (initial.st_dev, initial.st_ino) for v in (current, named))
                        or (root.st_dev, root.st_ino) != (p.st_dev, p.st_ino)
                        or anchor(plan, context) != expected):
                    raise ValueError('归档探针目录或挂载身份改变')
                check_fd(parent, context, expected['mount'], directory=True)
            recheck()
            yield fd, recheck
            recheck()
        finally: os.close(fd)
    finally: os.close(parent)


def proof(plan, location, digest):
    return {'attempt_id': plan['attempt_id'], 'contract_sha256': location['contract_sha256'],
            'context_sha256': plan['context_sha256'], 'path': str(cfg.DESTINATION/(
                '.tianming-install-'+plan['attempt_id'])/'complete.probe'), 'sha256': digest,
            'size': SIZE, 'mount': location['mount'], 'independent_readback': True}


def readback(plan, context, expected):
    import fcntl
    location = anchor(plan, context)
    if (not isinstance(expected, dict) or not cfg.sha(expected.get('sha256'))
            or expected != proof(plan, location, expected['sha256'])):
        raise ValueError('归档独立回读请求不符')
    with directory(plan, context, location) as (parent, recheck):
        fd = os.open('complete.probe', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            initial = check_fd(fd, context, location['mount'], directory=False)
            if initial.st_size != SIZE: raise ValueError('归档探针长度不符')
            fcntl.fcntl(fd, 48, 1)  # F_NOCACHE on this fresh process's own descriptor.
            result = hashlib.sha256(); remaining = SIZE
            while remaining:
                block = os.read(fd, min(remaining, 65536))
                if not block: raise ValueError('归档探针回读不完整')
                remaining -= len(block); result.update(block)
            if os.read(fd, 1): raise ValueError('归档探针读取期间增长')
            after = check_fd(fd, context, location['mount'], directory=False)
            named = os.stat('complete.probe', dir_fd=parent, follow_symlinks=False)
            if (storage._fingerprint(initial) != storage._fingerprint(after)
                    or storage._fingerprint(named) != storage._fingerprint(after)
                    or result.hexdigest() != expected['sha256']):
                raise ValueError('归档探针身份或完整摘要不同')
            recheck()
        finally: os.close(fd)
    return proof(plan, location, result.hexdigest())


def read_child(request, script, executable):
    process = subprocess.Popen([executable, '-I', '-B', script], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, pass_fds=(), cwd='/', umask=0o077,
        env={'HOME': str(setup.BASE/'home'), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
             'LANG': 'en_US.UTF-8', 'ERP_HOME_REHEARSAL': '1'})
    interrupted = False; first = True
    while True:
        try:
            output, _ = process.communicate(boot.canonical(request) if first else None)
            break
        except KeyboardInterrupt: interrupted = True; first = False
    if interrupted or process.returncode != 0: raise ValueError('独立归档回读未完成；保留现场')
    return boot.decode(output)


def reply(request, context):
    return {'type': 'tianming.archive-install-worker.v1', 'phase': request['phase'],
            'challenge': request['challenge'], 'plan_sha256': request['plan_sha256'],
            'identity': boot._expected(context['uid'], context['gid'])}


def run(request, *, script, executable):
    context, plan = authenticated(request, script, executable)
    result = reply(request, context)
    if request['phase'] == 'identity': return result
    if request['phase'] == 'readback': return {**result, 'proof': readback(plan, context, request['expected'])}
    location = anchor(plan, context); data = os.urandom(SIZE)
    expected = proof(plan, location, cfg.digest(data))
    with directory(plan, context, location, create=True) as (parent, recheck):
        fd = os.open('pending.probe', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            initial = check_fd(fd, context, location['mount'], directory=False)
            remaining = memoryview(data)
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0: raise ValueError('归档探针写入不完整')
                remaining = remaining[count:]
            os.fsync(fd)
            current = check_fd(fd, context, location['mount'], directory=False)
            named = os.stat('pending.probe', dir_fd=parent, follow_symlinks=False)
            if ((initial.st_dev, initial.st_ino) != (current.st_dev, current.st_ino)
                    or storage._fingerprint(current) != storage._fingerprint(named) or current.st_size != SIZE):
                raise ValueError('归档探针写入身份变化')
            recheck()
            if os.listdir(parent) != ['pending.probe']: raise ValueError('归档探针目录有额外对象')
            os.rename('pending.probe', 'complete.probe', src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            promoted = storage._fingerprint(check_fd(fd, context, location['mount'], directory=False))
            child_request = {**request, 'phase': 'readback', 'expected': expected}
            observed = read_child(child_request, script, executable)
            if observed != {**reply(child_request, context), 'proof': expected}:
                raise ValueError('独立归档回读身份、摘要或challenge不同')
            if (storage._fingerprint(os.stat('complete.probe', dir_fd=parent, follow_symlinks=False))
                    != promoted or storage._fingerprint(check_fd(fd, context, location['mount'], directory=False)) != promoted):
                raise ValueError('归档探针独立回读后文件已替换')
            recheck()
        finally: os.close(fd)
    return {**result, 'proof': expected}


def main(*, script, executable):
    request = boot.decode(sys.stdin.buffer.read(boot.LIMIT+1))
    result = run(request, script=script, executable=executable)
    sys.stdout.buffer.write(boot.canonical(result)+b'\n'); sys.stdout.buffer.flush()

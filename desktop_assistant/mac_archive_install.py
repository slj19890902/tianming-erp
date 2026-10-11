"""Reviewed stdlib autofs setup. No ERP, secrets, backup files or root NFS IO.

An interrupted apply keeps its pending plan and original master. It never
automatically rolls back, unmounts, retries or repairs a remote directory.
"""
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from desktop_assistant import mac_archive_contract as cfg
from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_release_publish as publication
from desktop_assistant import mac_system_service as service
from desktop_assistant import mac_mount as mounts

BASE = Path('/Library/TianmingERP')
CONTRACT = BASE/'archive-contract.json'
PLAN_TYPE = 'tianming.mac-autofs-plan.v1'
ENTRY = 'desktop_assistant/archive_probe_entry.py'
MODULE = 'desktop_assistant/mac_archive_probe.py'
FIELDS = {'type', 'attempt_id', 'created_at', 'expires_at', 'code_manifest_sha256',
          'package_sha256', 'context_sha256', 'publisher_sha256', 'registry_sha256',
          'master_before_sha256', 'contract'}


def pending(attempt): return BASE/('archive-install-'+attempt+'.pending.json')


def capability(manifest):
    value = manifest.get('runtime_capabilities', {}).get('archive_autofs_v1')
    if type(value) is not int or value != 1 or not {ENTRY, MODULE} <= manifest.get('files', {}).keys():
        raise ValueError('批准程序缺少专用归档探针协议')


def metadata(package):
    meta = service.metadata(package)
    with (meta['tree'].parent/'package.zip').open('rb') as stream:
        _, manifest, _ = publication.inspect(stream)
    capability(manifest)
    return meta


def vacant():
    # Cached mount inventory only; never stat/open the prospective remote key.
    if any(Path(m['mountpoint']) == alias or alias in Path(m['mountpoint']).parents
           for m in mounts.mounts() for alias in (cfg.MOUNT, BASE/'archive')):
        raise ValueError('已有归档挂载，不接管或卸载')
    with os.scandir(BASE) as entries: names = {entry.name for entry in entries}
    try: cfg.MAP.lstat()
    except FileNotFoundError: map_exists = False
    else: map_exists = True
    if 'archive-contract.json' in names or 'archive' in names or map_exists:
        raise ValueError('已有归档对象，不覆盖重试')
    if any(name.startswith('archive-install-') for name in names):
        raise ValueError('已有归档安装现场，须保留核查')


def make_plan(package, address, export):
    meta = metadata(package); vacant()
    original = cfg.read_configuration(cfg.MASTER)
    now = int(time.time())
    return {'type': PLAN_TYPE, 'attempt_id': uuid.uuid4().hex, 'created_at': now,
        'expires_at': now+7*86400, 'code_manifest_sha256': boot.verify_code(trusted=False),
        'package_sha256': package, **{k: meta[k] for k in
        ('context_sha256', 'publisher_sha256', 'registry_sha256')},
        'master_before_sha256': cfg.digest(original),
        'contract': cfg.create(meta['context_sha256'], address, export, original)}


def header(raw, approved_sha):
    if not cfg.sha(approved_sha) or cfg.digest(raw) != approved_sha:
        raise ValueError('归档安装计划批准摘要不符')
    plan = boot.decode(raw)
    if (boot.canonical(plan) != raw or set(plan) != FIELDS or plan['type'] != PLAN_TYPE
            or not isinstance(plan['attempt_id'], str) or not re.fullmatch('[0-9a-f]{32}', plan['attempt_id'])
            or any(type(plan[k]) is not int for k in ('created_at', 'expires_at'))
            or not 0 < plan['expires_at']-plan['created_at'] <= 7*86400
            or not plan['created_at'] <= time.time() < plan['expires_at']
            or any(not cfg.sha(plan[k]) for k in FIELDS if k.endswith('_sha256'))):
        raise ValueError('归档安装计划无效或过期')
    cfg.validate(plan['contract'], plan['context_sha256'])
    return plan


def binding(plan, meta):
    if any(plan[k] != meta[k] for k in ('context_sha256', 'publisher_sha256', 'registry_sha256')):
        raise ValueError('归档计划的本机身份或程序批准记录变化')


def validate(raw, approved_sha):
    plan = header(raw, approved_sha)
    if plan['code_manifest_sha256'] != boot.verify_code(): raise ValueError('管理员代码包已变化')
    meta = metadata(plan['package_sha256']); binding(plan, meta); vacant()
    original = cfg.read_configuration(cfg.MASTER)
    if (cfg.digest(original) != plan['master_before_sha256'] or plan['contract'] != cfg.create(
            plan['context_sha256'], plan['contract']['server_address'], plan['contract']['export'], original)):
        raise ValueError('auto_master或批准的派生配置已经变化')
    return plan, meta, original


def worker(meta, raw, phase):
    if phase not in {'identity', 'probe'}: raise ValueError('归档安装动作无效')
    challenge = uuid.uuid4().hex
    request = {'phase': phase, 'challenge': challenge, 'plan': boot.decode(raw), 'plan_sha256': cfg.digest(raw)}
    # Canonical raw bytes are required so the worker can independently bind the
    # protected pending record without transmitting a pathname chosen by stdin.
    if boot.canonical(request['plan']) != raw: raise ValueError('归档计划必须为规范原字节')
    context = meta['context']
    process = subprocess.Popen([str(meta['tree']/'runtime/bin/python3.12'), '-I', '-B', str(meta['tree']/ENTRY)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd='/',
        user=context['uid'], group=context['gid'], extra_groups=[], umask=0o077,
        close_fds=True, pass_fds=(), env={'HOME': str(BASE/'home'), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                                       'LANG': 'en_US.UTF-8', 'ERP_HOME_REHEARSAL': '1'})
    interrupted = False; first = True
    while True:
        try:
            output, _ = process.communicate(boot.canonical(request) if first else None)
            break
        except KeyboardInterrupt:
            interrupted = True; first = False
    if interrupted or process.returncode != 0: raise ValueError('专用归档探针未完成，保留现场')
    result = boot.decode(output)
    expected = {'type': 'tianming.archive-install-worker.v1', 'phase': phase, 'challenge': challenge,
        'plan_sha256': cfg.digest(raw), 'identity': boot._expected(context['uid'], context['gid'])}
    if phase == 'identity':
        if result != expected: raise ValueError('归档身份探针回执不符')
    elif set(result) != set(expected) | {'proof'} or any(result[k] != v for k, v in expected.items()):
        raise ValueError('归档写入探针回执不符')
    else:
        proof = result['proof']; plan = request['plan']
        expected_proof = {'attempt_id': plan['attempt_id'], 'contract_sha256': cfg.digest(boot.canonical(plan['contract'])),
            'context_sha256': plan['context_sha256'], 'size': 131072, 'path': str(cfg.DESTINATION/(
                '.tianming-install-'+plan['attempt_id'])/'complete.probe'), 'independent_readback': True}
        if (not isinstance(proof, dict) or set(proof) != set(expected_proof) | {'sha256', 'mount'}
                or any(proof[k] != v for k, v in expected_proof.items()) or not cfg.sha(proof['sha256'])
                or proof['independent_readback'] is not True or not cfg.matches(plan['contract'], proof['mount'])):
            raise ValueError('归档独立回读证据不符')
    return result


def apply(raw, approved_sha):
    identity, _ = boot.modules(); identity._administrator()
    validate(raw, approved_sha)
    with publication.lock():
        plan, meta, original = validate(raw, approved_sha)
        worker(meta, raw, 'identity')  # No NAS IO; checks actual UID and local addresses first.
        validate(raw, approved_sha)  # Nothing has been changed before this point.
        attempt = plan['attempt_id']
        boot._write(pending(attempt), raw, mode=0o644)
        boot._write(BASE/('archive-install-'+attempt+'.original-master'), original)
        publication.sync_directory(BASE)
        boot._write(cfg.MAP, cfg.map_bytes(plan['contract']), mode=0o644)
        replacement = cfg.MASTER.parent/('.auto_master.tianming-'+attempt)
        updated = cfg.append_master(original)
        boot._write(replacement, updated, mode=0o644)
        if cfg.read_configuration(cfg.MASTER) != original: raise ValueError('auto_master并发变化，保留待核查文件')
        os.replace(replacement, cfg.MASTER)
        publication.sync_directory(cfg.MASTER.parent)
        cfg.installed(plan['contract'], cfg.read_configuration)
        # Reread maps, without unmount/flush-all/force flags. No timeout kills a
        # potentially blocked hard-NFS operation or claims rollback completion.
        code, _, _ = service._communicate(subprocess.Popen(['/usr/sbin/automount', '-v'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, cwd='/', env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'}))
        if code: raise ValueError('系统自动挂载配置尚未确认，保留现场')
        result = worker(meta, raw, 'probe')
        binding(plan, metadata(plan['package_sha256']))
        if publication.read_record(pending(attempt)) != raw: raise ValueError('批准探针计划已经变化')
        cfg.installed(plan['contract'], cfg.read_configuration)
        header(raw, approved_sha)  # A hard-NFS probe can outlive its approved window.
        boot._write(CONTRACT, boot.canonical(plan['contract']), mode=0o644)
        receipt = {'type': 'tianming.mac-autofs-installed.v1', 'plan_sha256': approved_sha,
            'probe': result, 'contract_published': True, 'logout_reboot_verified': False,
            'remote_device_persistence_verified': False, 'service_started': False, 'factory_activated': False}
        boot._write(BASE/('archive-install-'+attempt+'.receipt.json'), boot.canonical(receipt), mode=0o644)
        publication.sync_directory(BASE)
        return receipt

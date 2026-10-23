"""Reviewed stdlib administrator broker for one fixed UAT export task.

Only plan/apply from the protected bootstrap pack reach native actions. The
signed worker captures under the service UID. Root copies verified clean files
into a new root-private directory and transfers that directory last.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import uuid

from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_release_publish as publication
from desktop_assistant import mac_system_service as system
from desktop_assistant import uat_export_contract as c


def _administrator():
    identity, _ = boot.modules(); identity._administrator()
    boot.verify_code()


def metadata(package):
    meta = system.metadata(package)
    c.require_capability(system._program_manifest(meta))
    control_raw = publication.read_record(system.CONTROL)
    control = boot.decode(control_raw)
    supervisor = system.metadata(control.get('package_sha256'))
    checked, definition = system.checked_control(supervisor)
    plist = system._installed_plist(supervisor)
    if checked != control_raw or not definition['enabled'] or not system.present():
        raise ValueError('系统监护不是完整启用的已注册身份')
    return meta, {'supervisor_package_sha256':supervisor['package'],
                  'control_sha256':c.digest(control_raw), 'plist_sha256':c.digest(plist)}


def worker(meta, action, *, plan=None):
    request = {'action':action, 'package_sha256':meta['package'], 'challenge':uuid.uuid4().hex,
        **{k:meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')}}
    if plan is not None: request.update(plan=plan, plan_sha256=c.digest(c.canonical(plan)))
    c.validate_request(request)
    context = meta['context']
    process = subprocess.Popen([str(meta['tree']/'runtime/bin/python3.12'), '-I', '-B',
        str(meta['tree']/system.MANAGE_ENTRY)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, user=context['uid'], group=context['gid'], extra_groups=[],
        close_fds=True, pass_fds=(), umask=0o077, cwd='/',
        env={'HOME':str(c.BASE/'home'),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
             'LANG':'en_US.UTF-8','ERP_HOME_REHEARSAL':'1'})
    interrupted = False; payload = c.canonical(request)
    while True:
        try:
            raw, _ = process.communicate(payload); break
        except KeyboardInterrupt: interrupted = True; payload = None
    if interrupted or process.returncode:
        raise ValueError('固定导出 worker 未完成，保留私有现场，不交付')
    value = boot.decode(raw)
    expected = {'type':'tianming.uat-export-worker.v1','action':action,
        'package_sha256':meta['package'],'challenge':request['challenge'],
        'identity':boot._expected(context['uid'],context['gid'])}
    fields = set(expected)|{'source'}|({'capture'} if action == 'export-uat' else set())
    if set(value) != fields or any(value.get(k) != v for k,v in expected.items()):
        raise ValueError('导出 worker 身份或挑战回执不符')
    c.validate_source(value['source'])
    if action == 'export-uat':
        capture = value['capture']
        if (value['source'] != plan['source'] or not isinstance(capture,dict)
                or set(capture) != {'receipt_sha256','files','bytes'} or not c.sha(capture['receipt_sha256'])
                or type(capture['files']) is not int or not 2 <= capture['files'] <= c.MAX_FILES+2
                or type(capture['bytes']) is not int or not 0 < capture['bytes'] <= c.MAX_BYTES):
            raise ValueError('导出 worker 成品回执无效')
    return value


def make_plan(package):
    _administrator(); who = c.recipient()
    meta, supervisor = metadata(package)
    source = worker(meta, 'inspect-uat')['source']
    now = int(time.time()); task_attempt = uuid.uuid4().hex
    plan = {'type':c.PLAN_TYPE, 'task':c.TASK, 'attempt_id':task_attempt,
        'created_at':now, 'expires_at':now+3600, 'code_manifest_sha256':boot.verify_code(),
        'package_sha256':package, **{k:meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')},
        **supervisor, 'recipient':who, 'source':source, 'output':str(c.output(task_attempt)), 'policy':c.POLICY}
    c.validate_plan(plan)
    if metadata(package)[1] != supervisor or c.recipient() != who:
        raise ValueError('生成计划期间监护或控制台身份变化')
    raw = c.canonical(plan)
    environment = c.read(c.ROOT/'shared/environment.json', uid=meta['context']['uid'], limit=boot.LIMIT)
    if c.digest(environment) != source['environment_sha256']:
        raise ValueError('生成计划期间运行配置身份变化')
    c.reject_environment_credentials(environment, raw)
    # A public plan contains no business rows, original names or secrets.
    # It is immutable, root-owned, and bound by explicit SHA at apply.
    boot._write(c.plan_path(task_attempt), raw, mode=0o644)
    return {'type':c.PLAN_TYPE,'attempt_id':task_attempt,'plan_sha256':c.digest(raw),
            'plan_file':str(c.plan_path(task_attempt)),'captured':False,'plan':plan}


def validate(attempt_id, approved_sha):
    _administrator(); c.attempt(attempt_id)
    raw = boot.protected(c.plan_path(attempt_id))
    plan = c.validate_plan(boot.decode(raw), approved_sha=approved_sha)
    if raw != c.canonical(plan) or plan['attempt_id'] != attempt_id:
        raise ValueError('批准计划原字节或任务身份不符')
    if plan['code_manifest_sha256'] != boot.verify_code(): raise ValueError('管理员代码包已变化')
    meta, supervisor = metadata(plan['package_sha256'])
    if any(plan[k] != meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')) or any(
            plan[k] != v for k,v in supervisor.items()):
        raise ValueError('批准后的程序、监护或安装身份变化')
    return plan, meta


def _directory_ok(fd, path):
    info = os.fstat(fd)
    owner = c.RECIPIENT_UID if path == c.RECIPIENT_HOME or c.RECIPIENT_HOME in path.parents else 0
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError('固定导出目的地父目录保护无效')
    if path == c.RECIPIENT_HOME: c.home_acl(fd)
    else: boot.no_acl(fd)
    if path != Path('/'):
        try: os.stat('.git', dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError: pass
        else: raise ValueError('导出目录不得位于 Git 工作区')


@contextmanager
def output_parent():
    """Open a recipient-prepared fixed chain; root never changes its owners."""
    opened = []
    try:
        fd = os.open('/', os.O_RDONLY|os.O_DIRECTORY)
        opened.append((Path('/'),fd,c.dir_stamp(os.fstat(fd))))
        current = Path('/')
        for name in c.OUTPUT.parts[1:]:
            current /= name
            child = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=fd)
            _directory_ok(child,current)
            fd = child; opened.append((current,fd,c.dir_stamp(os.fstat(fd))))
        def check():
            c.recipient()
            for named, fd, initial in opened:
                if c.dir_stamp(os.fstat(fd)) != initial or c.dir_stamp(
                        named.stat(follow_symlinks=False)) != initial:
                    raise ValueError('固定导出目的地被替换')
                _directory_ok(fd,named)
        check(); yield opened[-1][1], check; check()
    finally:
        for _,fd,_ in reversed(opened): os.close(fd)


def private_directory(fd):
    info=os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('管理员新交付目录必须为 root 私有')
    boot.no_acl(fd)


@contextmanager
def delivery_staging(plan):
    # The recipient cannot rename children of this protected root-owned parent.
    # Thus mkdir -> open cannot be redirected into an unrelated user directory.
    boot.protected(c.BASE,directory=True)
    base_fd=os.open(c.BASE,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    outer_fd=payload_fd=None
    try:
        outer='uat-export-delivery-'+plan['attempt_id']
        os.mkdir(outer,mode=0o700,dir_fd=base_fd)
        outer_fd=os.open(outer,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=base_fd)
        private_directory(outer_fd)
        if os.listdir(outer_fd): raise ValueError('新管理员交付目录不是空目录')
        os.mkdir('payload',mode=0o700,dir_fd=outer_fd)
        payload_fd=os.open('payload',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=outer_fd)
        private_directory(payload_fd)
        yield outer_fd,payload_fd
    finally:
        for fd in (payload_fd,outer_fd,base_fd):
            if fd is not None: os.close(fd)


def publish_directory(source_fd,parent_fd,name):
    """macOS atomic create-only directory rename; no overwrite fallback."""
    libc=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    rename=libc.renameatx_np
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype=ctypes.c_int
    # SDK sys/stdio.h: RENAME_EXCL = 0x00000004 (macOS >=10.12).
    if rename(source_fd,b'payload',parent_fd,name.encode('ascii'),0x00000004) != 0:
        raise OSError(ctypes.get_errno(),'固定导出目录原子发布失败；不覆盖目标')


def _receipt(raw, plan, capture):
    # Duplicate keys/nonfinite values are rejected without the small status limit.
    def unique(pairs):
        value = {}
        for key,item in pairs:
            if key in value: raise ValueError('导出清单含重复字段')
            value[key] = item
        return value
    value = json.loads(raw,object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限回执')))
    if (c.digest(raw) != capture['receipt_sha256'] or c.canonical(value) != raw
            or value.get('type') != c.RECEIPT_TYPE or value.get('task') != c.TASK
            or value.get('attempt_id') != plan['attempt_id'] or value.get('source') != plan['source']
            or value.get('plan_sha256') != c.digest(c.canonical(plan))
            or value.get('environment_credential_check') != {'policy': c.ENVIRONMENT_POLICY,
                'environment_sha256': plan['source']['environment_sha256'], 'checked': True}
            or value.get('pii_anonymized') is not False
            or value.get('source_business_written') is not False or value.get('service_stopped') is not False):
        raise ValueError('成品来源回执不属于批准计划')
    files = value.get('files')
    if not isinstance(files,dict) or not {'database.sqlite3','layout.json'} <= files.keys():
        raise ValueError('成品缺少数据库或地图')
    for name, info in files.items():
        c.relative(name)
        if (name not in {'database.sqlite3','layout.json'} and not name.startswith('originals/')):
            raise ValueError('成品包含未授权文件')
        if name.startswith('originals/'): c.original_relative(name[len('originals/'):])
        if (not isinstance(info,dict) or set(info) != {'sha256','bytes'} or not c.sha(info['sha256'])
                or type(info['bytes']) is not int or not 0 <= info['bytes'] <= c.MAX_BYTES):
            raise ValueError('成品文件摘要/大小无效')
    if len(files) != capture['files'] or sum(i['bytes'] for i in files.values()) != capture['bytes']:
        raise ValueError('成品清单总量不符')
    return value


def deliver(plan, meta, capture):
    source = c.staging(plan['attempt_id'])/'payload'; uid = meta['context']['uid']
    raw = c.read(source/'receipt.json',uid=uid)
    receipt = _receipt(raw,plan,capture)
    from desktop_assistant.uat_export_sanitize import verify_sanitized, verify_credential_values_absent
    environment = c.read(c.ROOT/'shared/environment.json', uid=uid, limit=boot.LIMIT)
    if c.digest(environment) != plan['source']['environment_sha256']:
        raise ValueError('管理员复核前运行配置身份变化')
    credentials = c.environment_credentials(environment)
    # Validate within the protected service staging chain, not via a pathname
    # below a user-writable parent. The later FD copy is bound to this hash.
    with c.regular(source/'database.sqlite3',uid=uid):
        verified = verify_sanitized(source/'database.sqlite3')
    sanitized = receipt.get('sanitization')
    if (not isinstance(sanitized,dict) or set(sanitized) != set(verified)|{'source_sha256'}
            or not c.sha(sanitized['source_sha256'])
            or {k:v for k,v in sanitized.items() if k != 'source_sha256'} != verified
            or verified['sanitized_sha256'] != receipt['files']['database.sqlite3']['sha256']):
        raise ValueError('管理员独立去凭据复核不符')
    # Root reads only individually bound clean outputs; never the raw snapshots.
    with output_parent() as (parent_fd,check), delivery_staging(plan) as (stage_fd,dest_fd):
        check(); name = plan['attempt_id']
        opened = {'':dest_fd}; handles = []
        initial = c.dir_stamp(os.fstat(dest_fd))
        try:
            def anchored():
                check()
                if c.dir_stamp(os.stat('payload',dir_fd=stage_fd,follow_symlinks=False)) != initial:
                    raise ValueError('新导出目录被替换')
            members = {**receipt['files'], 'receipt.json':{'sha256':c.digest(raw),'bytes':len(raw)}}
            for relative,info in sorted(members.items()):
                anchored(); parts = Path(relative).parts; rel_parent = ''; fd = dest_fd
                for part in parts[:-1]:
                    child_key = str(Path(rel_parent)/part)
                    if child_key not in opened:
                        os.mkdir(part,mode=0o700,dir_fd=fd)
                        opened[child_key] = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    fd = opened[child_key]; rel_parent = child_key
                target = os.open(parts[-1],os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
                handles.append(target); digest = hashlib.sha256(); count = 0
                with c.regular(source/relative,uid=uid) as (source_fd,_):
                    while True:
                        chunk = os.read(source_fd,1024**2)
                        if not chunk: break
                        digest.update(chunk); count += len(chunk)
                        if count > info['bytes']: raise ValueError('成品复制大小超过冻结清单')
                        remaining = memoryview(chunk)
                        while remaining:
                            written=os.write(target,remaining)
                            if written <= 0: raise ValueError('成品文件写入不完整')
                            remaining = remaining[written:]
                os.fsync(target)
                if count != info['bytes'] or digest.hexdigest() != info['sha256']:
                    raise ValueError('成品复制摘要不符；目录保持 root 私有')
            anchored()
            # Recheck the actual root-private copied bytes, not a worker claim.
            private_payload = c.BASE/('uat-export-delivery-'+name)/'payload'
            for relative in members:
                verify_credential_values_absent(private_payload/relative, credentials,
                    database=relative=='database.sqlite3',
                    json_document=relative in {'layout.json', 'receipt.json'})
            anchored()
            # Revalidate plan + installed identities after all IO, before the
            # sole access grant. No formal permissions are modified.
            validate(name,c.digest(c.canonical(plan))); anchored()
            if worker(meta,'inspect-uat')['source'] != plan['source']:
                raise ValueError('交付前运行来源身份变化')
            with final_source_guard(plan,meta):
                who = c.recipient()
                for target in handles: os.fchown(target,who['uid'],who['gid'])
                for key, fd in sorted(opened.items(),key=lambda pair:len(pair[0]),reverse=True):
                    if key: os.fchown(fd,who['uid'],who['gid'])
                anchored(); os.fsync(dest_fd)
                os.fchown(dest_fd,who['uid'],who['gid'])
                # Outer root0700 keeps these owned files inaccessible until the
                # verified payload is moved atomically into the fixed recipient.
                check(); publish_directory(stage_fd,parent_fd,name)
                os.fsync(stage_fd)
                os.fsync(parent_fd)
            return {'type':c.RECEIPT_TYPE,'task':c.TASK,'attempt_id':name,
                'output':str(c.output(name)),'receipt_sha256':c.digest(raw),'credential_sanitized':True,
                'pii_anonymized':False,'formal_business_written':False,'service_stopped':False}
        finally:
            for fd in handles: os.close(fd)
            for key,fd in reversed(list(opened.items())):
                if key: os.close(fd)


def apply(attempt_id, approved_sha):
    plan, meta = validate(attempt_id,approved_sha)
    # Test destination before capturing private business content. Do not grant
    # recipient access or create an attempt directory until capture verifies.
    with output_parent() as (fd,check):
        check()
        try: os.stat(attempt_id,dir_fd=fd,follow_symlinks=False)
        except FileNotFoundError: pass
        else: raise ValueError('导出任务已有现场，不覆盖重试')
    result = worker(meta,'export-uat',plan=plan)
    validate(attempt_id,approved_sha)
    return deliver(plan,meta,result['capture'])


@contextmanager
def final_source_guard(plan,meta):
    """Existing maintenance FD excludes lifecycle changes through final grant.

    No new lock file, credentials, business connection or process mutation.
    Normal business writes continue; this binds the captured instance, not a
    promise that a live database stops changing after its snapshot.
    """
    import fcntl
    uid=meta['context']['uid']
    with c.regular(c.ROOT/'control/operation.lock',uid=uid,limit=1) as (fd,_):
        if os.read(fd,2) != b'0': raise ValueError('移交维护锁不是已有完整锁')
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('移交前已有其他维护操作') from None
        try:
            source=plan['source']
            for path,key,limit in [('state.json','state_sha256',c.MAX_MANIFEST),
                    ('control/process.json','process_sha256',boot.LIMIT),
                    ('shared/environment.json','environment_sha256',boot.LIMIT)]:
                if c.digest(c.read(c.ROOT/path,uid=uid,limit=limit)) != source[key]:
                    raise ValueError('移交锁内运行来源身份变化')
            os.kill(source['pid'],0)  # Existence only; kernel identity was checked by worker.
            yield
        finally: fcntl.flock(fd,fcntl.LOCK_UN)

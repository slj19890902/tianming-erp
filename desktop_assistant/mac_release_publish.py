"""Administrator publication, invoked only through the verified stdlib installer.

The reviewed plan pins every input byte. Root only copies/checks data. Signature
verification runs in a fresh dedicated-UID process using the approved runtime;
neither ERP nor third-party code is imported by the privileged interpreter.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
import zipfile

from desktop_assistant import mac_keychain_install as bootstrap

BASE = Path('/Library/TianmingERP')
DATA_VOLUME = Path('/System/Volumes/Data')
TYPE = 'tianming.mac-publication-plan.v1'
PRISTINE_TYPE = 'tianming.mac-pristine-publication-plan.v1'
REGISTRY_TYPE = 'tianming.mac-published.v1'
MANAGED = {'data':'data', 'static/uploads':'legacy_uploads',
           'factory_twin/data':'factory_twin_data', 'logs':'logs'}
REQUIRED = {'main.py', 'app/main.py', 'desktop_assistant/server_entry.py',
            'desktop_assistant/migration_entry.py', 'runtime/bin/python3.12',
            'runtime/ocr/model/craft_mlt_25k.pth', 'runtime/ocr/model/zh_sim_g2.pth',
            'runtime/ocr/user_network/.keep'}
FIELDS = {'type', 'attempt_id', 'created_at', 'expires_at', 'code_manifest_sha256',
          'context_sha256', 'package_sha256', 'package_bytes', 'manifest_sha256',
          'public_key', 'publisher_sha256'}
MAX_PACKAGE = 8 * 1024**3
MAX_FILES_BYTES = 16 * 1024**3


def digest(stream):
    result = hashlib.sha256()
    while True:
        block = stream.read(1024**2)
        if not block: return result.hexdigest()
        result.update(block)


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('发布清单含重复字段')
            result[key] = value
        return result
    def constant(_): raise ValueError('发布清单含非法数值')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def _name(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or '\0' in name
            or name.startswith('/') or any(p in {'', '.', '..'} for p in name.split('/'))
            or PurePosixPath(name).as_posix() != name):
        raise ValueError('程序包路径不安全')
    return name


def inspect(stream):
    """No imports/extraction; reject ambiguous trees before any root writes."""
    stream.seek(0)
    with zipfile.ZipFile(stream) as archive:
        entries = archive.infolist()
        names = [_name(e.filename) for e in entries]
        names_set = set(names)
        if not 3 <= len(names) <= 100002 or len(names_set) != len(names):
            raise ValueError('程序包文件清单重复或过大')
        prefixes = {}
        for name in names:
            for prefix in [PurePosixPath(name), *PurePosixPath(name).parents]:
                if str(prefix) == '.': continue
                key = unicodedata.normalize('NFC', str(prefix)).casefold()
                if key in prefixes and prefixes[key] != str(prefix):
                    raise ValueError('程序路径存在大小写或Unicode冲突')
                prefixes[key] = str(prefix)
            if any(str(parent) in names_set for parent in PurePosixPath(name).parents):
                raise ValueError('程序文件与目录路径冲突')
        if any(stat.S_IFMT(e.external_attr >> 16) not in (0, stat.S_IFREG)
               or e.flag_bits & 1 for e in entries):
            raise ValueError('程序包不允许链接、目录、特殊或加密成员')
        total = sum(e.file_size for e in entries)
        if total > MAX_FILES_BYTES or archive.getinfo('manifest.json').file_size > 16 * 1024**2:
            raise ValueError('程序包展开长度超限')
        raw = archive.read('manifest.json')
        manifest = _decode(raw)
        if (not isinstance(manifest, dict) or manifest.get('type') != 'tianming.release.v1'
                or manifest.get('runtime_platform') != 'macos-arm64'):
            raise ValueError('不是Mac原生签名发布包')
        caps = manifest.get('runtime_capabilities')
        if (not isinstance(caps, dict) or type(caps.get('immutable_execution_v1')) is not int
                or caps['immutable_execution_v1'] != 1):
            raise ValueError('发布包未实现不可变执行协议')
        files = manifest.get('files')
        if (not isinstance(files, dict) or not REQUIRED <= files.keys()
                or set(names) != set(files) | {'manifest.json', 'manifest.sig'}
                or len(archive.read('manifest.sig')) != 64):
            raise ValueError('程序包缺少签名文件或必需入口')
        for name, value in files.items():
            _name(name)
            if (not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
                    or name in {'manifest.json', 'manifest.sig', '.env'}
                    or any(name == p or name.startswith(p+'/') for p in MANAGED)):
                raise ValueError('程序包包含业务数据、保留路径或无效摘要')
        executable = manifest.get('executable_files')
        if (not isinstance(executable, list) or any(not isinstance(n, str) for n in executable)
                or len(set(executable)) != len(executable) or not set(executable) <= files.keys()
                or 'runtime/bin/python3.12' not in executable):
            raise ValueError('发布包执行权限清单不完整')
        return raw, manifest, total


SERVICE_RESTORE_FILES = {'desktop_assistant/service_restore_entry.py',
                         'desktop_assistant/mac_service_restore.py'}


def require_service_restore(manifest):
    """Only pristine publication/explicit native recovery need this protocol."""
    caps = manifest.get('runtime_capabilities')
    readers = manifest.get('reader_capabilities')
    if (not isinstance(caps, dict) or any(type(caps.get(k)) is not int or caps[k] != 1
            for k in ('immutable_execution_v1', 'system_service_v1', 'service_restore_v1'))
            or not isinstance(readers, dict) or type(readers.get('restore_provenance_v1')) is not int
            or readers['restore_provenance_v1'] != 1
            or not SERVICE_RESTORE_FILES <= manifest.get('files', {}).keys()):
        raise ValueError('签名程序缺少专用完整恢复协议或来源保留能力')


def prepare(package, public, context_raw, code_manifest_raw, *, publisher_sha256, pristine_bootstrap=False):
    """Unprivileged Python 3.12 preparation; no system modifications."""
    from desktop_assistant.release_request import inspect_package, public_identity
    key, fingerprint = public_identity(public)
    if fingerprint != publisher_sha256:
        raise ValueError('不是独立交接回执中的既有工厂发布身份')
    from cryptography.hazmat.primitives import serialization
    public = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    code = bootstrap.decode(code_manifest_raw)
    if code.get('type') != 'tianming.keychain-code.v1' or set(code.get('files', {})) != set(bootstrap.FILES):
        raise ValueError('发布计划需要本版最小管理员代码包')
    context = bootstrap.decode(context_raw)
    if context.get('type') != 'tianming.service-keychain.v1':
        raise ValueError('需要已初始化的系统服务context')
    raw, _ = inspect_package(package, public)
    with Path(package).open('rb') as stream:
        checked, manifest, _ = inspect(stream)
        stream.seek(0); package_sha = digest(stream)
    if raw != checked: raise ValueError('计划准备期间程序包变化')
    size = Path(package).stat().st_size
    if not 0 < size <= MAX_PACKAGE: raise ValueError('程序包长度超限')
    now = int(time.time())
    if type(pristine_bootstrap) is not bool: raise ValueError('首次发布选择必须明确为布尔值')
    if pristine_bootstrap: require_service_restore(manifest)
    return {'type':PRISTINE_TYPE if pristine_bootstrap else TYPE,
            'attempt_id':uuid.uuid4().hex, 'created_at':now, 'expires_at':now+7*86400,
            'code_manifest_sha256':bootstrap.digest(code_manifest_raw),
            'context_sha256':bootstrap.digest(context_raw), 'package_sha256':package_sha,
            'package_bytes':size, 'manifest_sha256':bootstrap.digest(raw),
            'public_key':public.decode('ascii'), 'publisher_sha256':fingerprint}


def validate(raw, approved_sha):
    if bootstrap.digest(raw) != approved_sha: raise ValueError('发布计划与批准摘要不同')
    value = bootstrap.decode(raw)
    if (set(value) != FIELDS or value['type'] not in (TYPE, PRISTINE_TYPE)
            or not isinstance(value['attempt_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['attempt_id'])
            or any(type(value[k]) is not int for k in ('created_at','expires_at','package_bytes'))
            or not 0 < value['expires_at']-value['created_at'] <= 7*86400
            or not value['created_at'] <= time.time() < value['expires_at']
            or not 0 < value['package_bytes'] <= MAX_PACKAGE
            or any(not isinstance(value[k], str) or not re.fullmatch('[0-9a-f]{64}', value[k])
                   for k in FIELDS if k.endswith('_sha256'))
            or not isinstance(value['public_key'], str) or not 0 < len(value['public_key']) <= 4096
            or bootstrap.digest(value['public_key'].encode('ascii')) != value['publisher_sha256']
            or value['code_manifest_sha256'] != bootstrap.verify_code()):
        raise ValueError('发布计划无效、过期或管理员代码包不匹配')
    return value


def guard(path, *, uid=0, directory=False, mode=0o644, link=False):
    if link:
        info = path.lstat()
        if (not stat.S_ISLNK(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o755):
            raise ValueError('托管数据链接不受root保护或不能由服务身份读取')
        return  # Its separately checked root directory protects this link.
    _, keychain = bootstrap.modules()
    with keychain._checked_path(path, uid=uid, directory=directory, mode=mode): pass


def read_record(path):
    guard(path)
    return bootstrap.protected(path)


def preflight(plan):
    identity, keychain = bootstrap.modules()
    identity._administrator()
    raw = read_record(keychain.CONTEXT)
    context = bootstrap.decode(raw)
    keychain._identity(context, administrator=True)
    if bootstrap.digest(raw) != plan['context_sha256']:
        raise ValueError('发布计划不属于本机服务context')
    root = BASE/'instance'
    paths = (root,) if plan['type'] == PRISTINE_TYPE else (
        root, root/'shared', *(root/'shared'/v for v in MANAGED.values()))
    for path in paths:
        if path.resolve() != path.absolute() or path.stat().st_dev != BASE.stat().st_dev:
            raise ValueError('服务数据目录含链接或不在本机服务磁盘')
        guard(path, uid=context['uid'], directory=True, mode=0o700)
    public = BASE/'publisher-public.pem'
    if public.exists() or public.is_symlink():
        if read_record(public) != plan['public_key'].encode('ascii'):
            raise ValueError('不能替换本机已固定的发布身份')
    return context


def _no_service():
    # This remains stdlib-only in the reviewed administrator package.
    from desktop_assistant import mac_system_service as service
    for path in (service.PLIST, *BASE.iterdir()):
        if path == service.PLIST or path.name.startswith(('system-service.', 'system-service-')):
            try: path.lstat()
            except FileNotFoundError: continue
            raise ValueError('首次发布发现系统服务或安装现场，保留原件')
    if service.present(): raise ValueError('首次发布不允许已有系统服务注册')


@contextmanager
def pristine_instance(plan, context):
    """Keep the same empty instance open until approval; never initialize it.

    Directory descriptors and repeated observations detect replacement/change;
    they are not a mandatory lock against all possible service-UID writers.
    """
    if plan['type'] != PRISTINE_TYPE:
        yield None
        return
    _, keychain = bootstrap.modules()
    root = BASE/'instance'
    with ExitStack() as stack:
        base_fd = stack.enter_context(keychain._checked_path(BASE, uid=0, directory=True, mode=0o755))
        instance_fd = stack.enter_context(keychain._checked_path(root, uid=context['uid'], directory=True, mode=0o700))
        base_identity = os.fstat(base_fd)
        before = os.fstat(instance_fd)
        def fingerprint(info):
            return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
                    info.st_mtime_ns, info.st_ctime_ns)
        def verify():
            actual = os.fstat(instance_fd)
            base_path = BASE.lstat()
            named = os.stat('instance', dir_fd=base_fd, follow_symlinks=False)
            if (not stat.S_ISDIR(actual.st_mode) or actual.st_uid != context['uid']
                    or actual.st_gid != context['gid'] or stat.S_IMODE(actual.st_mode) != 0o700
                    or actual.st_dev != base_identity.st_dev or base_identity.st_dev != DATA_VOLUME.stat().st_dev
                    or (base_path.st_dev, base_path.st_ino) != (base_identity.st_dev, base_identity.st_ino)
                    or fingerprint(named) != fingerprint(actual) or fingerprint(actual) != fingerprint(before)
                    or os.listdir(instance_fd)):
                raise ValueError('首次发布要求固定且完全为空的私有实例，目录变化时保留现场')
            keychain._no_acl(base_fd)
            keychain._no_acl(instance_fd)
            _no_service()
        verify()
        # Any old slot, registry or attempt (including dangling links) is an
        # existing installation/failed attempt, not a reusable bootstrap.
        if any(p.name in {'published', 'published.json'}
               or p.name.startswith(('publication-', 'published-')) for p in BASE.iterdir()):
            raise ValueError('首次发布已有发布记录或失败现场，不覆盖重试')
        yield verify
        verify()


def _registry(plan):
    path = BASE/'published.json'
    value = {'type':REGISTRY_TYPE, 'context_sha256':plan['context_sha256'],
             'publisher_sha256':plan['publisher_sha256'], 'packages':[]}
    raw = None
    if path.exists() or path.is_symlink():
        raw = read_record(path)
        actual = bootstrap.decode(raw)
        packages = actual.get('packages')
        if (set(actual) != set(value) or any(actual[k] != value[k] for k in value if k != 'packages')
                or not isinstance(packages, list) or not 1 <= len(packages) < 32
                or any(not isinstance(p, str) or not re.fullmatch('[0-9a-f]{64}',p) for p in packages)
                or packages != sorted(set(packages))):
            raise ValueError('既有发布记录损坏或身份不同，保留现场')
        value = actual
    if plan['package_sha256'] in value['packages']:
        raise ValueError('程序已经批准，不覆盖重复发布')
    return raw, {**value, 'packages':sorted([*value['packages'], plan['package_sha256']])}


@contextmanager
def lock():
    import fcntl
    path = BASE/'publication.lock'
    try: bootstrap._write(path, b'publication lock\n')
    except FileExistsError: pass
    guard(path, mode=0o600)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(fd)


def _directory(path):
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    guard(path, directory=True, mode=0o755)


def sync_directory(path):
    fd = os.open(path,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def _extract(package, destination, manifest, raw):
    _directory(destination)
    with zipfile.ZipFile(package) as archive:
        for name, expected in manifest['files'].items():
            target = destination/name
            missing = []
            parent = target.parent
            while parent != destination and not parent.exists():
                missing.append(parent); parent = parent.parent
            for parent in reversed(missing): _directory(parent)
            mode = 0o755 if name in manifest['executable_files'] else 0o644
            with archive.open(name) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output, 1024**2)
                output.flush(); os.fchmod(output.fileno(),mode); os.fsync(output.fileno())
            guard(target, mode=mode)
            with target.open('rb') as stream:
                if digest(stream) != expected: raise ValueError('执行副本文件摘要不符，保留现场')
    # Large application manifests exceed the bootstrap's small-record limit.
    target = destination/'manifest.json'
    with target.open('xb') as output:
        output.write(raw); output.flush(); os.fchmod(output.fileno(),0o644); os.fsync(output.fileno())
    guard(target)
    if target.read_bytes() != raw: raise ValueError('执行副本清单回读不符')


def check_tree(tree, manifest, raw, *, server):
    actual, links = set(), set()
    pending = [tree]
    while pending:
        directory = pending.pop()
        guard(directory, directory=True, mode=0o755)
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                name = path.relative_to(tree).as_posix()
                if entry.is_symlink():
                    if (not server or name not in MANAGED
                            or path.resolve() != BASE/'instance/shared'/MANAGED[name]):
                        raise ValueError('执行副本含非托管链接')
                    guard(path, link=True); links.add(name)
                elif entry.is_dir(follow_symlinks=False): pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    actual.add(name)
                    guard(path, mode=0o755 if name in manifest['executable_files'] else 0o644)
                    with path.open('rb') as stream:
                        expected = bootstrap.digest(raw) if name == 'manifest.json' else manifest['files'].get(name)
                        if digest(stream) != expected: raise ValueError('完整执行树逐项回读不符')
                else: raise ValueError('执行副本含特殊文件')
    if actual != set(manifest['files']) | {'manifest.json'} or links != (set(MANAGED) if server else set()):
        raise ValueError('完整执行树文件或托管链接清单不符')


def worker(request):
    """Runs after exec as the dedicated UID; verifies Ed25519 without ERP imports."""
    bootstrap.verify_code()
    plan, context = request['plan'], request['context']
    if bootstrap._actual() != bootstrap._expected(context['uid'],context['gid']) or os.getuid() == 0:
        raise ValueError('发布验签进程未降到专用身份')
    package = BASE/'published'/plan['package_sha256']/'package.zip'
    guard(package)
    with package.open('rb') as stream:
        if digest(stream) != plan['package_sha256']: raise ValueError('签名原包摘要变化')
        raw, _, _ = inspect(stream)
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive: signature = archive.read('manifest.sig')
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = serialization.load_pem_public_key(plan['public_key'].encode('ascii'))
    if not isinstance(key, Ed25519PublicKey): raise ValueError('不是Ed25519发布身份')
    key.verify(signature, raw)
    if bootstrap.digest(raw) != plan['manifest_sha256']: raise ValueError('签名清单不符')
    for relative, folder in MANAGED.items():
        link = package.parent/'server'/relative
        guard(link.parent, directory=True, mode=0o755)
        guard(link, link=True)
        # Read the link as the actual service UID, including pristine targets
        # which do not exist yet. Darwin enforces the link's own read mode.
        if os.readlink(link) != str(BASE/'instance/shared'/folder):
            raise ValueError('专用身份读取到非托管数据链接')
    return {'challenge':request['challenge'], 'identity':bootstrap._actual(),
            'package_sha256':plan['package_sha256'], 'signature_verified':True,
            'managed_links_readable':True}


def verify_child(plan, context, slot):
    request = {'plan':plan, 'context':context, 'challenge':uuid.uuid4().hex}
    python = slot/'migration/runtime/bin/python3.12'
    guard(python, mode=0o755)
    process = subprocess.Popen([str(python),'-I','-B',str(bootstrap.code_root()/'desktop_assistant/mac_keychain_install.py'),
                                '_publish_worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        user=context['uid'], group=context['gid'], extra_groups=[], close_fds=True, pass_fds=(),
        umask=0o077, cwd='/', env={'HOME':str(BASE/'home'),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8'})
    data = bootstrap.canonical(request)
    interrupted = False
    while True:
        try:
            output, _ = process.communicate(data)
            break
        except KeyboardInterrupt:
            data = None; interrupted = True
    if interrupted or process.returncode != 0:
        raise ValueError('专用身份验签未完成，已观察进程退出；保留现场')
    result = bootstrap.decode(output)
    if result != {'challenge':request['challenge'], 'identity':bootstrap._expected(context['uid'],context['gid']),
                  'package_sha256':plan['package_sha256'], 'signature_verified':True,
                  'managed_links_readable':True}:
        raise ValueError('专用身份验签回执不匹配')


def publish(raw, approved_sha, package):
    if os.getuid() != 0 or os.geteuid() != 0: raise ValueError('程序发布需要实际管理员身份')
    plan = validate(raw, approved_sha)
    identity, _ = bootstrap.modules()
    identity._administrator()
    context = preflight(plan)
    with lock(), pristine_instance(plan, context) as verify_pristine:
        previous, registry = _registry(plan)
        destination = BASE/'published'
        if destination.exists() or destination.is_symlink():
            guard(destination, directory=True, mode=0o755)
        slot = destination/plan['package_sha256']
        if slot.exists() or slot.is_symlink(): raise ValueError('存在既有发布现场，不覆盖重试')
        fd = os.open(package, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != plan['package_bytes']:
                raise ValueError('输入不是计划绑定的完整程序文件')
            if digest(source) != plan['package_sha256']: raise ValueError('输入程序包摘要不符')
            manifest_raw, manifest, total = inspect(source)
            if plan['type'] == PRISTINE_TYPE:
                require_service_restore(manifest)
            if bootstrap.digest(manifest_raw) != plan['manifest_sha256']:
                raise ValueError('程序清单不属于批准计划')
            if shutil.disk_usage(BASE).free < total*2+before.st_size+1024**3:
                raise ValueError('本机空间不足以完整保留两份执行副本')
            if not destination.exists(): _directory(destination)
            _directory(slot)
            bootstrap._write(BASE/('publication-'+plan['attempt_id']+'.pending.json'), raw)
            cached = slot/'package.zip'
            source.seek(0)
            with cached.open('xb') as output:
                shutil.copyfileobj(source,output,1024**2)
                output.flush(); os.fchmod(output.fileno(),0o644); os.fsync(output.fileno())
            after = os.fstat(source.fileno())
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (
                    after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
                raise ValueError('复制期间源包发生变化，保留现场')
        guard(cached)
        with cached.open('rb') as stream:
            if digest(stream) != plan['package_sha256']: raise ValueError('root原包回读失败')
        for kind in ('server','migration'): _extract(cached, slot/kind, manifest, manifest_raw)
        for relative, folder in MANAGED.items():
            link = slot/'server'/relative
            if not link.parent.exists(): _directory(link.parent)
            link.symlink_to(BASE/'instance/shared'/folder, target_is_directory=True)
            # umask 077 otherwise creates root-only links on Darwin. Never
            # follow the link and chmod the private business directory.
            os.chmod(link, 0o755, follow_symlinks=False)
        for kind in ('server','migration'):
            check_tree(slot/kind,manifest,manifest_raw,server=kind=='server')
            for directory, _, _ in os.walk(slot/kind,topdown=False,followlinks=False):
                sync_directory(Path(directory))
        sync_directory(slot)
        sync_directory(destination)
        verify_child(plan,context,slot)
        validate(raw,approved_sha); preflight(plan)
        if verify_pristine is not None: verify_pristine()
        current, _ = _registry(plan)
        if current != previous: raise ValueError('发布记录发生变化，保留现场')
        public = BASE/'publisher-public.pem'
        if not public.exists(): bootstrap._write(public,plan['public_key'].encode('ascii'),mode=0o644)
        receipt = {'type':'tianming.mac-publication.v1','plan_sha256':approved_sha,
                   'package_sha256':plan['package_sha256'],'signature_verified_by_service_uid':True,
                   'system_daemon_installed':False,'factory_activated':False}
        if verify_pristine is not None:
            receipt.update(pristine_bootstrap=True, data_restored=False)
        bootstrap._write(BASE/('publication-'+plan['attempt_id']+'.verified.json'),bootstrap.canonical(receipt))
        temporary = BASE/('published-'+plan['attempt_id']+'.json')
        content = bootstrap.canonical(registry)
        bootstrap._write(temporary,content,mode=0o644)
        if verify_pristine is not None: verify_pristine()
        os.replace(temporary,BASE/'published.json')
        sync_directory(BASE)
        if read_record(BASE/'published.json') != content: raise ValueError('发布记录回读失败，保留现场')
        return receipt

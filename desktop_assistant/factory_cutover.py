"""Cutover protocol and read-only evidence; this module never activates ERP.

The operational issuer must prove a stopped source and an authenticated final
backup before calling the signing codec. The recipient must verify the local
machine/service identity, restoration and credentials before persisting an
activation. Neither an unsigned request nor a snapshot grants permission to run.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import sqlite3
import subprocess
import sys
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant.cleanup import local_path
from desktop_assistant.migration import facts, files, quote, schema
from desktop_assistant.preflight import inspect
from desktop_assistant.release_request import public_identity
from desktop_assistant.source_paths import relative_source_path
from desktop_assistant.storage import database_info, sha

REQUEST = 'tianming.factory-target.v1'
AUTHORIZATION = 'tianming.factory-cutover.v1'
DOMAIN = b'TIANMING-ERP-FACTORY-CUTOVER\x00v1\x00'
SERVICE_ACCOUNT = '_tianmingerp'
LIMIT = 65536
HASH = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[0-9a-f]{32}')
UTC = timezone.utc
TECHNICAL_FIELDS = {
    'pdf_order_training_samples': {'file_path'},
    'email_intake_settings': {'encrypted_secret'},
}
# Only these deployment settings may differ between final source and target.
# The exact target values must separately match the approved network digest.
NETWORK_FIELDS = frozenset({
    'ERP_HOME_REHEARSAL', 'ERP_PRODUCTION_TRANSPORT', 'ERP_BIND_HOST', 'ERP_PORT',
    'ERP_ALLOWED_ORIGINS', 'ERP_TRUSTED_HOSTS', 'ERP_TRUSTED_PROXY_IPS',
    'ERP_SESSION_COOKIE_SECURE', 'ERP_BROWSER_URL', 'ERP_HEALTH_URL',
    'ERP_LAN_HTTP_ORIGIN', 'ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS',
    'ERP_SHELF_LABEL_ORIGIN',
})
SNAPSHOT_HASHES = {
    'database_sha256', 'schema_sha256', 'facts_sha256', 'files_sha256',
    'preserved_environment_sha256', 'pdf_sources_sha256',
}
SNAPSHOT_COUNTS = {'tables', 'files', 'registered_attachments'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError('切换凭据字段或版本不完整')


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError('切换凭据摘要格式错误')


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('切换凭据安装或申请身份格式错误')


def _time(value):
    if not isinstance(value, str):
        raise ValueError('切换凭据时间格式错误')
    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
        if _stamp(parsed) != value:
            raise ValueError()
        return parsed
    except ValueError:
        raise ValueError('切换凭据时间必须是明确UTC整秒') from None


def _now(value=None):
    value = value if value is not None else datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('切换验证时间必须包含时区')
    return value.astimezone(UTC).replace(microsecond=0)


def _stamp(value):
    return _now(value).strftime('%Y-%m-%dT%H:%M:%SZ')


def _target(target):
    _keys(target, {'host_sha256', 'installation_id', 'service_uid', 'service_gid', 'service_account', 'root'})
    _hash(target['host_sha256'])
    _id(target['installation_id'])
    uid = target['service_uid']
    if type(uid) is not int or not 0 < uid < 2**31:
        raise ValueError('正式ERP不能以root或未知UID运行')
    gid = target['service_gid']
    if type(gid) is not int or not 0 < gid < 2**31:
        raise ValueError('正式ERP不能使用特权或未知GID')
    account = target['service_account']
    if account != SERVICE_ACCOUNT:
        raise ValueError('正式服务账号不是专用_tianmingerp身份')
    root = target['root']
    if (not isinstance(root, str) or not 1 < len(root) <= 4096
            or any(ord(char) < 32 for char in root) or '\\' in root
            or not root.startswith('/') or root.startswith('//')
            or '..' in PurePosixPath(root).parts or str(PurePosixPath(root)) != root):
        raise ValueError('目标安装目录不是明确的Mac绝对路径')


def validate_request(request):
    _keys(request, {'type', 'request_id', 'target', 'package_sha256', 'network_sha256',
                    'publisher_sha256', 'created_at', 'expires_at'})
    if request['type'] != REQUEST:
        raise ValueError('不是正式切换目标申请')
    _id(request['request_id'])
    _target(request['target'])
    for field in ('package_sha256', 'network_sha256', 'publisher_sha256'):
        _hash(request[field])
    duration = _time(request['expires_at']) - _time(request['created_at'])
    if not timedelta(0) < duration <= timedelta(days=7):
        raise ValueError('目标申请有效期必须在七天以内')
    if len(canonical(request)) > LIMIT:
        raise ValueError('目标申请过大')
    return request


def make_request(target, package_sha256, network_sha256, public, *, now=None):
    current = _now(now)
    request = {'type': REQUEST, 'request_id': uuid.uuid4().hex, 'target': target,
               'package_sha256': package_sha256, 'network_sha256': network_sha256,
               'publisher_sha256': public_identity(public)[1], 'created_at': _stamp(current),
               'expires_at': _stamp(current + timedelta(days=7))}
    validate_request(request)
    return json.loads(canonical(request))  # Do not retain a caller's mutable dict.


def _snapshot(snapshot):
    _keys(snapshot, SNAPSHOT_HASHES | SNAPSHOT_COUNTS | {'revision'})
    for field in SNAPSHOT_HASHES:
        _hash(snapshot[field])
    for field in SNAPSHOT_COUNTS:
        if type(snapshot[field]) is not int or snapshot[field] < 0:
            raise ValueError('完整事实计数格式错误')
    if snapshot['tables'] < 1 or snapshot['files'] < 1:
        raise ValueError('完整事实不能为空')
    if not isinstance(snapshot['revision'], str) or not re.fullmatch(r'[A-Za-z0-9_]{1,32}', snapshot['revision']):
        raise ValueError('迁移版本格式错误')


def _evidence(evidence):
    _keys(evidence, {'source_sha256', 'backup_sha256', 'source_package_sha256', 'backup_created_at',
                     'manual_stop', 'process_stopped', 'credential_purposes', 'snapshot'})
    for field in ('source_sha256', 'backup_sha256', 'source_package_sha256'):
        _hash(evidence[field])
    _time(evidence['backup_created_at'])
    if evidence['manual_stop'] is not True or evidence['process_stopped'] is not True:
        raise ValueError('未声明源端已停写并保持手动停止')
    purposes = evidence['credential_purposes']
    if (not isinstance(purposes, list) or any(not isinstance(v, str) for v in purposes)
            or purposes != sorted(set(purposes)) or 'backup' not in purposes
            or not set(purposes) <= {'backup', 'deepseek', 'openai', 'mailbox'}):
        raise ValueError('切换运营凭据用途不完整')
    _snapshot(evidence['snapshot'])


def _body(body):
    _keys(body, {'type', 'request', 'source', 'issued_at', 'install_before'})
    if body['type'] != AUTHORIZATION:
        raise ValueError('不是正式切换签名凭据')
    validate_request(body['request'])
    _evidence(body['source'])
    issued, before = _time(body['issued_at']), _time(body['install_before'])
    if not timedelta(0) < before - issued <= timedelta(hours=8):
        raise ValueError('切换激活时窗必须在八小时以内')
    if not (_time(body['request']['created_at']) <= _time(body['source']['backup_created_at'])
            <= issued < before <= _time(body['request']['expires_at'])):
        raise ValueError('最终备份必须晚于目标申请，且签发/激活时间有效')


def sign_authorization(request, stopped_backup_evidence, key, *, now=None):
    """Codec only: the source-side issuer must independently obtain the evidence.

    This never loads/exports a key or changes source state. A future operational
    entry must authenticate the backup and stopped process under its lock first.
    """
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('切换必须使用既有Ed25519发布身份')
    validate_request(request)
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    if public_identity(public)[1] != request['publisher_sha256']:
        raise ValueError('切换签名不是既有发布身份')
    current = _now(now)
    before = min(current + timedelta(hours=8), _time(request['expires_at']))
    body = json.loads(canonical({'type': AUTHORIZATION, 'request': request, 'source': stopped_backup_evidence,
                                 'issued_at': _stamp(current), 'install_before': _stamp(before)}))
    _body(body)
    signature = key.sign(DOMAIN + canonical(body))
    raw = canonical({'body': body, 'signature': base64.b64encode(signature).decode('ascii')})
    if len(raw) > LIMIT:
        raise ValueError('切换凭据过大')
    return raw


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('切换JSON含重复字段')
        result[name] = value
    return result


def verify_authorization(raw, public, expected_request):
    """Verify exact binding; installation age is checked by require_install_window.

    Runtime validation must not expire a server after a successful activation.
    Callers also need the protected local activation record and live identity.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMIT:
        raise ValueError('切换凭据大小或编码错误')
    envelope = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非标准JSON数值')))
    _keys(envelope, {'body', 'signature'})
    _body(envelope['body'])
    signature = envelope['signature']
    if not isinstance(signature, str):
        raise ValueError('切换签名格式错误')
    try:
        signature = base64.b64decode(signature, validate=True)
    except ValueError:
        raise ValueError('切换签名编码错误') from None
    if len(signature) != 64:
        raise ValueError('切换签名长度错误')
    key, fingerprint = public_identity(public)
    body = envelope['body']
    key.verify(signature, DOMAIN + canonical(body))
    validate_request(expected_request)
    if (body['request']['publisher_sha256'] != fingerprint
            or canonical(body['request']) != canonical(expected_request)):
        raise ValueError('切换凭据与本机、账号、目录、程序或网络申请不一致')
    return body


def require_install_window(verified_body, *, now=None):
    _body(verified_body)
    current = _now(now)
    if not _time(verified_body['issued_at']) <= current < _time(verified_body['install_before']):
        raise ValueError('切换凭据尚未生效或已过激活时窗')


def verify_installation_binding(raw, public, expected_request, restored_backup_sha256,
                                restored_snapshot, *, now=None):
    """Check the signed data binding; does not install/activate or verify secrets."""
    body = verify_authorization(raw, public, expected_request)
    require_install_window(body, now=now)
    _hash(restored_backup_sha256)
    if body['source']['backup_sha256'] != restored_backup_sha256:
        raise ValueError('目标不是本次最终停写备份的恢复')
    require_matching_snapshots(body['source']['snapshot'], restored_snapshot)
    return body


def local_target(root, installation_id):
    """Read identity for a provisioned non-admin Mac service account; no writes."""
    if (sys.platform != 'darwin' or platform.machine() != 'arm64'
            or os.geteuid() == 0 or os.getuid() != os.geteuid()):
        raise ValueError('目标必须是Mac arm64普通服务账号')
    import grp
    import pwd
    account = pwd.getpwuid(os.geteuid())
    admin = grp.getgrnam('admin')
    if (admin.gr_gid in os.getgroups() or os.getegid() == admin.gr_gid
            or account.pw_name in admin.gr_mem):
        raise ValueError('正式服务必须使用专用低权限账号，不能使用管理员')
    if (account.pw_name != SERVICE_ACCOUNT or account.pw_gid != os.getegid()
            or grp.getgrgid(account.pw_gid).gr_name != SERVICE_ACCOUNT):
        raise ValueError('正式服务须使用专用账号及其专用组')
    root = local_path(root)
    if (not root.is_dir() or root.stat().st_uid != account.pw_uid
            or root.stat().st_mode & 0o077
            or root.stat().st_dev != Path('/System/Volumes/Data').stat().st_dev):
        raise ValueError('目标目录须在本机数据盘、归服务账号独占所有')
    for parent in root.parents:
        info = parent.stat()
        if info.st_uid not in (0, account.pw_uid) or info.st_mode & 0o022:
            raise ValueError('目标祖先目录可被其他账号改写，不能绑定服务身份')
    result = subprocess.run(['/usr/sbin/ioreg', '-a', '-r', '-d', '1', '-c', 'IOPlatformExpertDevice'],
                            capture_output=True, check=True, timeout=10)
    if len(result.stdout) > 1024 * 1024:
        raise ValueError('本机硬件身份结果异常')
    tree = plistlib.loads(result.stdout)
    if not isinstance(tree, list) or len(tree) != 1:
        raise ValueError('不能确认本机硬件身份')
    raw_uuid = tree[0].get('IOPlatformUUID')
    if not isinstance(raw_uuid, str):
        raise ValueError('本机缺少硬件身份')
    hardware = uuid.UUID(raw_uuid).hex
    target = {'host_sha256': hashlib.sha256(DOMAIN + hardware.encode('ascii')).hexdigest(),
              'installation_id': installation_id, 'service_uid': account.pw_uid, 'service_gid': account.pw_gid,
              'service_account': account.pw_name, 'root': str(root)}
    _target(target)
    return target


def _offline(database):
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('最终事实要求已停写静态库，不能忽略WAL或热日志')


def capture_snapshot(shared, *, recorded_root=None):
    """Digest every static fact; callers hold the owning maintenance lock.

    Known portable transformations are normalized, not silently ignored: PDF
    references are rooted/hash-checked, all other mailbox columns are retained,
    and non-network environment settings are compared. Plain credentials still
    require an independent authenticated recovery comparison, never a digest.
    """
    shared = local_path(shared)
    database = shared / 'data/carton_erp.sqlite3'
    if not shared.is_dir() or not database.is_file():
        raise ValueError('缺少完整静态共享数据')
    # Reject links before opening the DB or any referenced environment file.
    before_files = files(shared)
    _offline(database)
    before = sha(database)
    info = database_info(database)
    checks = inspect(database, shared, managed=True, recorded_root=recorded_root or shared)
    if any(checks['counts'][name] for name in ('missing', 'external', 'hash_mismatch')):
        raise ValueError('完整事实中的附件缺失或不一致')
    columns, pdf = {}, []
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            columns[table] = [row[1] for row in db.execute('PRAGMA table_info(' + quote(table) + ')')
                              if row[1] not in TECHNICAL_FIELDS.get(table, set())]
        if 'pdf_order_training_samples' in tables:
            for identity, raw, expected in db.execute('SELECT id,file_path,file_sha256 FROM pdf_order_training_samples ORDER BY id'):
                if raw is None or raw == '':
                    relative = raw  # Preserve null versus empty; never merge rows.
                else:
                    relative = relative_source_path(raw, recorded_root or shared).as_posix()
                    actual = local_path(shared / relative)
                    if not actual.is_relative_to(shared) or not actual.is_file() or sha(actual) != expected:
                        raise ValueError('PDF源文件与记录不一致')
                pdf.append([identity, relative, expected])
    environment = json.loads((shared / 'environment.json').read_bytes(), object_pairs_hook=_unique)
    if not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()):
        raise ValueError('正式环境配置格式错误')
    stable_files = {name: value for name, value in before_files.items() if name != 'environment.json'}
    result = {'database_sha256': before, 'revision': info['revision'], 'tables': len(tables),
              'schema_sha256': schema(database, legacy=True), 'facts_sha256': digest(facts(database, columns)),
              'files_sha256': digest(stable_files), 'files': len(before_files),
              'preserved_environment_sha256': digest({k: v for k, v in environment.items() if k not in NETWORK_FIELDS}),
              'pdf_sources_sha256': digest(pdf), 'registered_attachments': checks['counts']['ok']}
    _offline(database)
    if sha(database) != before or files(shared) != before_files:
        raise ValueError('完整事实读取期间发生变化，保持停止')
    _snapshot(result)
    return result


def require_matching_snapshots(source, restored):
    _snapshot(source)
    _snapshot(restored)
    # SQLite page bytes change after the two explicitly checked transformations.
    # Raw source bytes must separately match the authenticated backup manifest.
    if {k: v for k, v in source.items() if k != 'database_sha256'} != {
            k: v for k, v in restored.items() if k != 'database_sha256'}:
        raise ValueError('最终恢复事实、配置或附件与停写来源不一致')

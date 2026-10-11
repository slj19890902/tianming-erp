"""Public target request contract shared with the stdlib administrator pack."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePosixPath
import plistlib
import re
import subprocess
import uuid

REQUEST = 'tianming.factory-target.v1'
DOMAIN = b'TIANMING-ERP-FACTORY-CUTOVER\x00v1\x00'
SERVICE_ACCOUNT = '_tianmingerp'
LIMIT = 65536
HASH = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[0-9a-f]{32}')
UTC = timezone.utc

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

def make_request(target, package_sha256, network_sha256, publisher_sha256, *, now=None):
    current = _now(now)
    request = {'type': REQUEST, 'request_id': uuid.uuid4().hex, 'target': target,
        'package_sha256': package_sha256, 'network_sha256': network_sha256,
        'publisher_sha256': publisher_sha256, 'created_at': _stamp(current),
        'expires_at': _stamp(current + timedelta(days=7))}
    validate_request(request)
    return json.loads(canonical(request))


def host_hash():
    result = subprocess.run(['/usr/sbin/ioreg', '-a', '-r', '-d', '1', '-c', 'IOPlatformExpertDevice'],
                            capture_output=True, check=True, timeout=10)
    if len(result.stdout) > 1024 * 1024: raise ValueError('本机硬件身份结果异常')
    tree = plistlib.loads(result.stdout)
    if not isinstance(tree, list) or len(tree) != 1: raise ValueError('不能确认本机硬件身份')
    raw = tree[0].get('IOPlatformUUID')
    if not isinstance(raw, str): raise ValueError('本机缺少硬件身份')
    return hashlib.sha256(DOMAIN + uuid.UUID(raw).hex.encode('ascii')).hexdigest()

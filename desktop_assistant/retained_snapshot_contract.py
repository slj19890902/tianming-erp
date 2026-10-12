"""Pure public shapes for explicitly retaining an already restored snapshot.

No filesystem, clock, randomness, key access or signing is performed here. An
old backup remains old: a new request binds its original restoration provenance,
a separate complete preview comparison and a new stopped source audit. These
validators check declarations and their bindings, never prove their truth or
grant permission to activate. Signature, complete private evidence, credentials,
source isolation and actual installed program checks belong to the consumers.

The v1 contracts are deliberately unchanged. Public preparation contains only
identity, hashes, counts and timestamps; typed login rows and exact environment
values belong to separately retained private evidence. It never includes a
request or authorization digest, so the preparation/request binding is acyclic.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re

# This existing validator is itself stdlib-only and performs no IO.
from desktop_assistant import factory_network

REQUEST = 'tianming.factory-target-retained.v2'
PREPARATION = 'tianming.factory-retained-preparation.v2'
SOURCE = 'tianming.retained-source-audit.v2'
STOP_PROOF = 'tianming.retained-source-stop-proof.v2'
AUTHORIZATION = 'tianming.factory-cutover-retained.v2'
TYPE = 'tianming.mac-factory-activation-retained.v2'
DOMAIN = b'TIANMING-ERP-FACTORY-CUTOVER\x00retained-v2\x00'
POLICY = 'preview-login-only.v1'
LIMIT = 65536
MAX_DEPTH = 32
MAX_INTEGER = 2**63 - 1
UTC = timezone.utc
ATTESTATION = {'factory_cutover_authorized': True, 'source_isolation_confirmed': True}
SNAPSHOT_HASHES = frozenset({'database_sha256', 'schema_sha256', 'facts_sha256',
    'files_sha256', 'preserved_environment_sha256', 'pdf_sources_sha256'})
SNAPSHOT_COUNTS = frozenset({'tables', 'files', 'registered_attachments'})
RETAINED_FIELDS = frozenset({'backup_sha256', 'backup_created_at', 'credentials_sha256',
    'restore_package_sha256', 'restore_receipt_sha256'})
RECORD_FIELDS = frozenset({'type', 'request', 'network', 'authorization',
    'authorization_sha256', 'context_sha256', 'registry_sha256', 'activated_at',
    'target_snapshot', 'operator_attestation'})


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('保留快照证明不是规范JSON') from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode(raw):
    """Decode bounded UTF-8 JSON, rejecting duplicate keys at every depth."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMIT:
        raise ValueError('保留快照证明长度无效')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('保留快照证明含重复字段')
            result[key] = value
        return result

    def invalid(_):
        raise ValueError('保留快照证明含非有限数值')

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=invalid)
        if not isinstance(value, dict):
            raise ValueError('保留快照证明不是对象')
        _bounded(value)
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('保留快照证明JSON无效') from None


def _bounded(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise ValueError('保留快照证明嵌套过深')
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    if len(canonical(value)) > LIMIT:
        raise ValueError('保留快照证明过大')


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError('保留快照证明字段或版本不完整')


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('保留快照摘要格式错误')


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('保留快照身份格式错误')


def _integer(value, *, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('保留快照计数或身份超界')


def _current(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('核验时间必须显式提供并包含时区')
    return value.astimezone(UTC)


def _stamp(value):
    return _current(value).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        raise ValueError('保留快照时间必须是UTC整秒')
    try:
        result = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
        if _stamp(result) != value:
            raise ValueError()
        return result
    except ValueError:
        raise ValueError('保留快照时间无效') from None


def _target(value):
    _keys(value, {'host_sha256', 'installation_id', 'service_uid', 'service_gid',
                  'service_account', 'root'})
    _hash(value['host_sha256']); _id(value['installation_id'])
    for name in ('service_uid', 'service_gid'):
        _integer(value[name], minimum=1, maximum=2**31 - 1)
    if value['service_account'] != '_tianmingerp':
        raise ValueError('正式服务必须使用既有专用身份')
    root = value['root']
    if (not isinstance(root, str) or not 1 < len(root) <= 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in root) or '\\' in root
            or not root.startswith('/') or root.startswith('//')
            or '..' in PurePosixPath(root).parts or str(PurePosixPath(root)) != root):
        raise ValueError('保留实例路径不是规范Mac绝对路径')


def _snapshot(value):
    _keys(value, SNAPSHOT_HASHES | SNAPSHOT_COUNTS | {'revision'})
    for name in SNAPSHOT_HASHES:
        _hash(value[name])
    for name in SNAPSHOT_COUNTS:
        _integer(value[name], minimum=1 if name in {'tables', 'files'} else 0)
    if not isinstance(value['revision'], str) or not re.fullmatch(r'[A-Za-z0-9_]{1,32}', value['revision']):
        raise ValueError('保留快照数据库版本无效')


def _same_except(left, right, excluded):
    if {k: v for k, v in left.items() if k not in excluded} != {
            k: v for k, v in right.items() if k not in excluded}:
        raise ValueError('保留快照的完整事实绑定不一致')


def _restore(value, fields):
    _keys(value, fields)
    for name in fields - {'backup_created_at'}:
        _hash(value[name])  # External credentials are mandatory, never null.
    _time(value['backup_created_at'])


def _presence(value):
    _keys(value, {'present', 'value'})
    if type(value['present']) is not bool:
        raise ValueError('序列存在标记必须明确')
    if value['present']:
        _integer(value['value'])
    elif value['value'] is not None:
        raise ValueError('缺失序列不能携带数值')


def _preview(value):
    _keys(value, {'operation_logs', 'sqlite_sequence', 'environment_chain'})
    logs = value['operation_logs']
    _keys(logs, {'columns_sha256', 'baseline_count', 'target_count', 'baseline_sha256',
        'target_sha256', 'appended_count', 'appended_sha256', 'first_appended_id', 'last_appended_id'})
    for name in ('columns_sha256', 'baseline_sha256', 'target_sha256', 'appended_sha256'):
        _hash(logs[name])
    for name in ('baseline_count', 'target_count', 'appended_count'):
        _integer(logs[name])
    if logs['target_count'] != logs['baseline_count'] + logs['appended_count']:
        raise ValueError('登录追加计数不一致')
    count = logs['appended_count']
    if count == 0:
        if (logs['first_appended_id'] is not None or logs['last_appended_id'] is not None
                or logs['appended_sha256'] != digest([])
                or logs['baseline_sha256'] != logs['target_sha256']):
            raise ValueError('空登录追加证明不一致')
    else:
        _integer(logs['first_appended_id'], minimum=1)
        _integer(logs['last_appended_id'], minimum=1)
        if not count <= logs['last_appended_id'] - logs['first_appended_id'] + 1:
            raise ValueError('登录追加ID区间不一致')
    sequence = value['sqlite_sequence']
    _keys(sequence, {'table_present', 'baseline_sha256', 'target_sha256', 'before', 'after'})
    if type(sequence['table_present']) is not bool:
        raise ValueError('序列表存在标记必须明确')
    _hash(sequence['baseline_sha256']); _hash(sequence['target_sha256'])
    before, after = sequence['before'], sequence['after']
    _presence(before); _presence(after)
    if not sequence['table_present'] and (before['present'] or after['present']):
        raise ValueError('不存在的序列表不能含登录序列')
    if count == 0 and before != after:
        raise ValueError('没有登录追加不能改变序列')
    if before['present'] and not after['present']:
        raise ValueError('不能删除原登录序列')
    if after['present'] and count and after['value'] != max(
            before['value'] if before['present'] else 0, logs['last_appended_id']):
        raise ValueError('登录序列变化不能由追加ID解释')
    if before == after and sequence['baseline_sha256'] != sequence['target_sha256']:
        raise ValueError('未改变的登录序列不能隐藏其它序列变化')
    chain = value['environment_chain']
    if not isinstance(chain, list) or len(chain) != 2:
        raise ValueError('环境批准摘要须保留home和lan两阶段')
    for step, kind, maximum in zip(chain, ('home', 'lan'), (15, 4)):
        _keys(step, {'kind', 'receipt_sha256', 'script_sha256', 'before_sha256',
                    'after_sha256', 'changes_sha256', 'changed_fields_count'})
        if step['kind'] != kind:
            raise ValueError('环境批准顺序错误')
        for name in ('receipt_sha256', 'script_sha256', 'before_sha256', 'after_sha256', 'changes_sha256'):
            _hash(step[name])
        _integer(step['changed_fields_count'], maximum=maximum)
        if step['changed_fields_count'] == 0 and (step['before_sha256'] != step['after_sha256']
                                                 or step['changes_sha256'] != digest([])):
            raise ValueError('空环境变化摘要不一致')
    if chain[0]['after_sha256'] != chain[1]['before_sha256']:
        raise ValueError('环境批准摘要链断裂')


def validate_preparation(value):
    _keys(value, {'type', 'policy', 'prepared_at', 'target', 'package_sha256', 'restore',
                  'baseline_snapshot', 'target_snapshot', 'preview_delta'})
    if value['type'] != PREPARATION or value['policy'] != POLICY:
        raise ValueError('不是明确保留已恢复副本的准备证明')
    _target(value['target']); _hash(value['package_sha256'])
    _restore(value['restore'], RETAINED_FIELDS | {'context_sha256', 'native_binding_sha256'})
    if _time(value['restore']['backup_created_at']) > _time(value['prepared_at']):
        raise ValueError('原备份不能晚于本次准备')
    baseline, target = value['baseline_snapshot'], value['target_snapshot']
    _snapshot(baseline); _snapshot(target); _preview(value['preview_delta'])
    # Keep both original snapshots. The independent audit explains these three
    # possible differences instead of modifying snapshots or dropping tables.
    _same_except(baseline, target, {'database_sha256', 'facts_sha256', 'preserved_environment_sha256'})
    delta = value['preview_delta']
    if not delta['operation_logs']['appended_count'] and baseline['facts_sha256'] != target['facts_sha256']:
        raise ValueError('没有登录追加不能改变完整事实摘要')
    if not any(step['changed_fields_count'] for step in delta['environment_chain']):
        if baseline['preserved_environment_sha256'] != target['preserved_environment_sha256']:
            raise ValueError('没有环境批准变化不能改变环境摘要')
    _bounded(value)
    return value


def validate_preview_delta(value):
    """Public summary shape only; the private comparison is still required."""
    _preview(value)
    _bounded(value)
    return value


def validate_request(value, *, preparation=None):
    _keys(value, {'type', 'request_id', 'target', 'package_sha256', 'network_sha256',
                  'publisher_sha256', 'created_at', 'expires_at', 'retained_snapshot'})
    if value['type'] != REQUEST:
        raise ValueError('不是保留快照的v2目标申请')
    _id(value['request_id']); _target(value['target'])
    for name in ('package_sha256', 'network_sha256', 'publisher_sha256'):
        _hash(value[name])
    created, expires = _time(value['created_at']), _time(value['expires_at'])
    if not timedelta(0) < expires - created <= timedelta(days=7):
        raise ValueError('目标申请有效期必须在七天以内')
    retained = value['retained_snapshot']
    _restore(retained, RETAINED_FIELDS | {'preparation_sha256'})
    if _time(retained['backup_created_at']) > created:
        raise ValueError('保留申请不能把未来备份当作已恢复副本')
    if preparation is not None:
        validate_preparation(preparation)
        if (retained['preparation_sha256'] != digest(preparation)
                or value['package_sha256'] != preparation['package_sha256']
                or value['target'] != preparation['target']
                or any(retained[k] != preparation['restore'][k] for k in RETAINED_FIELDS)
                or _time(preparation['prepared_at']) > created):
            raise ValueError('目标申请与原恢复或准备证明不一致')
    _bounded(value)
    return value


def make_request(target, package_sha256, network_sha256, publisher_sha256, retained_snapshot,
                 *, request_id, now, preparation=None):
    """Construct a detached request; the caller supplies identity and time."""
    current = _current(now).replace(microsecond=0)
    try:
        expires = current + timedelta(days=7)
    except OverflowError:
        raise ValueError('目标申请时间超界') from None
    result = {'type': REQUEST, 'request_id': request_id, 'target': target,
        'package_sha256': package_sha256, 'network_sha256': network_sha256,
        'publisher_sha256': publisher_sha256, 'retained_snapshot': retained_snapshot,
        'created_at': _stamp(current), 'expires_at': _stamp(expires)}
    validate_request(result, preparation=preparation)
    return decode(canonical(result))


def validate_source(value):
    _keys(value, {'type', 'source_sha256', 'backup_sha256', 'backup_created_at',
        'source_package_sha256', 'credentials_sha256', 'credential_purposes', 'snapshot',
        'current_snapshot', 'audit_started_at', 'verified_at', 'stop_proof'})
    if value['type'] != SOURCE:
        raise ValueError('不是保留快照的停写源端核验')
    for name in ('source_sha256', 'backup_sha256', 'source_package_sha256', 'credentials_sha256'):
        _hash(value[name])
    if not _time(value['backup_created_at']) <= _time(value['audit_started_at']) <= _time(value['verified_at']):
        raise ValueError('原备份与本次停写核验时间顺序错误')
    purposes = value['credential_purposes']
    if (not isinstance(purposes, list) or any(not isinstance(x, str) for x in purposes)
            or purposes != sorted(set(purposes)) or 'backup' not in purposes
            or not set(purposes) <= {'backup', 'deepseek', 'openai', 'mailbox'}):
        raise ValueError('原凭据用途不完整或重复')
    _snapshot(value['snapshot']); _snapshot(value['current_snapshot'])
    _same_except(value['snapshot'], value['current_snapshot'], {'database_sha256'})
    proof = value['stop_proof']
    flags = {'manual_stop', 'process_stopped', 'maintenance_lock_held',
             'database_read_lease_held', 'journals_absent'}
    hashes = {'state_sha256', 'database_sha256', 'files_sha256', 'environment_sha256',
              'credential_config_sha256', 'backup_receipt_sha256'}
    _keys(proof, hashes | flags | {'type', 'process_record_sha256'})
    if proof['type'] != STOP_PROOF or any(proof[k] is not True for k in flags):
        raise ValueError('源端停止、持锁或无日志声明不完整')
    for name in hashes:
        _hash(proof[name])
    if proof['process_record_sha256'] is not None:
        _hash(proof['process_record_sha256'])
    if any(proof[k] != value['current_snapshot'][k] for k in ('database_sha256', 'files_sha256')):
        raise ValueError('源端停写证明与本次完整快照不一致')
    _bounded(value)
    return value


def validate_body(value):
    _keys(value, {'type', 'request', 'preparation', 'source', 'issued_at', 'install_before'})
    if value['type'] != AUTHORIZATION:
        raise ValueError('不是保留快照的v2切换授权')
    preparation = validate_preparation(value['preparation'])
    request = validate_request(value['request'], preparation=preparation)
    source = validate_source(value['source'])
    retained = request['retained_snapshot']
    if (any(source[k] != retained[k] for k in ('backup_sha256', 'backup_created_at', 'credentials_sha256'))
            or source['snapshot'] != preparation['baseline_snapshot']
            or source['stop_proof']['environment_sha256'] != preparation['preview_delta']['environment_chain'][0]['before_sha256']):
        raise ValueError('源端核验与原完整备份、凭据或目标准备不一致')
    issued, before = _time(value['issued_at']), _time(value['install_before'])
    if not timedelta(0) < before - issued <= timedelta(hours=8):
        raise ValueError('首次激活窗口必须在八小时以内')
    if not (_time(request['created_at']) <= _time(source['audit_started_at'])
            <= _time(source['verified_at']) <= issued < before <= _time(request['expires_at'])):
        raise ValueError('必须先申请、再停写重验签发，且在有效期内首次激活')
    _bounded(value)
    return value


def authorization_body(raw, request):
    """Envelope shape/binding only; this does NOT verify its signature."""
    envelope = decode(raw)
    _keys(envelope, {'body', 'signature'})
    body = validate_body(envelope['body'])
    signature = envelope['signature']
    if not isinstance(signature, str):
        raise ValueError('保留快照签名编码无效')
    try:
        binary = base64.b64decode(signature, validate=True)
    except (ValueError, UnicodeError):
        raise ValueError('保留快照签名编码无效') from None
    if len(binary) != 64 or base64.b64encode(binary).decode('ascii') != signature:
        raise ValueError('保留快照签名长度或规范编码无效')
    if canonical(body['request']) != canonical(validate_request(request)):
        raise ValueError('授权与本次完整目标申请不一致')
    return body


def require_install_window(body, *, now):
    """First activation only; never expire an already validated runtime record."""
    validate_body(body)
    if not _time(body['issued_at']) <= _current(now) < _time(body['install_before']):
        raise ValueError('首次激活尚未生效或已过八小时窗口')


def validate_record(value):
    """Historical protected-record shape, not signature or root-owner proof."""
    _keys(value, RECORD_FIELDS)
    if value['type'] != TYPE:
        raise ValueError('不是保留快照的v2激活记录')
    request = validate_request(value['request'])
    if digest(factory_network.validate(value['network'])) != request['network_sha256']:
        raise ValueError('激活网络与明确申请不一致')
    for name in ('authorization_sha256', 'context_sha256', 'registry_sha256'):
        _hash(value[name])
    if not isinstance(value['authorization'], str):
        raise ValueError('授权原字节编码无效')
    try:
        raw = value['authorization'].encode('utf-8')
    except UnicodeError:
        raise ValueError('授权原字节编码无效') from None
    if hashlib.sha256(raw).hexdigest() != value['authorization_sha256']:
        raise ValueError('授权原字节摘要不符')
    body = authorization_body(raw, request)
    at = _time(value['activated_at'])
    if not _time(body['issued_at']) <= at < _time(body['install_before']):
        raise ValueError('记录的首次激活时点不在授权窗口')
    if value['context_sha256'] != body['preparation']['restore']['context_sha256']:
        raise ValueError('激活服务上下文与原恢复不一致')
    _snapshot(value['target_snapshot'])
    if value['target_snapshot'] != body['preparation']['target_snapshot']:
        raise ValueError('激活目标不是本次准备的精确完整快照')
    _keys(value['operator_attestation'], ATTESTATION)
    if any(value['operator_attestation'][k] is not True for k in ATTESTATION):
        raise ValueError('尚未明确授权本次切换和确认旧端隔离')
    _bounded(value)
    return body

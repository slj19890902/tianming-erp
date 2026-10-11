"""Public activation shape only; signature/content authentication is separate.

The administrator pack is stdlib-only. These validators do not verify Ed25519
or authorize execution. The pinned service worker authenticates the final data;
a future runtime consumer must also verify the source signature and root record.
"""
import base64
from datetime import timedelta
import json
import re

from desktop_assistant.factory_target_contract import (
    canonical, digest, validate_request, _keys, _hash, _time, _stamp, _now)
from desktop_assistant import factory_network as network

AUTHORIZATION = 'tianming.factory-cutover.v1'
TYPE = 'tianming.mac-factory-activation.v1'
NAME = 'factory-activation.json'
ENTRY = 'desktop_assistant/factory_activation_entry.py'
MODULE = 'desktop_assistant/mac_factory_activation.py'
FIELDS = {'type', 'request', 'network', 'authorization', 'authorization_sha256',
          'context_sha256', 'registry_sha256', 'activated_at', 'target_snapshot', 'operator_attestation'}
ATTESTATION = {'factory_cutover_authorized': True, 'source_isolation_confirmed': True}
LIMIT = 65536

SNAPSHOT_HASHES = {
    'database_sha256', 'schema_sha256', 'facts_sha256', 'files_sha256',
    'preserved_environment_sha256', 'pdf_sources_sha256',
}
SNAPSHOT_COUNTS = {'tables', 'files', 'registered_attachments'}


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


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMIT: raise ValueError('激活记录长度无效')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('激活记录含重复字段')
            result[key] = value
        return result
    def invalid(_): raise ValueError('激活记录含非有限数字')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(value, dict): raise ValueError('激活记录不是对象')
    return value


def authorization_body(raw, request):
    """Shape/binding only. Never use this instead of verify_authorization."""
    value = decode(raw)
    _keys(value, {'body', 'signature'}); _body(value['body'])
    signature = value['signature']
    if not isinstance(signature, str) or len(base64.b64decode(signature, validate=True)) != 64:
        raise ValueError('激活授权签名形状无效')
    if canonical(value['body']['request']) != canonical(validate_request(request)):
        raise ValueError('激活授权与本机申请不符')
    return value['body']


def validate_record(value):
    _keys(value, FIELDS)
    if value['type'] != TYPE: raise ValueError('不是受保护的工厂激活记录')
    request = validate_request(value['request'])
    proposed = network.validate(value['network'])
    if digest(proposed) != request['network_sha256']: raise ValueError('激活网络与申请不符')
    for key in ('context_sha256', 'registry_sha256', 'authorization_sha256'): _hash(value[key])
    if not isinstance(value['authorization'], str): raise ValueError('激活授权原字节编码无效')
    raw = value['authorization'].encode('utf-8')
    import hashlib
    if hashlib.sha256(raw).hexdigest() != value['authorization_sha256']: raise ValueError('激活授权原字节摘要不符')
    body = authorization_body(raw, request)
    at = _time(value['activated_at'])
    if not _time(body['issued_at']) <= at < _time(body['install_before']):
        raise ValueError('激活发布时点不在源端授权窗口')
    _snapshot(value['target_snapshot'])
    if {k:v for k,v in value['target_snapshot'].items() if k!='database_sha256'} != {
            k:v for k,v in body['source']['snapshot'].items() if k!='database_sha256'}:
        raise ValueError('激活目标事实与来源不符')
    if canonical(value['operator_attestation']) != canonical(ATTESTATION):
        raise ValueError('尚未明确授权本次切换和确认旧端隔离')
    if len(canonical(value)) > LIMIT: raise ValueError('激活记录过大')
    return body


def require_protocol(manifest):
    runtime = manifest.get('runtime_capabilities', {})
    readers = manifest.get('reader_capabilities', {})
    if (any(type(runtime.get(k)) is not int or runtime[k] != 1 for k in
            ('factory_activation_v1', 'factory_audit_v1', 'factory_execution_v1'))
            or type(readers.get('factory_cutover_v1')) is not int or readers['factory_cutover_v1'] != 1
            or not {ENTRY, MODULE, 'desktop_assistant/factory_target_audit.py',
                    'desktop_assistant/factory_activation_contract.py'} <= manifest.get('files', {}).keys()):
        raise ValueError('程序尚未完整实现受保护激活、核验及正式执行协议')


def reply(value, request, context):
    expected = {'type':'tianming.factory-activation-check.v1', 'challenge':request['challenge'],
        'identity':{'uid':context['uid'],'euid':context['uid'],'gid':context['gid'],'egid':context['gid'],
                    'kernel_groups':[context['gid']]}, 'manual_stop':True, 'process_stopped':True,
        'keychain_canary_verified':True,'system_ready':True,'factory_activated':False}
    _keys(value, set(expected)|{'record','mac_backup_verified_at'})
    if any(canonical(value[k]) != canonical(v) for k,v in expected.items()):
        raise ValueError('激活核验子进程回执与本次请求不符')
    validate_record(value['record'])
    record = value['record']
    if (record['request']['request_id'] != request['request_id']
            or record['request']['package_sha256'] != request['package_sha256']
            or any(record[k] != request[k] for k in ('authorization_sha256','context_sha256','registry_sha256'))):
        raise ValueError('激活核验回执绑定不符')
    # This is a prior verified backup receipt, not a new network/storage probe.
    _time(value['mac_backup_verified_at'])
    return value

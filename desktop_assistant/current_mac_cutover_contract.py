"""Public v3 claims for preserving the current Mac business, with no IO/clock.

This is neither an audit nor authorization to execute. The future consumers
must authenticate the actual stopped Mac, full cold backup/NAS readback, raw
shared tree and actual credentials, original restore receipt/native binding,
root approvals and the stopped Windows source under their original leases.
Windows approves the exact request; its business need not equal the Mac's.

The five package roles are independent, but may share a hash. Backup ``release``
is not the stream transport, new execution, monitor or historical native-restore
package. Nullable schema authorities are explicit claims of absence, never a
waiver of the consumer's authenticated metadata/schema/provenance checks.
Credential hashes name encrypted envelopes only, never plaintext passwords.
Existing v1/v2 formats and their business comparisons are not reinterpreted.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from desktop_assistant import factory_network


REQUEST = 'tianming.factory-current-mac.target.v3'
PREPARATION = 'tianming.factory-current-mac.preparation.v3'
SOURCE_STOP = 'tianming.factory-current-mac.source-stop.v3'
AUTHORIZATION = 'tianming.factory-current-mac.authorization.v3'
TYPE = 'tianming.factory-current-mac.activation.v3'
DOMAIN = b'TIANMING-ERP-FACTORY-CUTOVER\x00current-mac-v3\x00'
LIMIT = 65536
MAX_DEPTH = 24
MAX_INTEGER = 2**63 - 1
UTC = timezone.utc
ROOT = '/Library/TianmingERP/instance'
ATTESTATION = frozenset({'factory_cutover_authorized', 'source_isolation_confirmed'})
TARGET_FIELDS = frozenset({'host_sha256', 'installation_id', 'service_uid', 'service_gid',
    'service_account', 'root'})
REQUEST_FIELDS = frozenset({'type', 'request_id', 'created_at', 'expires_at', 'target',
    'publisher_sha256', 'network_sha256', 'execution_package_sha256', 'monitor_package_sha256',
    'preparation_sha256'})
PREPARATION_FIELDS = frozenset({'type', 'prepared_at', 'target', 'publisher_sha256',
    'context_sha256', 'registry_sha256', 'state_sha256', 'execution_package_sha256',
    'monitor_package_sha256', 'system_control_sha256', 'system_plist_sha256', 'restore',
    'backup', 'target_snapshot', 'shared_manifest_sha256', 'credential_purposes'})
RESTORE_FIELDS = frozenset({'backup_sha256', 'backup_created_at', 'credentials_sha256',
    'restore_package_sha256', 'restore_receipt_sha256', 'native_binding_sha256',
    'restored_release_sha256', 'restored_schema_authority_sha256'})
BACKUP_FIELDS = frozenset({'sha256', 'size', 'created_at', 'verified_at', 'receipt_sha256',
    'transport_control_sha256', 'transport_contract_sha256', 'transport_package_sha256',
    'release_sha256', 'schema_authority_sha256', 'credentials_sha256'})
SNAPSHOT_HASHES = frozenset({'database_sha256', 'schema_sha256', 'facts_sha256', 'files_sha256',
    'preserved_environment_sha256', 'pdf_sources_sha256'})
SNAPSHOT_COUNTS = frozenset({'tables', 'files', 'registered_attachments'})
STOP_FLAGS = frozenset({'manual_stop', 'process_stopped', 'maintenance_lock_held',
    'database_read_lease_held', 'journals_absent'})
SOURCE_STOP_FIELDS = STOP_FLAGS | frozenset({'type', 'source_sha256', 'source_package_sha256',
    'request_sha256', 'state_sha256', 'database_sha256', 'files_sha256', 'environment_sha256',
    'credential_config_sha256', 'process_record_sha256', 'audit_started_at', 'verified_at'})
BODY_FIELDS = frozenset({'type', 'request', 'preparation', 'source_stop', 'issued_at', 'install_before'})
RECORD_FIELDS = frozenset({'type', 'request', 'network', 'authorization', 'authorization_sha256',
    'context_sha256', 'registry_sha256', 'activated_at', 'target_snapshot', 'operator_attestation'})
ERROR = '当前Mac业务接管公开合同无效；未核验底层事实或授权执行'


def _require(ok):
    if not ok:
        raise ValueError(ERROR)


def _tree(value, depth=0):
    _require(depth <= MAX_DEPTH)
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        value.encode('utf-8')
    elif type(value) is int:
        _require(-MAX_INTEGER-1 <= value <= MAX_INTEGER)
    elif type(value) is dict:
        _require(all(type(key) is str for key in value))
        for key, child in value.items():
            _tree(key, depth+1)
            _tree(child, depth+1)
    elif type(value) is list:
        for child in value:
            _tree(child, depth+1)
    else:
        # No public field uses floats, including finite floats or overflowed exponents.
        raise ValueError(ERROR)


def canonical(value):
    """Bounded canonical UTF-8 bytes, also for hashing detached public objects."""
    try:
        _tree(value)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
        _require(len(raw) <= LIMIT)
        return raw
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError(ERROR) from None


def digest(value):
    """SHA256 of canonical JSON; raw-file hashes are a separate consumer check."""
    return hashlib.sha256(canonical(value)).hexdigest()


def decode(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= LIMIT)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    def invalid(_):
        raise ValueError(ERROR)
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_float=invalid, parse_constant=invalid)
        _require(type(value) is dict)
        canonical(value)
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError(ERROR) from None


def _keys(value, fields):
    _require(type(value) is dict and set(value) == fields)
    canonical(value)


def _hash(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)


def _id(value):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{32}', value) is not None)


def _integer(value, minimum=0):
    _require(type(value) is int and minimum <= value <= MAX_INTEGER)


def _current(value):
    _require(type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None)
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError(ERROR) from None


def _stamp(value):
    return _current(value).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _time(value):
    _require(type(value) is str and re.fullmatch(
        '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value) is not None)
    try:
        result = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
        _require(_stamp(result) == value)
        return result
    except ValueError:
        raise ValueError(ERROR) from None


def normalize_timestamp(authenticated_iso):
    """Normalize already authenticated metadata, never authenticate or edit it.

    The consumer must first verify the original backup/receipt bytes and its
    exact ISO timestamp (including fractional ordering). Only the public UTC
    representation is floored to seconds; no original time/file is rewritten.
    Naive/local times are deliberately unsupported, not guessed as UTC.
    """
    _require(type(authenticated_iso) is str and re.fullmatch(
        '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
        r'(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])',
        authenticated_iso) is not None)
    try:
        return _stamp(datetime.fromisoformat(authenticated_iso.replace('Z', '+00:00')))
    except (ValueError, OverflowError):
        raise ValueError(ERROR) from None


def _target(value):
    _keys(value, TARGET_FIELDS)
    _hash(value['host_sha256']); _id(value['installation_id'])
    _require(type(value['service_uid']) is int and value['service_uid'] == 499
        and type(value['service_gid']) is int and value['service_gid'] == 499
        and value['service_account'] == '_tianmingerp' and value['root'] == ROOT)


def _snapshot(value):
    _keys(value, SNAPSHOT_HASHES | SNAPSHOT_COUNTS | {'revision'})
    for key in SNAPSHOT_HASHES:
        _hash(value[key])
    for key in SNAPSHOT_COUNTS:
        _integer(value[key], 0 if key == 'registered_attachments' else 1)
    _require(type(value['revision']) is str and re.fullmatch('[A-Za-z0-9_]{1,32}', value['revision']))


def validate_preparation(value):
    """Validate claims, not the independent Mac/backup/credential equality proof."""
    _keys(value, PREPARATION_FIELDS)
    _require(value['type'] == PREPARATION)
    _target(value['target']); _snapshot(value['target_snapshot'])
    for key in PREPARATION_FIELDS:
        if key.endswith('_sha256'):
            _hash(value[key])
    restore, backup = value['restore'], value['backup']
    _keys(restore, RESTORE_FIELDS); _keys(backup, BACKUP_FIELDS)
    for key in RESTORE_FIELDS - {'backup_created_at'}:
        if key != 'restored_schema_authority_sha256' or restore[key] is not None:
            _hash(restore[key])
    for key in BACKUP_FIELDS - {'size', 'created_at', 'verified_at'}:
        if key != 'schema_authority_sha256' or backup[key] is not None:
            _hash(backup[key])
    _integer(backup['size'], 1)
    _require(_time(restore['backup_created_at']) <= _time(backup['created_at'])
        <= _time(backup['verified_at']) <= _time(value['prepared_at']))
    purposes = value['credential_purposes']
    _require(type(purposes) is list and all(type(item) is str for item in purposes))
    _require(purposes == sorted(set(purposes)) and 'backup' in purposes
        and set(purposes) <= {'backup', 'deepseek', 'openai', 'mailbox'})
    return value


def make_preparation(*, target, publisher_sha256, context_sha256, registry_sha256, state_sha256,
        execution_package_sha256, monitor_package_sha256, system_control_sha256, system_plist_sha256,
        restore, backup, target_snapshot, shared_manifest_sha256, credential_purposes, now):
    value = dict(type=PREPARATION, prepared_at=_stamp(now), target=target,
        publisher_sha256=publisher_sha256, context_sha256=context_sha256, registry_sha256=registry_sha256,
        state_sha256=state_sha256, execution_package_sha256=execution_package_sha256,
        monitor_package_sha256=monitor_package_sha256, system_control_sha256=system_control_sha256,
        system_plist_sha256=system_plist_sha256, restore=restore, backup=backup,
        target_snapshot=target_snapshot, shared_manifest_sha256=shared_manifest_sha256,
        credential_purposes=credential_purposes)
    value = decode(canonical(value))
    return validate_preparation(value)


def validate_request(value, *, preparation=None):
    """Without preparation this checks shape only; body validation always binds it."""
    _keys(value, REQUEST_FIELDS)
    _require(value['type'] == REQUEST)
    _id(value['request_id']); _target(value['target'])
    for key in REQUEST_FIELDS:
        if key.endswith('_sha256'):
            _hash(value[key])
    created, expires = _time(value['created_at']), _time(value['expires_at'])
    _require(timedelta(0) < expires-created <= timedelta(days=7))
    if preparation is not None:
        validate_preparation(preparation)
        _require(value['preparation_sha256'] == digest(preparation)
            and all(value[key] == preparation[key] for key in
                ('target', 'publisher_sha256', 'execution_package_sha256', 'monitor_package_sha256'))
            and _time(preparation['prepared_at']) <= created)
    return value


def make_request(preparation, network_sha256, publisher_sha256, *, request_id, now):
    prep = validate_preparation(decode(canonical(preparation)))
    current = _current(now).replace(microsecond=0)
    try:
        expires = current + timedelta(days=7)
    except OverflowError:
        raise ValueError(ERROR) from None
    value = dict(type=REQUEST, request_id=request_id, created_at=_stamp(current), expires_at=_stamp(expires),
        target=prep['target'], publisher_sha256=publisher_sha256, network_sha256=network_sha256,
        execution_package_sha256=prep['execution_package_sha256'],
        monitor_package_sha256=prep['monitor_package_sha256'], preparation_sha256=digest(prep))
    return validate_request(value, preparation=prep)


def validate_source_stop(value, *, request=None):
    """Windows stop/request claims only; no equality with historical or Mac data."""
    _keys(value, SOURCE_STOP_FIELDS)
    _require(value['type'] == SOURCE_STOP and all(value[key] is True for key in STOP_FLAGS))
    for key in SOURCE_STOP_FIELDS:
        if key.endswith('_sha256') and (key != 'process_record_sha256' or value[key] is not None):
            _hash(value[key])
    _require(_time(value['audit_started_at']) <= _time(value['verified_at']))
    if request is not None:
        validate_request(request)
        _require(value['request_sha256'] == digest(request)
            and _time(request['created_at']) <= _time(value['audit_started_at'])
            <= _time(value['verified_at']) < _time(request['expires_at']))
    return value


def validate_body(value):
    _keys(value, BODY_FIELDS)
    _require(value['type'] == AUTHORIZATION)
    request = validate_request(value['request'], preparation=validate_preparation(value['preparation']))
    source = validate_source_stop(value['source_stop'], request=request)
    issued, before = _time(value['issued_at']), _time(value['install_before'])
    _require(timedelta(0) < before-issued <= timedelta(hours=8)
        and _time(source['verified_at']) <= issued < before <= _time(request['expires_at']))
    return value


def make_body(request, preparation, source_stop, *, now):
    request = validate_request(decode(canonical(request)))
    current = _current(now).replace(microsecond=0)
    try:
        before = min(current+timedelta(hours=8), _time(request['expires_at']))
    except OverflowError:
        raise ValueError(ERROR) from None
    value = decode(canonical(dict(type=AUTHORIZATION, request=request, preparation=preparation,
        source_stop=source_stop, issued_at=_stamp(current), install_before=_stamp(before))))
    return validate_body(value)


def authorization_body(raw, expected_request):
    """Strict envelope/binding only: this function does NOT verify a signature."""
    envelope = decode(raw)
    _keys(envelope, {'body', 'signature'})
    body = validate_body(envelope['body'])
    signature = envelope['signature']
    _require(type(signature) is str)
    try:
        binary = base64.b64decode(signature, validate=True)
    except (ValueError, UnicodeError):
        raise ValueError(ERROR) from None
    _require(len(binary) == 64 and base64.b64encode(binary).decode('ascii') == signature
        and canonical(body['request']) == canonical(validate_request(expected_request)))
    return body


def require_install_window(body, *, now):
    """First activation only; persistent verification must not call this later."""
    validate_body(body)
    _require(_time(body['issued_at']) <= _current(now) < _time(body['install_before']))


def validate_record(value):
    """Historical shape, not signature verification, root ownership or live audit."""
    _keys(value, RECORD_FIELDS)
    _require(value['type'] == TYPE)
    request = validate_request(value['request'])
    _require(digest(factory_network.validate(value['network'])) == request['network_sha256'])
    for key in ('authorization_sha256', 'context_sha256', 'registry_sha256'):
        _hash(value[key])
    _require(type(value['authorization']) is str)
    raw = value['authorization'].encode('utf-8')
    _require(hashlib.sha256(raw).hexdigest() == value['authorization_sha256'])
    body = authorization_body(raw, request)
    prep = body['preparation']
    _require(all(value[key] == prep[key] for key in
        ('context_sha256', 'registry_sha256', 'target_snapshot')))
    _snapshot(value['target_snapshot'])
    require_install_window(body, now=_time(value['activated_at']))
    _keys(value['operator_attestation'], ATTESTATION)
    _require(all(value['operator_attestation'][key] is True for key in ATTESTATION))
    return body


def make_record(request, network, authorization, *, activated_at, operator_attestation):
    """Construct only public claims; explicit attestation is not physical proof."""
    body = authorization_body(authorization, request)
    require_install_window(body, now=activated_at)
    prep = body['preparation']
    value = decode(canonical(dict(type=TYPE, request=request, network=network,
        authorization=authorization.decode('utf-8'), authorization_sha256=hashlib.sha256(authorization).hexdigest(),
        context_sha256=prep['context_sha256'], registry_sha256=prep['registry_sha256'],
        activated_at=_stamp(activated_at), target_snapshot=prep['target_snapshot'],
        operator_attestation=operator_attestation)))
    validate_record(value)
    return value

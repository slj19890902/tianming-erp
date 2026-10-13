"""Closed public protocol for a future current-Mac cutover worker.

No IO, clock, signing, locking, backup or service operation occurs here.
Consumers must authenticate raw root files separately: an object digest never
substitutes for a raw-file digest. Reply booleans are claims, not evidence.
Execution, monitor, transport and historical restore roles may differ.
"""
import hashlib
import math
import re

from desktop_assistant import current_mac_cutover_contract as cutover

BASE = '/Library/TianmingERP'
PLAN = 'tianming.current-mac-worker-plan.v1'
MOUNTED_PLAN = 'tianming.current-mac-mounted-worker-plan.v1'
REQUEST = 'tianming.current-mac-worker-request.v1'
REPLY = 'tianming.current-mac-worker-reply.v1'
PHASES = frozenset({'update', 'prepare', 'activate'})
ALL_PHASES = PHASES | {'checkpoint'}
LIMIT = cutover.LIMIT
MAX_TIME = 253402300799
MAX_AGE = 7 * 86400
MAX_FD = 2**31 - 1
COMMON = frozenset({'type', 'attempt_id', 'phase', 'created_at', 'expires_at',
    'uid', 'gid', 'code_manifest_sha256', 'context_sha256', 'publisher_sha256',
    'registry_sha256', 'execution_package_sha256', 'monitor_package_sha256',
    'transport_package_sha256', 'restore_attempt_id', 'restore_receipt_sha256',
    'system_control_sha256', 'system_plist_sha256', 'transport_control_sha256',
    'transport_contract_sha256', 'network_file_sha256', 'network_sha256'})
CHECKPOINT_COMMON = COMMON - {'transport_control_sha256','transport_contract_sha256'}
PHASE_FIELDS = {
    'checkpoint': frozenset({'state_before_sha256'}),
    'update': frozenset({'current_package_sha256', 'state_before_sha256'}),
    'prepare': frozenset({'update_result_file_sha256', 'state_stopped_sha256',
        'backup_sha256', 'backup_receipt_sha256', 'request_id'}),
    'activate': frozenset({'preparation_file_sha256', 'preparation_sha256',
        'request_file_sha256', 'request_sha256', 'authorization_file_sha256',
        'state_stopped_sha256', 'backup_sha256', 'backup_receipt_sha256',
        'request_id', 'operator_attestation'}),
}
REQUEST_FIELDS = frozenset({'type', 'attempt_id', 'phase', 'plan_sha256',
    'challenge', 'lock_fd'})
REPLY_FIELDS = frozenset({'type', 'attempt_id', 'phase', 'plan_sha256',
    'challenge', 'identity', 'manual_stop', 'process_stopped', 'home_rehearsal',
    'borrowed_lock', 'result'})
UPDATE_RESULT_FIELDS = frozenset({'state_before_sha256', 'state_after_sha256',
    'previous_package_sha256', 'current_package_sha256', 'backup_sha256',
    'backup_receipt_sha256', 'nas_independent_readback'})
ERROR = 'Current Mac worker public protocol invalid; execution not authorized'


def require(ok):
    if not ok:
        raise ValueError(ERROR)


def sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def token(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{32}', value) is not None


def canonical(value):
    return cutover.canonical(value)


def decode(raw):
    return cutover.decode(raw)


def raw_digest(raw):
    require(type(raw) is bytes and 0 < len(raw) <= LIMIT)
    return hashlib.sha256(raw).hexdigest()


def exact(value, fields):
    require(type(value) is dict and set(value) == fields)
    canonical(value)


def plan_path(attempt_id, phase):
    require(token(attempt_id) and type(phase) is str and phase in ALL_PHASES)
    return BASE + '/current-mac-cutover-' + attempt_id + '/' + phase + '-plan.json'


def validate_plan(value, *, now=None):
    require(type(value) is dict and type(value.get('phase')) is str
            and value['phase'] in ALL_PHASES)
    mounted = value.get('type') == MOUNTED_PLAN
    common = CHECKPOINT_COMMON if value['phase'] == 'checkpoint' else COMMON
    fields = PHASE_FIELDS[value['phase']]
    if mounted and value['phase'] == 'prepare':
        fields = (fields - {'update_result_file_sha256'}) | {'local_update_receipt_sha256'}
    if mounted and value['phase'] == 'activate': fields = fields | {'local_update_receipt_sha256'}
    require(value.get('type') in (PLAN,MOUNTED_PLAN)
        and (value['phase'] != 'checkpoint' or mounted)
        and (not mounted or value['phase'] != 'update'))
    exact(value, common | fields)
    require(value['type'] in (PLAN,MOUNTED_PLAN) and token(value['attempt_id'])
            and token(value['restore_attempt_id']))
    require(type(value['uid']) is int and value['uid'] == 499
            and type(value['gid']) is int and value['gid'] == 499)
    require(all(sha(value[key]) for key in value if key.endswith('_sha256')))
    require(all(type(value[key]) is int and 0 <= value[key] <= MAX_TIME
                for key in ('created_at', 'expires_at')))
    require(0 < value['expires_at'] - value['created_at'] <= MAX_AGE)
    if value['phase'] == 'update':
        require(value['current_package_sha256'] != value['execution_package_sha256'])
    elif value['phase'] != 'checkpoint':
        require(token(value['request_id']))
    if value['phase'] == 'activate':
        exact(value['operator_attestation'], cutover.ATTESTATION)
        require(all(value['operator_attestation'][key] is True for key in cutover.ATTESTATION))
    if now is not None:
        require(type(now) in (int, float) and math.isfinite(now)
                and value['created_at'] <= now < value['expires_at'])
    return value


def validate_request(value, plan, plan_sha):
    validate_plan(plan)
    exact(value, REQUEST_FIELDS)
    require(sha(plan_sha) and value['type'] == REQUEST
        and value['attempt_id'] == plan['attempt_id'] and value['phase'] == plan['phase']
        and value['plan_sha256'] == plan_sha and token(value['challenge']))
    if value['phase'] == 'activate':
        require(type(value['lock_fd']) is int and 3 <= value['lock_fd'] <= MAX_FD)
    else:
        # update owns Manager.lock; prepare owns its own original audit lease.
        require(value['lock_fd'] is None)
    return value


def validate_identity(value, plan):
    exact(value, {'uid', 'euid', 'gid', 'egid', 'kernel_groups'})
    require(all(type(value[key]) is int for key in ('uid', 'euid', 'gid', 'egid')))
    require(type(value['kernel_groups']) is list and len(value['kernel_groups']) == 1
            and type(value['kernel_groups'][0]) is int)
    require(value == {'uid':plan['uid'], 'euid':plan['uid'], 'gid':plan['gid'],
                     'egid':plan['gid'], 'kernel_groups':[plan['gid']]})


def _preparation(preparation, plan):
    cutover.validate_preparation(preparation)
    require(all(preparation[key] == plan[key] for key in (
        'context_sha256', 'publisher_sha256', 'registry_sha256',
        'execution_package_sha256', 'monitor_package_sha256',
        'system_control_sha256', 'system_plist_sha256')))
    require(preparation['state_sha256'] == plan['state_stopped_sha256']
        and preparation['restore']['restore_receipt_sha256'] == plan['restore_receipt_sha256'])
    backup = preparation['backup']
    require(backup['sha256'] == plan['backup_sha256']
        and backup['receipt_sha256'] == plan['backup_receipt_sha256']
        and all(backup[key] == plan[key] for key in ('transport_package_sha256',
            'transport_control_sha256', 'transport_contract_sha256')))


def _target_request(request, preparation, plan):
    cutover.validate_request(request, preparation=preparation)
    require(request['request_id'] == plan['request_id']
            and request['network_sha256'] == plan['network_sha256'])


def validate_reply(value, request, plan, plan_sha):
    validate_request(request, plan, plan_sha)
    exact(value, REPLY_FIELDS)
    validate_identity(value['identity'], plan)
    require(all(value[key] == request[key] for key in
                ('attempt_id', 'phase', 'plan_sha256', 'challenge')))
    require(value['type'] == REPLY and value['manual_stop'] is True
        and value['process_stopped'] is True and value['home_rehearsal'] is True
        and type(value['borrowed_lock']) is bool
        and value['borrowed_lock'] == (request['phase'] == 'activate'))
    result = value['result']
    if request['phase'] == 'update':
        exact(result, UPDATE_RESULT_FIELDS)
        require(all(sha(result[key]) for key in result if key.endswith('_sha256')))
        require(result['state_before_sha256'] == plan['state_before_sha256']
            and result['previous_package_sha256'] == plan['current_package_sha256']
            and result['current_package_sha256'] == plan['execution_package_sha256']
            and result['state_after_sha256'] != result['state_before_sha256']
            and result['nas_independent_readback'] is True)
    elif request['phase'] == 'checkpoint':
        from desktop_assistant.mounted_nas_proof import validate_checkpoint
        exact(result, {'checkpoint'})
        checkpoint = validate_checkpoint(result['checkpoint'])
        require(checkpoint['state_sha256'] == plan['state_before_sha256']
            and checkpoint['release_sha256'] == plan['execution_package_sha256'])
    elif request['phase'] == 'prepare':
        exact(result, {'preparation', 'request'})
        _preparation(result['preparation'], plan)
        _target_request(result['request'], result['preparation'], plan)
    else:
        exact(result, {'record'})
        record = result['record']
        body = cutover.validate_record(record)  # Shape only, never signature verification.
        _preparation(body['preparation'], plan)
        _target_request(body['request'], body['preparation'], plan)
        require(cutover.digest(body['preparation']) == plan['preparation_sha256']
            and cutover.digest(body['request']) == plan['request_sha256']
            and record['authorization_sha256'] == plan['authorization_file_sha256']
            and record['operator_attestation'] == plan['operator_attestation'])
    return value


def validate_update_link(update_reply, update_request, update_plan, update_plan_sha,
                         prepare_plan, *, update_result_raw):
    """Bind the full authenticated prior reply, not just user-supplied digests."""
    validate_reply(update_reply, update_request, update_plan, update_plan_sha)
    validate_plan(prepare_plan)
    require(update_plan['phase'] == 'update' and prepare_plan['phase'] == 'prepare'
        and raw_digest(update_result_raw) == prepare_plan['update_result_file_sha256']
        and decode(update_result_raw) == update_reply)
    require(all(update_plan[key] == prepare_plan[key] for key in
        COMMON - {'type', 'phase', 'created_at', 'expires_at'}))
    require(update_plan['created_at'] <= prepare_plan['created_at'])
    result = update_reply['result']
    require(result['state_after_sha256'] == prepare_plan['state_stopped_sha256']
        and result['backup_sha256'] == prepare_plan['backup_sha256']
        and result['backup_receipt_sha256'] == prepare_plan['backup_receipt_sha256'])
    return prepare_plan

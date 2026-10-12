"""Pure public shapes for a separately approved system supervisor upgrade.

No filesystem, clock, process, credential, signing or service operation occurs
here. The caller must authenticate the fixed root plan, published code, actual
identity, inherited lock and observed state before reporting these declarations.
``process_stopped`` means the managed ERP only: neither phase proves the old
supervisor exited, launchd health, reboot/logout acceptance or factory cutover.

The initial control raw digest authorizes the future administrator transaction.
Workers subsequently require the exact disabled old/new control bytes derived
below, not the initial enabled record. Original v1 service contracts stay intact.
"""
import hashlib
import json
import math
import re

BASE = '/Library/TianmingERP'
PLAN = 'tianming.system-upgrade-plan.v1'
REQUEST = 'tianming.system-upgrade-request.v1'
REPLY = 'tianming.system-upgrade-worker.v1'
PHASES = frozenset({'stop', 'verify'})
LIMIT = 65536
MAX_DEPTH = 16
MAX_INTEGER = 2**63 - 1
MAX_TIMESTAMP = 253402300799  # Last whole UTC second representable in year 9999.
MAX_AGE = 7 * 86400
MAX_FD = 2**31 - 1
PLAN_FIELDS = frozenset({'type', 'attempt_id', 'created_at', 'expires_at', 'uid', 'gid',
    'code_manifest_sha256', 'old_package_sha256', 'new_package_sha256', 'current_package_sha256',
    'context_sha256', 'publisher_sha256', 'registry_sha256', 'old_control_sha256',
    'old_plist_sha256', 'new_plist_sha256', 'state_before_sha256', 'state_stopped_sha256'})
REQUEST_FIELDS = frozenset({'type', 'attempt_id', 'plan_sha256', 'phase', 'challenge', 'lock_fd'})
REPLY_FIELDS = frozenset({'type', 'attempt_id', 'plan_sha256', 'phase', 'challenge',
    'new_package_sha256', 'current_package_sha256', 'control_package_sha256', 'plist_sha256',
    'identity', 'state_before_sha256', 'state_after_sha256', 'manual_stop', 'process_stopped',
    'root_gate_enabled', 'home_rehearsal', 'keychain_canary_verified', 'borrowed_lock'})
IDENTITY_FIELDS = frozenset({'uid', 'euid', 'gid', 'egid', 'kernel_groups'})
BOOLEAN_FIELDS = frozenset({'manual_stop', 'process_stopped', 'root_gate_enabled',
    'home_rehearsal', 'keychain_canary_verified', 'borrowed_lock'})
ERROR = '系统监护升级公开合同不符；保留现场，不授权执行或重试'


def require(ok):
    if not ok:
        raise ValueError(ERROR)


def sha(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def token(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{32}', value) is not None


def _tree(value, depth=0):
    require(depth <= MAX_DEPTH)
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ValueError(ERROR) from None
        return
    if type(value) is int:
        require(-MAX_INTEGER - 1 <= value <= MAX_INTEGER)
        return
    if type(value) is dict:
        require(all(type(key) is str for key in value))
        for key, item in value.items():
            _tree(key, depth + 1)
            _tree(item, depth + 1)
        return
    if type(value) is list:
        for item in value:
            _tree(item, depth + 1)
        return
    # The public protocol has no floating-point fields, even finite ones.
    raise ValueError(ERROR)


def canonical(value):
    """Match bootstrap canonical bytes, including ASCII escaping of text."""
    try:
        _tree(value)
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, UnicodeError, RecursionError):
        raise ValueError(ERROR) from None


def digest(raw):
    require(type(raw) is bytes)
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    """Bounded strict UTF-8 object; duplicate keys and all floats are refused."""
    require(type(raw) is bytes and 0 < len(raw) <= LIMIT)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result
    def invalid(_):
        raise ValueError(ERROR)
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                            parse_constant=invalid, parse_float=invalid)
        _tree(result)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise ValueError(ERROR) from None
    require(type(result) is dict)
    return result


def _exact(value, fields):
    require(type(value) is dict and set(value) == fields)
    require(len(canonical(value)) <= LIMIT)


def plan_path(attempt_id):
    """The only future root plan location; this function does not access it."""
    require(token(attempt_id))
    return BASE + '/system-upgrade-' + attempt_id + '/plan.json'


def validate_plan(value, *, now=None):
    """Validate shape; an execution consumer must also supply its current time."""
    _exact(value, PLAN_FIELDS)
    require(value['type'] == PLAN and token(value['attempt_id']))
    require(all(sha(value[key]) for key in PLAN_FIELDS if key.endswith('_sha256')))
    require(value['old_package_sha256'] != value['new_package_sha256']
            and value['old_plist_sha256'] != value['new_plist_sha256'])
    require(all(type(value[key]) is int and 400 <= value[key] < 500 for key in ('uid', 'gid')))
    require(all(type(value[key]) is int and 0 <= value[key] <= MAX_TIMESTAMP
                for key in ('created_at', 'expires_at')))
    require(0 < value['expires_at'] - value['created_at'] <= MAX_AGE)
    if now is not None:
        validate_window(value, now)
    return value


def validate_window(plan, now):
    validate_plan(plan)
    require(type(now) in (int, float) and 0 <= now <= MAX_TIMESTAMP and math.isfinite(now))
    require(plan['created_at'] <= now < plan['expires_at'])
    return plan


def validate_request(value, plan, plan_sha):
    """plan_sha is the separately authenticated raw root plan digest."""
    validate_plan(plan)
    require(sha(plan_sha))
    _exact(value, REQUEST_FIELDS)
    require(value['type'] == REQUEST and value['attempt_id'] == plan['attempt_id']
            and value['plan_sha256'] == plan_sha and type(value['phase']) is str
            and value['phase'] in PHASES and token(value['challenge']))
    if value['phase'] == 'stop':
        require(value['lock_fd'] is None)
    else:
        require(type(value['lock_fd']) is int and 3 <= value['lock_fd'] <= MAX_FD)
    return value


def disabled_control(plan, phase):
    validate_plan(plan)
    require(type(phase) is str and phase in PHASES)
    prefix = 'old' if phase == 'stop' else 'new'
    return {'type': 'tianming.system-service.v1', 'package_sha256': plan[prefix + '_package_sha256'],
            'context_sha256': plan['context_sha256'], 'publisher_sha256': plan['publisher_sha256'],
            'plist_sha256': plan[prefix + '_plist_sha256'], 'enabled': False}


def disabled_control_bytes(plan, phase):
    return canonical(disabled_control(plan, phase))


def validate_identity(value, plan):
    validate_plan(plan)
    _exact(value, IDENTITY_FIELDS)
    require(all(type(value[key]) is int for key in ('uid', 'euid', 'gid', 'egid')))
    require(type(value['kernel_groups']) is list and len(value['kernel_groups']) == 1
            and type(value['kernel_groups'][0]) is int)
    expected = {'uid': plan['uid'], 'euid': plan['uid'], 'gid': plan['gid'], 'egid': plan['gid'],
                'kernel_groups': [plan['gid']]}
    require(value == expected)
    return value


def validate_reply(value, request, plan, plan_sha):
    validate_request(request, plan, plan_sha)
    _exact(value, REPLY_FIELDS)
    validate_identity(value['identity'], plan)
    require(all(type(value[key]) is bool for key in BOOLEAN_FIELDS))
    verify = request['phase'] == 'verify'
    control = disabled_control(plan, request['phase'])
    expected = {'type': REPLY, 'attempt_id': plan['attempt_id'], 'plan_sha256': plan_sha,
        'phase': request['phase'], 'challenge': request['challenge'],
        'new_package_sha256': plan['new_package_sha256'], 'current_package_sha256': plan['current_package_sha256'],
        'control_package_sha256': control['package_sha256'], 'plist_sha256': control['plist_sha256'],
        'identity': value['identity'], 'state_before_sha256': plan['state_stopped_sha256'] if verify else plan['state_before_sha256'],
        'state_after_sha256': plan['state_stopped_sha256'], 'manual_stop': True, 'process_stopped': True,
        'root_gate_enabled': False, 'home_rehearsal': True, 'keychain_canary_verified': verify, 'borrowed_lock': verify}
    require(value == expected)
    return value

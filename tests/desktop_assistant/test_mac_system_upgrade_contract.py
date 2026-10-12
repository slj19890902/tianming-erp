"""Portable public protocol tests; no root/service/keychain or NAS operations."""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from desktop_assistant import mac_system_upgrade_contract as contract


def valid_plan():
    return dict(type=contract.PLAN, attempt_id='a' * 32, created_at=1700000000,
        expires_at=1700000000 + contract.MAX_AGE, uid=499, gid=499,
        code_manifest_sha256='6' * 64, old_package_sha256='a' * 64,
        new_package_sha256='b' * 64, current_package_sha256='c' * 64,
        context_sha256='d' * 64, publisher_sha256='e' * 64, registry_sha256='f' * 64,
        old_control_sha256='1' * 64, old_plist_sha256='2' * 64, new_plist_sha256='3' * 64,
        state_before_sha256='4' * 64, state_stopped_sha256='5' * 64)


def valid_request(plan, phase='stop'):
    return dict(type=contract.REQUEST, attempt_id=plan['attempt_id'],
        plan_sha256=contract.digest(contract.canonical(plan)), phase=phase, challenge='7' * 32,
        lock_fd=None if phase == 'stop' else 7)


def valid_reply(plan, request):
    verify = request['phase'] == 'verify'
    control = contract.disabled_control(plan, request['phase'])
    return dict(type=contract.REPLY, attempt_id=plan['attempt_id'], plan_sha256=request['plan_sha256'],
        phase=request['phase'], challenge=request['challenge'], new_package_sha256=plan['new_package_sha256'],
        current_package_sha256=plan['current_package_sha256'], control_package_sha256=control['package_sha256'],
        plist_sha256=control['plist_sha256'], identity=dict(uid=plan['uid'], euid=plan['uid'],
            gid=plan['gid'], egid=plan['gid'], kernel_groups=[plan['gid']]),
        state_before_sha256=plan['state_stopped_sha256'] if verify else plan['state_before_sha256'],
        state_after_sha256=plan['state_stopped_sha256'], manual_stop=True, process_stopped=True,
        root_gate_enabled=False, home_rehearsal=True, keychain_canary_verified=verify, borrowed_lock=verify)


@pytest.mark.parametrize('phase', ['stop', 'verify'])
def test_valid_bound_plan_request_and_observed_reply(phase):
    plan = valid_plan()
    request = valid_request(plan, phase)
    reply = valid_reply(plan, request)
    before = deepcopy((plan, request, reply))
    assert contract.validate_plan(plan, now=plan['created_at']) is plan
    assert contract.validate_request(request, plan, request['plan_sha256']) is request
    assert contract.validate_reply(reply, request, plan, request['plan_sha256']) is reply
    assert (plan, request, reply) == before
    for value in (plan, request, reply):
        assert contract.decode(contract.canonical(value)) == value
    assert reply['keychain_canary_verified'] is (phase == 'verify')
    assert 'supervisor_stopped' not in reply and 'health_verified' not in reply


@pytest.mark.parametrize('field', sorted(contract.PLAN_FIELDS))
def test_every_plan_field_is_required(field):
    plan = valid_plan()
    plan.pop(field)
    with pytest.raises(ValueError):
        contract.validate_plan(plan)


@pytest.mark.parametrize('kind', ['plan', 'request', 'reply'])
def test_unknown_top_level_fields_are_rejected(kind):
    plan = valid_plan()
    request = valid_request(plan)
    reply = valid_reply(plan, request)
    value = {'plan': plan, 'request': request, 'reply': reply}[kind]
    value['path'] = '/arbitrary/unapproved'
    with pytest.raises(ValueError):
        if kind == 'plan':
            contract.validate_plan(plan)
        elif kind == 'request':
            contract.validate_request(request, plan, request['plan_sha256'])
        else:
            contract.validate_reply(reply, request, plan, request['plan_sha256'])


@pytest.mark.parametrize('field', sorted(k for k in contract.PLAN_FIELDS if k.endswith('_sha256')))
@pytest.mark.parametrize('bad', [None, '', 'A' * 64, 'a' * 63, 'g' * 64, True])
def test_plan_hashes_are_exact_lowercase_sha256(field, bad):
    plan = valid_plan()
    plan[field] = bad
    with pytest.raises(ValueError):
        contract.validate_plan(plan)


@pytest.mark.parametrize('field,bad', [('type', 'tianming.system-service-plan.v1'),
    ('attempt_id', '../arbitrary'), ('attempt_id', 'a' * 31), ('attempt_id', 'A' * 32),
    ('attempt_id', True), ('uid', True), ('uid', '499'), ('uid', 499.0), ('uid', 0),
    ('uid', 399), ('uid', 500), ('gid', False), ('gid', 0), ('gid', 500),
    ('created_at', True), ('expires_at', False), ('created_at', 1700000000.0),
    ('created_at', '1700000000'), ('created_at', -1), ('expires_at', contract.MAX_TIMESTAMP + 1)])
def test_plan_identity_attempt_and_time_types_are_strict(field, bad):
    plan = valid_plan()
    plan[field] = bad
    with pytest.raises(ValueError):
        contract.validate_plan(plan)


def test_three_package_roles_are_distinct_without_forcing_erp_to_match_supervisor():
    plan = valid_plan()
    assert len({plan[k] for k in ('old_package_sha256', 'new_package_sha256', 'current_package_sha256')}) == 3
    contract.validate_plan(plan)
    for package in ('old_package_sha256', 'new_package_sha256'):
        changed = {**plan, 'current_package_sha256': plan[package]}
        contract.validate_plan(changed)  # ERP may coincide; it need not coincide.
    with pytest.raises(ValueError):
        contract.validate_plan({**plan, 'new_package_sha256': plan['old_package_sha256']})
    with pytest.raises(ValueError):
        contract.validate_plan({**plan, 'new_plist_sha256': plan['old_plist_sha256']})


def test_already_stopped_canonical_state_can_have_identical_hashes():
    plan = valid_plan()
    plan['state_stopped_sha256'] = plan['state_before_sha256']
    for phase in contract.PHASES:
        request = valid_request(plan, phase)
        contract.validate_reply(valid_reply(plan, request), request, plan, request['plan_sha256'])


@pytest.mark.parametrize('span', [0, -1, contract.MAX_AGE + 1])
def test_plan_window_must_be_positive_and_at_most_seven_days(span):
    plan = valid_plan()
    plan['expires_at'] = plan['created_at'] + span
    with pytest.raises(ValueError):
        contract.validate_plan(plan)


def test_window_creation_is_inclusive_expiry_exclusive_and_no_clock_is_implicitly_read():
    plan = valid_plan()
    for now in (plan['created_at'], plan['created_at'] + 0.5, plan['expires_at'] - 0.5):
        assert contract.validate_window(plan, now) is plan
        assert contract.validate_plan(plan, now=now) is plan
    for now in (plan['created_at'] - 0.5, plan['expires_at']):
        with pytest.raises(ValueError):
            contract.validate_window(plan, now)
    assert contract.validate_plan(plan) is plan  # Shape-only is not execution approval.


@pytest.mark.parametrize('now', [True, False, '1700000000', None, float('nan'), float('inf'),
                                -float('inf'), -1, contract.MAX_TIMESTAMP + 1, 10**100])
def test_invalid_explicit_clock_is_rejected(now):
    with pytest.raises(ValueError):
        contract.validate_window(valid_plan(), now)


@pytest.mark.parametrize('edge', [0, contract.MAX_TIMESTAMP - 1])
def test_epoch_boundaries_with_one_second_plan(edge):
    plan = {**valid_plan(), 'created_at': edge, 'expires_at': edge + 1}
    contract.validate_plan(plan, now=edge)


@pytest.mark.parametrize('field', sorted(contract.REQUEST_FIELDS))
def test_every_request_field_is_required(field):
    plan = valid_plan()
    request = valid_request(plan)
    digest = request['plan_sha256']
    request.pop(field)
    with pytest.raises(ValueError):
        contract.validate_request(request, plan, digest)


@pytest.mark.parametrize('field,bad', [('type', 'tianming.system-service-worker.v1'),
    ('attempt_id', 'b' * 32), ('plan_sha256', '8' * 64), ('phase', 'verify-old'),
    ('phase', 'install'), ('phase', ['stop']), ('challenge', ''), ('challenge', 'A' * 32),
    ('challenge', '7' * 33), ('challenge', 7), ('lock_fd', 7)])
def test_stop_request_binding_unknown_phase_and_borrowed_lock_are_rejected(field, bad):
    plan = valid_plan()
    request = valid_request(plan)
    digest = request['plan_sha256']
    request[field] = bad
    with pytest.raises(ValueError):
        contract.validate_request(request, plan, digest)


@pytest.mark.parametrize('bad', [None, False, True, 0, 1, 2, -1, 7.0, '7', contract.MAX_FD + 1])
def test_verify_requires_explicit_valid_inherited_fd(bad):
    plan = valid_plan()
    request = valid_request(plan, 'verify')
    request['lock_fd'] = bad
    with pytest.raises(ValueError):
        contract.validate_request(request, plan, request['plan_sha256'])


@pytest.mark.parametrize('fd', [3, contract.MAX_FD])
def test_verify_fd_boundaries(fd):
    plan = valid_plan()
    request = valid_request(plan, 'verify')
    request['lock_fd'] = fd
    contract.validate_request(request, plan, request['plan_sha256'])


@pytest.mark.parametrize('field', sorted(contract.REPLY_FIELDS))
def test_every_reply_field_is_required(field):
    plan = valid_plan()
    request = valid_request(plan)
    reply = valid_reply(plan, request)
    reply.pop(field)
    with pytest.raises(ValueError):
        contract.validate_reply(reply, request, plan, request['plan_sha256'])


@pytest.mark.parametrize('field', sorted(contract.BOOLEAN_FIELDS))
@pytest.mark.parametrize('phase', ['stop', 'verify'])
def test_reply_boolean_semantics_cannot_be_flipped_or_use_pseudo_booleans(field, phase):
    plan = valid_plan()
    request = valid_request(plan, phase)
    reply = valid_reply(plan, request)
    for bad in (not reply[field], int(reply[field]), str(reply[field]), None):
        changed = {**reply, field: bad}
        with pytest.raises(ValueError):
            contract.validate_reply(changed, request, plan, request['plan_sha256'])


@pytest.mark.parametrize('field', ['attempt_id', 'plan_sha256', 'phase', 'challenge',
    'new_package_sha256', 'current_package_sha256', 'control_package_sha256', 'plist_sha256',
    'state_before_sha256', 'state_after_sha256'])
@pytest.mark.parametrize('phase', ['stop', 'verify'])
def test_reply_cannot_be_replayed_across_plan_phase_or_state(field, phase):
    plan = valid_plan()
    request = valid_request(plan, phase)
    reply = valid_reply(plan, request)
    reply[field] = '9' * (32 if field in ('attempt_id', 'challenge') else 64)
    with pytest.raises(ValueError):
        contract.validate_reply(reply, request, plan, request['plan_sha256'])


@pytest.mark.parametrize('field,bad', [('uid', 0), ('euid', 0), ('gid', 0), ('egid', 0),
    ('uid', True), ('uid', 499.0), ('kernel_groups', []), ('kernel_groups', [499, 0]),
    ('kernel_groups', [499, 499]), ('kernel_groups', [499.0]), ('kernel_groups', (499,)),
    ('kernel_groups', ['499'])])
def test_observed_identity_must_match_actual_dedicated_ids_and_exact_kernel_groups(field, bad):
    plan = valid_plan()
    request = valid_request(plan)
    reply = valid_reply(plan, request)
    reply['identity'][field] = bad
    with pytest.raises(ValueError):
        contract.validate_reply(reply, request, plan, request['plan_sha256'])


def test_identity_extra_metadata_is_not_accepted_as_actual_kernel_identity():
    plan = valid_plan()
    request = valid_request(plan)
    reply = valid_reply(plan, request)
    reply['identity']['administrator'] = False
    with pytest.raises(ValueError):
        contract.validate_reply(reply, request, plan, request['plan_sha256'])


def test_disabled_old_and_new_control_bytes_use_v1_shape_without_blessing_original_control():
    plan = valid_plan()
    for phase, prefix in (('stop', 'old'), ('verify', 'new')):
        expected = {'type': 'tianming.system-service.v1', 'enabled': False,
            'package_sha256': plan[prefix + '_package_sha256'],
            'plist_sha256': plan[prefix + '_plist_sha256'], 'context_sha256': plan['context_sha256'],
            'publisher_sha256': plan['publisher_sha256']}
        assert contract.disabled_control(plan, phase) == expected
        assert contract.disabled_control_bytes(plan, phase) == json.dumps(
            expected, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        assert contract.digest(contract.disabled_control_bytes(plan, phase)) != plan['old_control_sha256']
    assert contract.disabled_control_bytes(plan, 'stop') != contract.disabled_control_bytes(plan, 'verify')
    for phase in ('verify-old', 'install', None, ['stop']):
        with pytest.raises(ValueError):
            contract.disabled_control(plan, phase)


def test_plan_path_is_fixed_and_never_takes_a_caller_path():
    assert contract.plan_path('a' * 32) == '/Library/TianmingERP/system-upgrade-' + 'a' * 32 + '/plan.json'
    for value in ('../plan.json', '/Library/other', 'A' * 32, 'a' * 33, '', None, 12):
        with pytest.raises(ValueError):
            contract.plan_path(value)


@pytest.mark.parametrize('raw', [b'', b'[]', b'null', b'"text"', b'123', b'{}x', b'\xff',
    b'\xef\xbb\xbf{}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1.0}',
    b'{"x":1e3}', b'{"x":1e400}', b'{"x":9223372036854775808}', b'{"x":-9223372036854775809}',
    b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":1}}', b'{"list":[{"x":1,"x":2}]}',
    b'{"x":"\\ud800"}', b'{"' + b'x' * contract.LIMIT + b'":0}'])
def test_decode_rejects_ambiguous_non_object_or_unbounded_wire_records(raw):
    with pytest.raises(ValueError):
        contract.decode(raw)


def test_depth_and_byte_limits_and_integer_edges():
    raw = b'{}' + b' ' * (contract.LIMIT - 2)
    assert contract.decode(raw) == {}
    with pytest.raises(ValueError):
        contract.decode(raw + b' ')
    assert contract.decode(b'{"max":9223372036854775807,"min":-9223372036854775808}') == {
        'max': 2**63 - 1, 'min': -2**63}
    for depth in (contract.MAX_DEPTH + 1, 1000):
        raw = b'{"x":' + b'[' * depth + b'0' + b']' * depth + b'}'
        with pytest.raises(ValueError):
            contract.decode(raw)


@pytest.mark.parametrize('raw', ['{}', bytearray(b'{}'), memoryview(b'{}'), None])
def test_decode_and_digest_only_accept_original_bytes(raw):
    with pytest.raises(ValueError):
        contract.decode(raw)
    with pytest.raises(ValueError):
        contract.digest(raw)


def test_canonical_matches_bootstrap_ascii_bytes_and_does_not_confuse_object_and_raw_digest():
    value = {'中文': '原恢复保持', 'z': [False, None, 3]}
    expected = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    assert contract.canonical(value) == expected
    assert contract.digest(expected) == hashlib.sha256(expected).hexdigest()
    assert contract.digest(b'{}') != contract.digest(b'{ }')  # Original-byte plan binding.
    for bad in ({1: 'integer key'}, {'x': float('nan')}, {'x': 1.0}, {'x': object()}, {'x': 2**63}):
        with pytest.raises(ValueError):
            contract.canonical(bad)


def test_module_is_stdlib_only_and_runs_with_site_disabled_on_this_host():
    source = Path(contract.__file__).read_text()
    tree = ast.parse(source, feature_version=(3, 9))
    assert {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names} == {
        'hashlib', 'json', 'math', 're'}
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(tree))
    code = ('import importlib.util,sys; '
            'spec=importlib.util.spec_from_file_location("pure_contract",sys.argv[1]); '
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module); '
            'assert module.PHASES=={"stop","verify"}; '
            'assert not any(n.startswith("desktop_assistant") for n in sys.modules); '
            'print("pure-stdlib-contract-ok")')
    completed = subprocess.run([sys.executable, '-I', '-S', '-c', code, contract.__file__],
        capture_output=True, text=True, check=True, timeout=10)
    assert completed.stdout.strip() == 'pure-stdlib-contract-ok'
    assert completed.stderr == ''

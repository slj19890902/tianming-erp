"""Activation-preserving monitor transaction on disposable files only.

Root/launchd/UID are explicit adapters. The original home suite is unchanged.
"""
import json

import pytest

from desktop_assistant import mac_system_upgrade as upgrade
from desktop_assistant import mac_system_upgrade_contract as contract
from desktop_assistant import owner_activation_contract as owner
from tests.desktop_assistant.test_mac_system_upgrade import case, put
from tests.desktop_assistant.test_mac_system_upgrade_contract import valid_plan, valid_request, valid_reply

FACTORY_PLAN = 'tianming.system-upgrade-preserve-activation-plan.v1'
CAPABILITY = 'system_upgrade_preserve_activation_v1'


def binding_records(context_sha):
    # Shape-only root fixture; genuine owner signatures are exercised separately.
    record = dict(type=owner.TYPE, context_sha256=context_sha,
        request={'request_id': '8' * 32, 'target': {'label': '合成实例'}},
        network={}, authorization='synthetic', authorization_sha256='a' * 64,
        registry_sha256='b' * 64, activated_at='2026-10-10T00:00:00Z', target_snapshot={})
    raw = owner.canonical(record) + b'\n'
    gate = dict(type=owner.GATE, attempt_id='9' * 32, plan_sha256='2' * 64,
        activation_sha256=contract.digest(raw), request_sha256=owner.digest(record['request']), ready=True)
    return raw, owner.canonical(gate) + b'\n'


def factory_plan():
    return {**valid_plan(), 'type': FACTORY_PLAN, 'activation_sha256': '7' * 64,
            'owner_publication_sha256': '8' * 64}


def test_factory_plan_is_a_separate_exact_contract():
    plan = factory_plan()
    assert contract.validate_plan(plan) == plan
    assert contract.PLAN_FIELDS == frozenset(valid_plan())
    for phase in ('stop', 'verify'):
        request = valid_request(plan, phase)
        reply = valid_reply(plan, request)
        # This flag describes the worker's environment, not configured ERP mode.
        assert reply['home_rehearsal'] is True
        assert contract.validate_reply(reply, request, plan, request['plan_sha256']) == reply


@pytest.mark.parametrize('fault', ['home-type', 'unknown-type', 'missing', 'extra', 'none', 'bool', 'upper', 'short'])
def test_factory_plan_never_extends_or_weakens_home_plan(fault):
    plan = factory_plan()
    if fault == 'home-type': plan['type'] = contract.PLAN
    elif fault == 'unknown-type': plan['type'] += '.unknown'
    elif fault == 'missing': plan.pop('activation_sha256')
    elif fault == 'extra': plan['allow_factory'] = True
    else: plan['owner_publication_sha256'] = {'none': None, 'bool': True, 'upper': 'A' * 64, 'short': 'a'}[fault]
    with pytest.raises(ValueError): contract.validate_plan(plan)


@pytest.fixture
def factory_case(case, monkeypatch):
    c = case
    activation, gate = binding_records(c.plan['context_sha256'])
    c.active_path = c.base / 'factory-activation.json'
    c.gate_path = c.base / owner.GATE_NAME
    put(c.active_path, activation, 0o644); put(c.gate_path, gate, 0o644)
    c.original_authority = (activation, gate)
    old_sha = c.sha
    c.plan.update(type=FACTORY_PLAN, activation_sha256=contract.digest(activation),
                  owner_publication_sha256=contract.digest(gate))
    c.raw = contract.canonical(c.plan); c.sha = contract.digest(c.raw)
    c.manifest['runtime_capabilities'][CAPABILITY] = 1
    c.manifest['files']['desktop_assistant/mac_factory_runtime.py'] = 'a' * 64
    home_worker, home_launch = c.worker, c.launch
    def worker(plan, sha, meta, phase, fd=None):
        assert sha == c.sha
        return home_worker(plan, old_sha, meta, phase, fd)
    monkeypatch.setattr(upgrade, '_worker', worker)
    c.factory_worker = worker
    def launch(action):
        home_launch(action)
        if action == 'bootstrap':
            path = c.control / 'service-status.json'
            value = json.loads(path.read_bytes())
            value.update(configured_mode='factory', home_rehearsal=False)
            put(path, upgrade.boot.canonical(value))
    monkeypatch.setattr(upgrade, '_launch', launch)
    c.factory_launch = launch
    return c


def test_factory_make_plan_freezes_existing_authority_without_writes(factory_case):
    c = factory_case
    before = {str(p): p.read_bytes() for p in c.base.rglob('*') if p.is_file()}
    plan = upgrade.make_plan(c.old['package'], c.new['package'])
    assert plan['type'] == FACTORY_PLAN
    assert plan['activation_sha256'] == c.plan['activation_sha256']
    assert plan['owner_publication_sha256'] == c.plan['owner_publication_sha256']
    assert before == {str(p): p.read_bytes() for p in c.base.rglob('*') if p.is_file()}


def test_factory_full_transaction_preserves_activation_and_business_current(factory_case):
    c = factory_case; c.prepare(); result = c.apply()
    assert result['configured_mode'] == 'factory'
    assert result['factory_activated'] is result['activation_preserved'] is True
    assert result['activation_performed'] is result['erp_started'] is False
    assert result['activation_sha256'] == c.plan['activation_sha256']
    assert result['owner_publication_sha256'] == c.plan['owner_publication_sha256']
    assert result['current_package_sha256'] == c.state['current']
    assert (c.root / 'state.json').read_bytes() == c.stopped
    assert (c.active_path.read_bytes(), c.gate_path.read_bytes()) == c.original_authority
    assert not (c.slot / 'failed.json').exists()
    assert not c.locked('operation.lock') and not c.locked('service-supervisor.lock')


@pytest.mark.parametrize('fault', ['old-plan', 'bad-type', 'context', 'gate-missing', 'not-ready',
    'gate-activation', 'gate-request', 'gate-extra', 'gate-bool-sha', 'failed', 'active-link', 'gate-link',
    'activation-sha', 'gate-sha', 'cap-missing', 'cap-bool', 'runtime-missing'])
def test_factory_rejects_incomplete_or_unapproved_authority_before_stop(factory_case, fault):
    c = factory_case
    if fault == 'old-plan':
        c.plan.pop('activation_sha256'); c.plan.pop('owner_publication_sha256'); c.plan['type'] = contract.PLAN
    elif fault in ('bad-type', 'context'):
        value = json.loads(c.active_path.read_bytes())
        value['type' if fault == 'bad-type' else 'context_sha256'] = 'unknown' if fault == 'bad-type' else 'f' * 64
        put(c.active_path, owner.canonical(value), 0o644)
        if fault == 'context':
            # Even a self-consistent gate and reapproved byte SHA cannot move
            # this authority to a different installed context.
            gate = json.loads(c.gate_path.read_bytes())
            gate['activation_sha256'] = contract.digest(c.active_path.read_bytes())
            put(c.gate_path, owner.canonical(gate), 0o644)
            c.plan.update(activation_sha256=contract.digest(c.active_path.read_bytes()),
                          owner_publication_sha256=contract.digest(c.gate_path.read_bytes()))
    elif fault == 'gate-missing': c.gate_path.unlink()
    elif fault.startswith('gate-') and fault not in ('gate-link', 'gate-sha') or fault == 'not-ready':
        value = json.loads(c.gate_path.read_bytes())
        if fault == 'not-ready': value['ready'] = False
        elif fault == 'gate-extra': value['unapproved'] = True
        else: value[{'gate-activation': 'activation_sha256', 'gate-request': 'request_sha256',
                     'gate-bool-sha': 'plan_sha256'}[fault]] = True if fault == 'gate-bool-sha' else 'f' * 64
        put(c.gate_path, owner.canonical(value), 0o644)
    elif fault == 'failed': put(c.base / owner.FAILED, b'failed')
    elif fault in ('active-link', 'gate-link'):
        path = c.active_path if fault == 'active-link' else c.gate_path
        saved = c.base / 'saved.json'; path.rename(saved); path.symlink_to(saved)
    elif fault in ('activation-sha', 'gate-sha'):
        c.plan['activation_sha256' if fault == 'activation-sha' else 'owner_publication_sha256'] = 'f' * 64
    elif fault == 'cap-missing': c.manifest['runtime_capabilities'].pop(CAPABILITY)
    elif fault == 'cap-bool': c.manifest['runtime_capabilities'][CAPABILITY] = True
    else: c.manifest['files'].pop('desktop_assistant/mac_factory_runtime.py')
    c.raw = contract.canonical(c.plan); c.sha = contract.digest(c.raw)
    with pytest.raises((ValueError, OSError)): c.prepare()
    assert not c.slot.exists() and 'stop' not in c.trace and 'disable' not in c.trace
    assert (c.root / 'state.json').read_bytes() == c.before


@pytest.mark.parametrize('phase', ['stop', 'verify', 'bootstrap'])
@pytest.mark.parametrize('fault', ['activation-bytes', 'gate-bytes', 'failed', 'restore-state', 'registry'])
def test_factory_authority_drift_fails_closed_at_each_transaction_boundary(factory_case, monkeypatch, phase, fault):
    c = factory_case; c.prepare()
    def mutate():
        if fault == 'activation-bytes': c.active_path.write_bytes(c.active_path.read_bytes() + b' ')
        elif fault == 'gate-bytes': c.gate_path.write_bytes(c.gate_path.read_bytes() + b' ')
        elif fault == 'failed': put(c.base / owner.FAILED, b'failed')
        elif fault == 'restore-state':
            value = json.loads((c.root / 'state.json').read_bytes()); value['native_restore_binding']['extra'] = True
            put(c.root / 'state.json', upgrade.boot.canonical(value))
        else: c.metas[c.new['package']]['registry_sha256'] = 'f' * 64
    def worker(*args, **kwargs):
        result = c.factory_worker(*args, **kwargs)
        if args[3] == phase: mutate()
        return result
    def launch(action):
        c.factory_launch(action)
        if action == phase: mutate()
    monkeypatch.setattr(upgrade, '_worker', worker); monkeypatch.setattr(upgrade, '_launch', launch)
    with pytest.raises(upgrade.UpgradeError) as error: c.apply()
    assert error.value.result['gate_disabled_confirmed'] is True
    assert json.loads(upgrade.system.CONTROL.read_bytes())['enabled'] is False
    assert (c.slot / 'failed.json').exists() and not (c.slot / 'verified.json').exists()
    # An external mutation is retained, never rewritten to make verification pass.
    assert c.active_path.read_bytes() == c.original_authority[0] + (b' ' if fault == 'activation-bytes' else b'')
    assert c.gate_path.read_bytes() == c.original_authority[1] + (b' ' if fault == 'gate-bytes' else b'')


@pytest.mark.parametrize('fault', ['home', 'attention', 'home-flag', 'keychain', 'stale', 'identity', 'old-pid'])
def test_factory_new_monitor_must_report_authenticated_factory_manual_stop(factory_case, monkeypatch, fault):
    c = factory_case; c.prepare()
    def launch(action):
        c.factory_launch(action)
        if action == 'bootstrap':
            path = c.control / 'service-status.json'; value = json.loads(path.read_bytes())
            if fault in ('home', 'attention'): value['configured_mode'] = fault
            elif fault == 'home-flag': value['home_rehearsal'] = True
            elif fault == 'keychain': value['keychain_canary_verified'] = False
            elif fault == 'stale': value['updated_at'] = '2000-01-01T00:00:00+00:00'
            elif fault == 'old-pid': value['pid'] = c.old_identity['pid']
            else: value['system_identity']['uid'] = 0
            put(path, upgrade.boot.canonical(value))
    monkeypatch.setattr(upgrade, '_launch', launch)
    if fault == 'old-pid': monkeypatch.setattr(upgrade, 'START_TIMEOUT', 0.05)
    with pytest.raises(upgrade.UpgradeError): c.apply()
    assert (c.active_path.read_bytes(), c.gate_path.read_bytes()) == c.original_authority


@pytest.mark.parametrize('fault', ['pid_reuse', 'exited_reused_pid', 'replacement', 'job_absent',
    'missing_exit', 'label_persists', 'residual', 'unknown_control', 'state_after', 'log_inode_after'])
def test_factory_preserves_existing_process_and_cas_race_protections(factory_case, monkeypatch, fault):
    from tests.desktop_assistant import test_mac_system_upgrade as home_tests
    c = factory_case; c.worker, c.launch = c.factory_worker, c.factory_launch
    home_tests.test_post_stop_race_does_not_promote_unknown_scene(c, monkeypatch, fault)
    assert (c.active_path.read_bytes(), c.gate_path.read_bytes()) == c.original_authority


@pytest.mark.parametrize('phase', ['disable', 'stop', 'bootout', 'verify', 'enable', 'bootstrap', 'fresh_status'])
def test_factory_retains_fail_closed_for_every_original_failure_phase(factory_case, monkeypatch, phase):
    from tests.desktop_assistant import test_mac_system_upgrade as home_tests
    c = factory_case; c.worker, c.launch = c.factory_worker, c.factory_launch
    home_tests.test_failure_keeps_known_gate_disabled_and_pending_without_rollback(c, monkeypatch, phase)
    assert (c.active_path.read_bytes(), c.gate_path.read_bytes()) == c.original_authority


def test_home_plan_still_rejects_factory_status(case, monkeypatch):
    c = case; c.prepare()
    def launch(action):
        c.launch(action)
        if action == 'bootstrap':
            path = c.control / 'service-status.json'; value = json.loads(path.read_bytes())
            value.update(configured_mode='factory', home_rehearsal=False)
            put(path, upgrade.boot.canonical(value))
    monkeypatch.setattr(upgrade, '_launch', launch)
    with pytest.raises(upgrade.UpgradeError) as error: c.apply()
    assert error.value.result['gate_disabled_confirmed'] is True

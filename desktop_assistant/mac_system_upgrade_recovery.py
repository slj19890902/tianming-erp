"""Bounded roll-forward of NEW plist / disabled OLD control, never ERP start.

An external fixed-SHA loader authenticates the entire closed stdlib pack first.
Only a failed original factory upgrade, with all original records intact, may
produce a separately approved recovery plan. No arbitrary path or CLI exists.
There is no resume, rollback, activation or business-state mutation here.
"""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import time
import uuid

from desktop_assistant import mac_system_upgrade as upgrade
from desktop_assistant import mac_system_upgrade_contract as contract
from desktop_assistant import mac_system_upgrade_recovery_contract as recovery

MODULE = 'desktop_assistant/mac_system_upgrade_recovery.py'
CONTRACT_MODULE = 'desktop_assistant/mac_system_upgrade_recovery_contract.py'


class RecoveryError(ValueError):
    def __init__(self, result):
        super().__init__(recovery.ERROR)
        self.result = result


def _entry():
    upgrade._entry(); upgrade.verify_code()
    recovery.require(Path(__file__).absolute() == upgrade._code_root() / MODULE
                     and Path(recovery.__file__).absolute() == upgrade._code_root() / CONTRACT_MODULE)


def _source():
    return contract.decode(upgrade.boot.protected(
        upgrade._code_root() / 'install-manifest.json', readable=True))['source']


def _metadata(plan):
    recovery.require(plan['type'] == contract.FACTORY_PLAN)
    old, new = upgrade._metadata(plan)
    manifest = upgrade.system._program_manifest(new)
    capability = manifest.get('runtime_capabilities', {}).get(recovery.CAPABILITY)
    recovery.require(type(capability) is int and capability == 1
                     and {MODULE, CONTRACT_MODULE} <= manifest.get('files', {}).keys()
                     and manifest.get('git_sha') == _source())
    return old, new


@contextmanager
def _evidence(attempt, paths):
    recovery.require(contract.token(attempt))
    with ExitStack() as held:
        slot = held.enter_context(upgrade.Pin(upgrade.BASE / ('system-upgrade-' + attempt),
            mode=0o755, directory=True, parent=paths['base']))
        recovery.require(set(os.listdir(slot.fd)) == recovery.EVIDENCE)
        pins = {name: held.enter_context(upgrade.Pin(slot.path / name, parent=slot,
                    mode=0o644 if name == 'plan.json' else 0o600)) for name in recovery.EVIDENCE}
        raw = {name: pin.read() for name, pin in pins.items()}
        def check():
            slot.check(); recovery.require(set(os.listdir(slot.fd)) == recovery.EVIDENCE)
            for name, pin in pins.items():
                pin.check(); recovery.require(pin.read() == raw[name])
        check(); yield raw, check; check()


def _facts(raw, paths):
    sha = contract.digest(raw['plan.json'])
    plan = upgrade._header(raw['plan.json'], sha)
    old, new = _metadata(plan)
    paths['check']()
    recovery.require(paths['authority'] == {key: plan[key] for key in
        ('activation_sha256', 'owner_publication_sha256', 'context_sha256')})
    value = {name: contract.decode(data) for name, data in raw.items() if name != 'old.plist'}
    recovery.require(value['prepared.json'] == {'type': 'tianming.system-upgrade-prepared.v1',
                                              'plan_sha256': sha})
    old_control = value['old-control.json']
    recovery.require(type(old_control.get('enabled')) is bool
                     and old_control == upgrade.system.record_for(old, old_control['enabled'])
                     and contract.digest(raw['old-control.json']) == plan['old_control_sha256']
                     and raw['old.plist'] == upgrade.system._plist(old['package'], old['publisher_sha256'])
                     and contract.digest(raw['old.plist']) == plan['old_plist_sha256'])
    pending = value['pending.json']
    recovery.require(set(pending) == {'type', 'plan_sha256', 'old_monitor', 'logs',
        'state_before_sha256', 'state_stopped_sha256'}
        and pending['type'] == 'tianming.system-upgrade-attempt.v1' and pending['plan_sha256'] == sha
        and all(pending[key] == plan[key] for key in ('state_before_sha256', 'state_stopped_sha256')))
    identity = recovery.process(pending['old_monitor'], str((old['tree'] / 'runtime/bin/python3.12').resolve(strict=True)))
    recovery.require(pending['logs'] == {item.path.name: list(item.initial[:6]) for item in paths['logs']})
    exited = value['old-exited.json']
    recovery.require(set(exited) == {'old_monitor', 'kernel_exit_observed', 'label_absent'}
                     and exited['old_monitor'] == identity
                     and exited['kernel_exit_observed'] is True and exited['label_absent'] is True)
    absent = value['old-service-processes.json']
    recovery.require(set(absent) == {'old_interpreter_absent_for_service_uid', 'sampled_process_count'}
                     and absent['old_interpreter_absent_for_service_uid'] is True
                     and type(absent['sampled_process_count']) is int and 0 <= absent['sampled_process_count'] <= 131072)
    stop = value['stop.json']
    request = dict(type=contract.REQUEST, attempt_id=plan['attempt_id'], plan_sha256=sha,
                   phase='stop', challenge=stop.get('challenge'), lock_fd=None)
    contract.validate_request(request, plan, sha); contract.validate_reply(stop, request, plan, sha)
    failed = value['failed.json']
    recovery.require(set(failed) == {'type', 'plan_sha256', 'attempt_id', 'preserve_scene', 'do_not_retry',
        'error_type', 'gate_disabled_confirmed', 'label_disabled_confirmed'}
        and failed['type'] == 'tianming.system-upgrade-failure.v1'
        and failed['plan_sha256'] == sha and failed['attempt_id'] == plan['attempt_id']
        and type(failed['error_type']) is str and 0 < len(failed['error_type']) <= 80
        and all(failed[key] is True for key in
                ('preserve_scene', 'do_not_retry', 'gate_disabled_confirmed', 'label_disabled_confirmed')))
    return plan, old, new, identity


def _state_and_tuple(plan, paths, control):
    paths['check']()
    raw, state = upgrade._state(paths['root'], 499, 499)
    recovery.require(contract.digest(raw) == plan['state_stopped_sha256']
                     and state.get('manual_stop') is True and state.get('current') == plan['current_package_sha256'])
    with upgrade.Pin(upgrade.system.CONTROL, parent=paths['base']) as item:
        recovery.require(item.read() == control)
    with upgrade.Pin(upgrade.system.PLIST, parent=paths['plists']) as item:
        recovery.require(item.read() == upgrade.system._plist(plan['new_package_sha256'], plan['publisher_sha256']))


def _absent(old, new):
    recovery.require(upgrade._job() is None)
    upgrade._no_old_monitor(old); upgrade._no_old_monitor(new)
    recovery.require(upgrade._job() is None)


def _bound(value, raw, plan):
    recovery.validate(value, now=time.time())
    recovery.require(value['attempt_id'] == plan['attempt_id']
        and value['upgrade_plan_sha256'] == contract.digest(raw['plan.json'])
        and value['evidence_sha256'] == {name: contract.digest(data) for name, data in raw.items()}
        and all(value[key] == plan[key] for key in recovery.BINDINGS)
        and value['expires_at'] <= plan['expires_at'] and value['source'] == _source())


def make_plan(attempt_id):
    """Inspect only the exact failed split. Never stop or change a control."""
    _entry()
    with upgrade._publication() as publication, upgrade._paths(factory=True) as paths, \
            _evidence(attempt_id, paths) as (raw, evidence_check):
        plan, old, new, _ = _facts(raw, paths)
        with upgrade._locked(paths['operation']), upgrade._locked(paths['monitor']):
            _state_and_tuple(plan, paths, contract.disabled_control_bytes(plan, 'stop')); _absent(old, new)
            now = int(time.time())
            value = dict(type=recovery.TYPE, recovery_id=uuid.uuid4().hex, attempt_id=attempt_id,
                created_at=now, expires_at=min(now + contract.MAX_AGE, plan['expires_at']), source=_source(),
                upgrade_plan_sha256=contract.digest(raw['plan.json']),
                evidence_sha256={name: contract.digest(data) for name, data in raw.items()},
                **{key: plan[key] for key in recovery.BINDINGS})
            _bound(value, raw, plan); publication.check(); evidence_check(); paths['check']()
            return value


def _header(raw, approved_sha):
    recovery.require(contract.sha(approved_sha) and contract.digest(raw) == approved_sha)
    value = recovery.validate(contract.decode(raw), now=time.time())
    recovery.require(value['code_manifest_sha256'] == upgrade.verify_code() and value['source'] == _source())
    return value


def prepare(raw, approved_sha):
    """Publish a fresh bounded recovery plan; all old failure records stay held."""
    _entry(); value = _header(raw, approved_sha)
    with upgrade._publication() as publication, upgrade._paths(factory=True) as paths, \
            _evidence(value['attempt_id'], paths) as (original, evidence_check):
        plan, old, new, _ = _facts(original, paths); _bound(value, original, plan)
        with upgrade._locked(paths['operation']), upgrade._locked(paths['monitor']):
            _state_and_tuple(plan, paths, contract.disabled_control_bytes(plan, 'stop')); _absent(old, new)
            name = 'system-upgrade-recovery-' + value['recovery_id']
            os.mkdir(name, mode=0o755, dir_fd=paths['base'].fd)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=paths['base'].fd)
            try: os.fchmod(fd, 0o755); os.fsync(fd)
            finally: os.close(fd)
            os.fsync(paths['base'].fd)
            with upgrade.Pin(upgrade.BASE / name, mode=0o755, directory=True, mutable=True, parent=paths['base']) as slot:
                upgrade._new(slot, 'plan.json', raw, mode=0o644)
                evidence_check(); publication.check(); _facts(original, paths)
                _state_and_tuple(plan, paths, contract.disabled_control_bytes(plan, 'stop')); _absent(old, new)
                upgrade._new(slot, 'prepared.json', contract.canonical({
                    'type': 'tianming.system-upgrade-split-recovery-prepared.v1', 'plan_sha256': approved_sha}))
        return {'recovery_id': value['recovery_id'], 'plan_sha256': approved_sha, 'service_changed': False}


def apply(recovery_id, approved_sha):
    """One attempt: disabled NEW -> original signed verify -> fresh monitor."""
    _entry(); recovery.require(contract.token(recovery_id) and contract.sha(approved_sha))
    with upgrade._publication() as publication, upgrade._paths(factory=True) as paths, ExitStack() as held:
        slot = held.enter_context(upgrade.Pin(upgrade.BASE / ('system-upgrade-recovery-' + recovery_id),
            mode=0o755, directory=True, mutable=True, parent=paths['base']))
        recovery.require(set(os.listdir(slot.fd)) == {'plan.json', 'prepared.json'})
        plan_file = held.enter_context(upgrade.Pin(slot.path / 'plan.json', parent=slot))
        raw = plan_file.read(); value = _header(raw, approved_sha)
        recovery.require(value['recovery_id'] == recovery_id)
        prepared = held.enter_context(upgrade.Pin(slot.path / 'prepared.json', mode=0o600, parent=slot))
        recovery.require(contract.decode(prepared.read()) == {
            'type': 'tianming.system-upgrade-split-recovery-prepared.v1', 'plan_sha256': approved_sha})
        started_attempt = False
        try:
            # Evidence teardown validates its still-pinned original bytes. It
            # must finish inside this catch while slot/paths FDs remain alive.
            with _evidence(value['attempt_id'], paths) as (original, evidence_check):
                plan, old, new, old_identity = _facts(original, paths); _bound(value, original, plan)
                current_control = contract.disabled_control_bytes(plan, 'stop')
                def check():
                    publication.check(); plan_file.check(); prepared.check(); evidence_check()
                    recovery.require(plan_file.read() == raw)
                    _header(raw, approved_sha); _bound(value, original, plan); _facts(original, paths)
                    _state_and_tuple(plan, paths, current_control)
                check()
                with upgrade._locked(paths['operation']) as fd:
                    with upgrade._locked(paths['monitor']):
                        check(); _absent(old, new)
                        # Include lock exit, receipt and every post-rename failure
                        # in the same fail-closed boundary as the original upgrade.
                        started_attempt = True
                        upgrade._new(slot, 'pending.json', contract.canonical({'type': 'tianming.system-upgrade-split-recovery-attempt.v1',
                            'plan_sha256': approved_sha, 'upgrade_plan_sha256': value['upgrade_plan_sha256']}))
                        check(); _absent(old, new)
                        disabled = contract.disabled_control_bytes(plan, 'verify')
                        upgrade._cas(paths['base'], upgrade.system.CONTROL.name, current_control, disabled,
                                     'system-upgrade-recovery-' + recovery_id + '-select-new.json')
                        current_control = disabled; check(); _absent(old, new)
                        reply = upgrade._worker(plan, value['upgrade_plan_sha256'], new, 'verify', fd)
                        check(); _absent(old, new)
                        upgrade._new(slot, 'verify.json', contract.canonical(reply))
                    check(); _absent(old, new)
                    enabled = contract.disabled_control(plan, 'verify'); enabled['enabled'] = True
                    enabled = contract.canonical(enabled)
                    upgrade._cas(paths['base'], upgrade.system.CONTROL.name, current_control, enabled,
                                 'system-upgrade-recovery-' + recovery_id + '-enable-new.json')
                    current_control = enabled; check(); _absent(old, new)
                    upgrade._launch('enable'); started = time.time(); upgrade._launch('bootstrap')
                identity, status = upgrade._wait_new(new, old_identity, started, paths, check)
                check(); recovery.require(upgrade._monitor(new) == identity)
                result = dict(type='tianming.system-upgrade-split-recovery-result.v1',
                    recovery_id=recovery_id, attempt_id=value['attempt_id'], plan_sha256=approved_sha,
                    upgrade_plan_sha256=value['upgrade_plan_sha256'], current_package_sha256=plan['current_package_sha256'],
                    old_package_sha256=old['package'], new_package_sha256=new['package'], new_monitor=identity,
                    system_identity=status['system_identity'], state_sha256=plan['state_stopped_sha256'],
                    manual_stop=True, erp_started=False, root_gate_enabled=True, configured_mode='factory',
                    activation_preserved=True, activation_performed=False, activation_sha256=plan['activation_sha256'],
                    owner_publication_sha256=plan['owner_publication_sha256'], system_keychain_canary_verified=True)
                upgrade._new(slot, 'verified.json', contract.canonical(result))
                check(); recovery.require(upgrade._monitor(new) == identity)
                return result
        except (Exception, KeyboardInterrupt) as exc:
            if not started_attempt: raise
            _failed(slot, value, approved_sha, plan, paths, original, exc)


def _failed(slot, value, sha, plan, paths, original, exc):
    closure = upgrade._fail_closed(plan, paths, original['old-control.json'])
    result = dict(type='tianming.system-upgrade-split-recovery-failure.v1', recovery_id=value['recovery_id'],
        attempt_id=value['attempt_id'], plan_sha256=sha, preserve_scene=True, do_not_retry=True,
        error_type=type(exc).__name__, **closure)
    try: upgrade._new(slot, 'failed.json', contract.canonical(result))
    except Exception: pass
    raise RecoveryError(result) from None

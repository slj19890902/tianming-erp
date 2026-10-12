"""Real synthetic pipes/FD locks, adapted root identity and signed metadata.

No installed ERP, operational keychain, NAS, privileged service or real UID
transition. The root producer/closed code pack and publication remain pending.
"""
from copy import deepcopy
import fcntl
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import current_mac_dispatch as d, current_mac_worker_contract as c
from desktop_assistant import mac_system_upgrade as root, mac_keychain_install as boot
from desktop_assistant.home_process import PROFILE
from tests.desktop_assistant.test_current_mac_worker_contract import chain


@pytest.fixture
def scene(chain, tmp_path, monkeypatch):
    plans, requests, replies, hashes, _ = deepcopy(chain)
    base = tmp_path / 'base'; base.mkdir(mode=0o755); base.chmod(0o755)
    instance = base / 'instance'; instance.mkdir(mode=0o700)
    control = instance / 'control'; control.mkdir(mode=0o700)
    lock = control / 'operation.lock'; lock.write_bytes(b'0'); lock.chmod(0o600)
    slot = base / ('current-mac-cutover-' + plans['prepare']['attempt_id'])
    slot.mkdir(mode=0o755); slot.chmod(0o755)
    for phase in c.PHASES:
        path = slot / (phase + '-plan.json')
        path.write_bytes(c.canonical(plans[phase])); path.chmod(0o644)
    original_pin = root.Pin
    def pin(path, **kwargs):
        kwargs['uid'] = os.getuid(); kwargs['gid'] = os.getgid()
        return original_pin(path, **kwargs)
    monkeypatch.setattr(root, 'Pin', pin)
    monkeypatch.setattr(root, 'DATA', tmp_path)
    monkeypatch.setattr(boot, 'no_acl', lambda fd: None)
    monkeypatch.setattr(root, '_entry', lambda: None)
    monkeypatch.setattr(d, 'BASE', base); monkeypatch.setattr(d, 'ROOT', instance)
    monkeypatch.setattr(c, 'BASE', str(base))
    monkeypatch.setattr(d.time, 'time', lambda: plans['prepare']['created_at'] + 10)
    manifest = dict(runtime_capabilities={d.CAPABILITY: 1},
        files={d.WORKER: 'synthetic', 'desktop_assistant/current_mac_worker_contract.py': 'synthetic'})
    def metadata(package):
        return dict(tree=tmp_path / package,
            context=dict(uid=499, gid=499), public=b'synthetic public',
            **{key: plans['prepare'][key] for key in
                ('context_sha256', 'publisher_sha256', 'registry_sha256')})
    monkeypatch.setattr(d.system, 'metadata', metadata)
    monkeypatch.setattr(d.system, '_program_manifest', lambda meta: manifest)
    monkeypatch.setattr(d, 'command', lambda argv, env: ['/usr/bin/sandbox-exec', '-p', PROFILE, *argv])
    verifier = Mock(return_value=plans['prepare']['code_manifest_sha256'])
    calls = []; finished = tmp_path / 'child-finished'
    original_popen = subprocess.Popen
    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        phase = argv[argv.index('--phase') + 1]
        reply = deepcopy(replies[phase])
        reply['challenge'] = argv[argv.index('--challenge') + 1]
        reply['plan_sha256'] = argv[argv.index('--plan-sha256') + 1]
        # Run a real unprivileged child; root/UID/sandbox observations above are
        # assertions on the proposed invocation, not privileged acceptance.
        script = '''import os,sys,fcntl
if sys.argv[3]:
    fd=int(sys.argv[3])
    assert os.pread(fd,1,0)==b'0'
    assert fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY
    with open(sys.argv[4],'rb') as other:
        try: fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: pass
        else: raise AssertionError('Parent lock not held in actual child')
os.write(1,sys.argv[1].encode())
open(sys.argv[2],'w').write('finished')
'''
        return original_popen([sys.executable, '-I', '-B', '-c', script,
            c.canonical(reply).decode(), str(finished),
            str(kwargs['pass_fds'][0]) if kwargs['pass_fds'] else '', str(lock)], stdin=kwargs['stdin'],
            stdout=kwargs['stdout'], stderr=kwargs['stderr'], close_fds=True,
            pass_fds=kwargs['pass_fds'], cwd='/', env=kwargs['env'])
    monkeypatch.setattr(d.subprocess, 'Popen', launch)
    return SimpleNamespace(
        plans=plans, replies=replies, slot=slot, base=base, lock=lock, calls=calls,
        verifier=verifier, finished=finished, manifest=manifest, metadata=metadata)


def run(s, phase='prepare', fd=None):
    return d.dispatch(c.canonical(s.plans[phase]), verify_code=s.verifier, inherited_lock=fd)


def test_prepare_fixed_identity_environment_policy_and_reply(scene):
    result = run(scene)
    argv, options = scene.calls[0]
    assert argv[:3] == ['/usr/bin/sandbox-exec', '-p', PROFILE]
    assert '-I' in argv and '-B' in argv and argv[6].endswith(d.WORKER)
    assert options['user'] == options['group'] == 499 and options['extra_groups'] == []
    assert options['pass_fds'] == () and options['cwd'] == '/' and options['umask'] == 0o077
    assert options['env'] == dict(HOME=str(scene.base / 'home'),
        PATH='/usr/bin:/bin:/usr/sbin:/sbin', LANG='en_US.UTF-8', ERP_HOME_REHEARSAL='1')
    assert result['result'] == scene.replies['prepare']['result']
    assert scene.finished.read_text() == 'finished' and scene.verifier.call_count == 3
    assert not (scene.base / 'factory-activation.json').exists()


def test_activate_preserves_parent_lock_after_actual_child_exit(scene):
    with scene.lock.open('rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(scene, 'activate', held.fileno())
        assert result['borrowed_lock'] is True and scene.finished.exists()
        assert scene.calls[0][1]['pass_fds'] == (held.fileno(),)
        with scene.lock.open('rb') as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize('failure', ['unlocked', 'writable', 'other', 'content'])
def test_bad_parent_lock_prevents_child(scene, failure):
    path = scene.lock
    if failure == 'other':
        path = scene.base / 'other.lock'; path.write_bytes(b'0'); path.chmod(0o600)
    if failure == 'content': scene.lock.write_bytes(b'1')
    with path.open('r+b' if failure == 'writable' else 'rb') as held:
        if failure != 'unlocked': fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError): run(scene, 'activate', held.fileno())
    assert not scene.calls


@pytest.mark.parametrize('failure', ['code', 'plan', 'symlink', 'capability', 'identity', 'activation'])
def test_unapproved_inputs_prevent_child(scene, monkeypatch, failure):
    path = scene.slot / 'prepare-plan.json'
    if failure == 'code': scene.verifier.return_value = '0' * 64
    elif failure == 'plan': path.write_bytes(path.read_bytes() + b' ')
    elif failure == 'symlink':
        other = scene.base / 'plan'; other.write_bytes(path.read_bytes())
        path.unlink(); path.symlink_to(other)
    elif failure == 'capability': scene.manifest['runtime_capabilities'][d.CAPABILITY] = True
    elif failure == 'identity':
        def wrong(package):
            meta = scene.metadata(package); meta['context']['uid'] = 501; return meta
        monkeypatch.setattr(d.system, 'metadata', wrong)
    else: (scene.base / 'factory-activation.json').write_bytes(b'{}')
    with pytest.raises((ValueError, OSError)): run(scene)
    assert not scene.calls


def test_update_rejected_before_any_launch(scene):
    with pytest.raises(ValueError): run(scene, 'update')
    assert not scene.calls and not scene.finished.exists()


def test_post_exit_code_failure_retains_parent_lock_and_no_publication(scene):
    scene.verifier.side_effect = [scene.plans['activate']['code_manifest_sha256'], '0' * 64]
    with scene.lock.open('rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError): run(scene, 'activate', held.fileno())
        assert scene.finished.exists()
        with scene.lock.open('rb') as other:
            with pytest.raises(BlockingIOError): fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not (scene.base / 'factory-activation.json').exists()


@pytest.mark.parametrize('failure', ['challenge', 'identity', 'result', 'extra'])
def test_invalid_reply_rejected_after_actual_exit(scene, monkeypatch, failure):
    collect = d._collect
    def changed(process):
        reply = c.decode(collect(process))
        if failure == 'challenge': reply['challenge'] = '0' * 32
        elif failure == 'identity': reply['identity']['kernel_groups'] = [499, 12]
        elif failure == 'result': reply['result']['request']['request_id'] = '0' * 32
        else: reply['unapproved'] = True
        return c.canonical(reply)
    monkeypatch.setattr(d, '_collect', changed)
    with pytest.raises(ValueError): run(scene)
    assert scene.finished.exists() and not (scene.base / 'factory-activation.json').exists()


@pytest.mark.parametrize('failure', ['plan', 'metadata', 'expiry'])
def test_post_exit_recheck_refuses_changed_facts(scene, monkeypatch, failure):
    collect = d._collect
    def changed(process):
        raw = collect(process)
        if failure == 'plan':
            path = scene.slot / 'prepare-plan.json'; path.write_bytes(path.read_bytes() + b' ')
        elif failure == 'metadata':
            monkeypatch.setattr(d.system, 'metadata', lambda package: {
                **scene.metadata(package), 'registry_sha256': '0' * 64})
        else: monkeypatch.setattr(d.time, 'time', lambda: scene.plans['prepare']['expires_at'])
        return raw
    monkeypatch.setattr(d, '_collect', changed)
    with pytest.raises(ValueError): run(scene)
    assert scene.finished.exists() and not (scene.base / 'factory-activation.json').exists()


@pytest.mark.parametrize('script', [
    "import os; os.write(1,b'x'*65537)",
    "import os; os.write(2,b'private detail deliberately not returned')",
    "import sys; sys.exit(1)"])
def test_collector_drains_actual_process_and_rejects_output(script):
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with pytest.raises(ValueError, match='dispatch unconfirmed'): d._collect(process)
    assert process.poll() is not None


def test_collector_interruption_waits_for_original_child(monkeypatch):
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'print("{}")'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wait = process.wait; calls = []
    def interrupted():
        calls.append(True)
        if len(calls) == 1: raise KeyboardInterrupt
        return wait()
    monkeypatch.setattr(process, 'wait', interrupted)
    with pytest.raises(ValueError): d._collect(process)
    assert len(calls) == 2 and process.poll() == 0

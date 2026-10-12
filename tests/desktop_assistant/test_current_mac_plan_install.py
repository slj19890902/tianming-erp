"""Synthetic stage records with real exclusive files, pins and flock.

UID/root metadata and the business worker are explicit adapters. No installed
ERP, operational secret, NAS or source signature is used by this suite.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_assistant import current_mac_plan_install as m
from desktop_assistant import current_mac_worker_contract as c
from desktop_assistant import current_mac_cutover_contract as v3
from desktop_assistant import mac_system_upgrade as root
from tests.desktop_assistant.test_current_mac_dispatch import scene, chain
from tests.desktop_assistant.test_current_mac_cutover_contract import valid_network, valid_body, envelope


@pytest.fixture
def staged(scene, monkeypatch):
    s = scene
    # Only synthetic fixture files; the producer must itself refuse old slots.
    for path in s.slot.iterdir(): path.unlink()
    s.slot.rmdir()
    monkeypatch.setattr(m, 'BASE', s.base)
    monkeypatch.setattr(m, 'ROOT', s.base/'instance')
    monkeypatch.setattr(root, 'BASE', s.base)
    monkeypatch.setattr(root.boot, 'protected', lambda path, **kw: None)
    monkeypatch.setattr(m, '_code', s.verifier)
    stamp = datetime.fromtimestamp(s.plans['update']['created_at']+20, timezone.utc)
    monkeypatch.setattr(m, 'datetime', SimpleNamespace(now=lambda tz: stamp))
    state = s.base/'instance/state.json'
    state.write_bytes(c.canonical(dict(current=s.plans['update']['current_package_sha256'], manual_stop=True, pid=None)))
    state.chmod(0o600)
    home = s.base/'home'; home.mkdir(mode=0o700)
    restore = home/('service-restore-'+s.plans['update']['restore_attempt_id']+'.json')
    restore.write_bytes(b'{"synthetic_restore":true}'); restore.chmod(0o600)
    controls = []
    for module, key, name, raw in [(root.system,'CONTROL','system.json',b'system'),
            (root.system,'PLIST','system.plist',b'plist'),
            (m.stream,'CONTROL','transport.json',b'transport'),
            (m.stream,'CONTRACT','contract.json',b'{"synthetic":true}')]:
        path=s.base/name; path.write_bytes(raw); path.chmod(0o644)
        monkeypatch.setattr(module,key,path); controls.append(path)
    lock=s.base/'publication.lock';lock.write_bytes(b'publication lock\n');lock.chmod(0o600)
    @contextmanager
    def publication():
        with lock.open('rb') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:yield
            finally:fcntl.flock(held,fcntl.LOCK_UN)
    monkeypatch.setattr(root.publication,'lock',publication)
    network=c.canonical(valid_network()); calls=[]
    def dispatch(raw, *, verify_code):
        with lock.open('rb') as other:
            with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=c.decode(raw);assert verify_code()==plan['code_manifest_sha256'];calls.append(plan['phase'])
        reply=deepcopy(s.replies[plan['phase']]);reply['plan_sha256']=c.raw_digest(raw)
        if plan['phase']=='update':
            state.write_bytes(c.canonical(dict(current=plan['execution_package_sha256'],manual_stop=True,pid=None)))
            reply['result'].update(state_before_sha256=plan['state_before_sha256'],
                state_after_sha256=c.raw_digest(state.read_bytes()),previous_package_sha256=plan['current_package_sha256'])
        else:
            prep=reply['result']['preparation']
            for key in ('context_sha256','publisher_sha256','registry_sha256','execution_package_sha256',
                    'monitor_package_sha256','system_control_sha256','system_plist_sha256'):
                prep[key]=plan[key]
            prep['state_sha256']=plan['state_stopped_sha256']
            prep['restore']['restore_receipt_sha256']=plan['restore_receipt_sha256']
            for key in ('transport_package_sha256','transport_control_sha256','transport_contract_sha256'):
                prep['backup'][key]=plan[key]
            reply['result']['request']=v3.make_request(prep,plan['network_sha256'],plan['publisher_sha256'],
                request_id=plan['request_id'],now=stamp)
        return reply
    monkeypatch.setattr(m.activation.dispatch,'dispatch',dispatch)
    def plan():
        p=s.plans['update']
        return m.update_plan(p['attempt_id'],p['execution_package_sha256'],p['monitor_package_sha256'],
            p['transport_package_sha256'],p['restore_attempt_id'],c.raw_digest(restore.read_bytes()),network)
    return SimpleNamespace(s=s,state=state,restore=restore,controls=controls,network=network,
        plan=plan,calls=calls,dispatch=dispatch,stamp=stamp)


def update(t):return m.update(t.plan(),t.network)


def test_public_plan_is_readonly_and_completed_stages_are_exclusive(staged):
    t=staged;before=t.state.read_bytes();raw=t.plan()
    assert t.state.read_bytes()==before and not t.s.slot.exists()
    reply=m.update(raw,t.network)
    assert t.calls==['update'] and reply['result']['nas_independent_readback'] is True
    assert (t.s.slot/'update-completed.json').exists()
    prepared=m.prepare(t.s.plans['update']['attempt_id'])
    assert t.calls==['update','prepare'] and (t.s.slot/'prepare-completed.json').exists()
    assert c.decode((t.s.slot/'preparation.json').read_bytes())==prepared['result']['preparation']
    assert c.decode((t.s.slot/'request.json').read_bytes())==prepared['result']['request']
    with pytest.raises((ValueError,FileExistsError)):m.prepare(t.s.plans['update']['attempt_id'])
    assert t.calls==['update','prepare']
    assert not (t.s.base/'factory-activation.json').exists()


@pytest.mark.parametrize('field,value',[('manual_stop',False),('pid',123)])
def test_running_state_prevents_attempt_creation(staged,field,value):
    t=staged;state=c.decode(t.state.read_bytes());state[field]=value;t.state.write_bytes(c.canonical(state))
    with pytest.raises(ValueError):t.plan()
    assert not t.calls and not t.s.slot.exists()


@pytest.mark.parametrize('change',['control','restore','code','network','state','pending','expired'])
def test_changed_approved_inputs_refuse_before_worker(staged,monkeypatch,change):
    t=staged;raw=t.plan();network=t.network
    if change=='control':t.controls[0].write_bytes(b'changed')
    elif change=='restore':t.restore.write_bytes(b'changed')
    elif change=='code':t.s.verifier.return_value='0'*64
    elif change=='network':network+=b' '
    elif change=='state':t.state.write_bytes(t.state.read_bytes()+b' ')
    elif change=='pending':(t.s.base/'instance/state.json.pending').write_bytes(b'prior')
    else:monkeypatch.setattr(m.time,'time',lambda:c.decode(raw)['expires_at']+1)
    with pytest.raises((ValueError,OSError)):m.update(raw,network)
    assert not t.calls and not t.s.slot.exists()


def test_failed_worker_retains_attempt_and_cannot_retry(staged,monkeypatch):
    t=staged;raw=t.plan()
    def fail(*args,**kwargs):raise ValueError('synthetic worker failure')
    monkeypatch.setattr(m.activation.dispatch,'dispatch',fail)
    with pytest.raises(ValueError):m.update(raw,t.network)
    assert (t.s.slot/'update-attempt.json').exists() and not (t.s.slot/'update-completed.json').exists()
    monkeypatch.setattr(m.activation.dispatch,'dispatch',t.dispatch)
    with pytest.raises(ValueError):m.update(raw,t.network)
    assert not t.calls


@pytest.mark.parametrize('fault',['identity','state','control'])
def test_changed_worker_result_preserves_attempt_without_completion(staged,monkeypatch,fault):
    t=staged;raw=t.plan()
    def changed(*args,**kwargs):
        reply=t.dispatch(*args,**kwargs)
        if fault=='identity':reply['identity']['euid']=501
        elif fault=='state':t.state.write_bytes(t.state.read_bytes()+b' ')
        else:t.controls[0].write_bytes(b'changed')
        return reply
    monkeypatch.setattr(m.activation.dispatch,'dispatch',changed)
    with pytest.raises(ValueError):m.update(raw,t.network)
    assert (t.s.slot/'update-attempt.json').exists()
    assert not (t.s.slot/'update-completed.json').exists()
    assert not (t.s.base/'factory-activation.json').exists()


@pytest.mark.parametrize('name',['update-result.json','update-completed.json','network.json'])
def test_tampered_prior_stage_prevents_prepare(staged,name):
    t=staged;update(t);path=t.s.slot/name
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError):m.prepare(t.s.plans['update']['attempt_id'])
    assert t.calls==['update'] and not (t.s.slot/'prepare-completed.json').exists()


def test_activation_plan_remains_plan_only_with_explicit_attestations(staged,monkeypatch):
    t=staged;update(t);reply=m.prepare(t.s.plans['update']['attempt_id'])
    body=valid_body(v3);body['preparation']=reply['result']['preparation'];body['request']=reply['result']['request']
    body['source_stop']['request_sha256']=v3.digest(body['request'])
    body['source_stop']['audit_started_at']='2026-10-12T02:00:21Z'
    body['source_stop']['verified_at']='2026-10-12T02:00:22Z'
    body['issued_at']='2026-10-12T02:00:23Z';body['install_before']='2026-10-12T10:00:23Z'
    # Shape-only synthetic envelope; original activation worker must verify crypto.
    raw=envelope(v3,body)
    monkeypatch.setattr(m,'datetime',SimpleNamespace(now=lambda tz:datetime(2026,10,12,2,0,30,tzinfo=timezone.utc)))
    with pytest.raises(ValueError):m.activation_plan(t.s.plans['update']['attempt_id'],raw)
    assert not (t.s.slot/'authorization.json').exists()
    result=m.activation_plan(t.s.plans['update']['attempt_id'],raw,
        factory_cutover_authorized=True,source_isolation_confirmed=True)
    assert result['started'] is result['factory_activated'] is False
    assert t.calls==['update','prepare'] and not (t.s.base/'factory-activation.json').exists()

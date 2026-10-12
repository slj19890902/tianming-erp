"""Full synthetic target audit with real stream approval and large state reads.

All archives, SQL, credentials and root records are temporary synthetic inputs.
Root/UID ownership and system metadata use explicit adapters; stream selection,
signed tree verification, binding rechecks, GCM and fact comparison run normally.
No network, real NAS credential, service control or production path is accessed.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from desktop_assistant import factory_target_audit as audit, migration
from desktop_assistant import mac_service_manage as manage, mac_smb_stream_target as stream
from desktop_assistant import mac_smb_stream_runtime as runtime, mac_smb_stream_contract as archive
from desktop_assistant import mac_smb_stream_runtime_contract as cfg
from desktop_assistant.storage import read_json, sha, write_json
from tests.desktop_assistant.test_factory_target_audit import (
    target, case, recovery_case, host_store, protected_facts, rewrite,
)

pytestmark = [pytest.mark.skipif(sys.platform != 'darwin', reason='Mac target protected FD contract'),
              pytest.mark.parametrize('target', ['stream'], indirect=True)]


@pytest.fixture
def streamed(target, monkeypatch):
    t = target; x = t.x
    for name, path in [('BASE', x.base), ('CONTROL', x.base/'smb-stream-service.json'),
                       ('CONTRACT', x.base/'smb-stream-archive.json')]:
        monkeypatch.setattr(cfg, name, path)
    value = dict(type=archive.TYPE, context_sha256=sha(x.base/'keychain-context.json'),
        uid=499, server_address='192.168.1.245', port=445, username='synthetic_admin',
        share='synthetic_share', relative_directory=archive.DIRECTORY, server_guid='b'*32,
        protection='smb311-session-aes-gcm')
    control = dict(type=cfg.TYPE, attempt_id='d'*32, plan_sha256='e'*64, package_sha256=x.id,
        context_sha256=value['context_sha256'], publisher_sha256=cfg.digest(t.c.public),
        contract_sha256=archive.digest(value))
    control['credential_account'] = cfg.account(value['context_sha256'], control['contract_sha256'], 'd'*32)
    for path, data in ((cfg.CONTROL, control), (cfg.CONTRACT, value)):
        path.write_bytes(cfg.canonical(data)); path.chmod(0o644)
    selected = str(stream.destination(control))
    rewrite(x.root/'preferences.json', lambda v: v.update(nas=selected))
    # Actual protected-FD reader under the parent fixture's ownership adapter.
    monkeypatch.setattr(runtime.publication, 'read_record', runtime.published._read)
    def metadata(package):
        tree, _ = runtime.published.resolve(x.root, package, t.c.public, kind='migration')
        return dict(package=package, context=deepcopy(x.context),
            context_sha256=sha(x.base/'keychain-context.json'), publisher_sha256=cfg.digest(t.c.public),
            registry_sha256=sha(runtime.published.REGISTRY), public=t.c.public, tree=tree)
    monkeypatch.setattr(runtime.service, 'metadata', metadata)
    monkeypatch.setattr(stream, '__file__', str(x.slot/'migration'/stream.MODULE))
    monkeypatch.setattr(runtime.mounts, 'local_addresses', lambda: {'127.0.0.1'})
    calls = []
    check = runtime.Binding.check
    def observed(binding):
        calls.append('check'); return check(binding)
    monkeypatch.setattr(runtime.Binding, 'check', observed)
    def forbidden(*args, **kwargs):
        raise AssertionError('audit must not use NAS transport, NAS secrets, or mounted fallback')
    monkeypatch.setattr(audit.remote, '_contract', forbidden)
    monkeypatch.setattr(audit.remote, 'prepare_destination', forbidden)
    for module, names in ((runtime, ('preflight', 'transfer', '_dispatch', '_credential')),
                          (stream, ('preflight',)), (runtime.session, ('open_share',)),
                          (runtime.keychain, ('ServiceStore',))):
        for name in names: monkeypatch.setattr(module, name, forbidden)
    return SimpleNamespace(t=t, x=x, value=value, control=control, destination=selected, checks=calls)


def test_complete_stream_audit_preserves_facts_without_nas_access(streamed):
    s=streamed; before=protected_facts(s.x); secrets=dict(s.t.case.secrets)
    root_records={p:p.read_bytes() for p in (cfg.CONTROL, cfg.CONTRACT, runtime.published.REGISTRY)}
    with s.t.run() as (evidence,recheck):
        assert evidence['credentials_verified'] and not evidence['factory_activated']
        assert evidence['backup_sha256']==s.t.evidence['backup_sha256']
        assert not evidence['network_reachability_verified']
        recheck()
    assert len(s.checks)>=4
    assert protected_facts(s.x)==before and s.t.case.secrets==secrets
    assert all(path.read_bytes()==raw for path,raw in root_records.items())


@pytest.mark.parametrize('fault', ['token-short', 'token-other', 'contract-context',
                                  'control-context', 'unapproved-package', 'missing-control', 'missing-contract'])
def test_stream_failure_never_uses_mounted_fallback_or_decrypts(streamed, monkeypatch, fault):
    s=streamed
    if fault.startswith('token-'):
        destination=stream.PREFIX+('bad' if fault=='token-short' else 'f'*64)
        rewrite(s.x.root/'preferences.json', lambda v:v.update(nas=destination))
    elif fault=='contract-context':
        rewrite(cfg.CONTRACT, lambda v:v.update(context_sha256='f'*64))
    elif fault=='control-context':rewrite(cfg.CONTROL, lambda v:v.update(context_sha256='f'*64))
    elif fault=='unapproved-package':rewrite(cfg.CONTROL, lambda v:v.update(package_sha256='f'*64))
    else:(cfg.CONTROL if fault=='missing-control' else cfg.CONTRACT).unlink()
    def forbidden(*args, **kwargs):raise AssertionError('invalid archive must fail before backup authentication')
    monkeypatch.setattr(audit, 'decrypt_file', forbidden)
    before=protected_facts(s.x)
    with pytest.raises(ValueError):
        with s.t.run():pytest.fail('unapproved stream reached activation consumer')
    assert protected_facts(s.x)==before
    assert not list((s.x.root/'staging').glob('factory-audit-*'))


def test_audit_rejects_binding_context_different_from_held_restore_context(streamed, monkeypatch):
    s=streamed; binding=stream.prepare(s.destination)
    binding.context={**binding.context, 'installation_id':'f'*32}
    # Deliberately mismatched boundary adapter checks the audit's own comparison.
    binding.check=lambda:None
    monkeypatch.setattr(stream, 'prepare', lambda path:binding)
    with pytest.raises(ValueError):
        with s.t.run():pytest.fail('different archive context accepted')
    assert not list((s.x.root/'staging').glob('factory-audit-*'))


@pytest.mark.parametrize('fault', ['contract-bytes', 'control-bytes', 'control-plan', 'preferences'])
@pytest.mark.parametrize('explicit', [False, True], ids=['context-exit', 'callback'])
def test_stream_original_binding_rechecked_after_yield(streamed, fault, explicit):
    s=streamed
    with pytest.raises(ValueError):
        with s.t.run() as (evidence,recheck):
            if fault=='preferences':rewrite(s.x.root/'preferences.json',lambda v:v.update(nas='other'))
            elif fault=='control-plan':
                value=read_json(cfg.CONTROL);value['plan_sha256']='f'*64
                cfg.CONTROL.write_bytes(cfg.canonical(value))
            else:
                path=cfg.CONTRACT if fault=='contract-bytes' else cfg.CONTROL
                path.write_bytes(path.read_bytes()+b' ')
            if explicit:recheck()
    assert Path(evidence['retained_workspace']).exists()


def test_late_stream_approval_change_during_credential_comparison_refuses_before_yield(streamed, monkeypatch):
    s=streamed; collect=audit.credentials.collect
    def changed(*args, **kwargs):
        result=collect(*args, **kwargs)
        cfg.CONTROL.write_bytes(cfg.CONTROL.read_bytes()+b' ')
        return result
    monkeypatch.setattr(audit.credentials, 'collect', changed)
    with pytest.raises(ValueError):
        with s.t.run():pytest.fail('changed archive binding reached activation consumer')
    assert list((s.x.root/'staging').glob('factory-audit-*'))


def test_large_2991_item_history_survives_complete_stream_audit(streamed):
    s=streamed; data=s.t.c.root/'synthetic-history'; data.mkdir()
    for index in range(2991):
        (data/f'preserved-{index:05d}.pdf').write_bytes(b'synthetic historical attachment')
    inventory=migration.files(data)
    rewrite(s.x.root/'state.json', lambda v:v.update(migration_files=inventory,
        historical_metadata={'preserve':['unchanged',2991]}))
    path=s.x.root/'state.json'; before=path.read_bytes()
    assert len(inventory)==2991 and len(before)>65536
    with s.t.run() as (evidence,recheck):
        recheck(); assert evidence['credentials_verified']
        assert 'migration_files' not in json.dumps(evidence)
    assert path.read_bytes()==before and read_json(path)['migration_files']==inventory
    assert migration.files(data)==inventory


@pytest.mark.parametrize('fault', ['duplicate-root', 'duplicate-nested', 'nan', 'infinity', 'overflow'])
def test_strict_state_rejected_before_archive_or_secret_access(streamed, monkeypatch, fault):
    s=streamed; path=s.x.root/'state.json'; raw=path.read_bytes().rstrip()
    extra={'duplicate-root':b'"manual_stop":true', 'duplicate-nested':b'"nested":{"same":1,"same":2}',
           'nan':b'"nonfinite":NaN', 'infinity':b'"nonfinite":Infinity',
           'overflow':b'"nonfinite":1e999'}[fault]
    invalid=raw[:-1]+b','+extra+b'}';path.write_bytes(invalid)
    def forbidden(*args,**kwargs):raise AssertionError('bad state reached archive approval or backup secret')
    monkeypatch.setattr(stream,'prepare',forbidden)
    monkeypatch.setattr(audit.credential_store,'unprotect_backup',forbidden)
    with pytest.raises(ValueError):
        with s.t.run():pytest.fail('invalid state accepted')
    assert path.read_bytes()==invalid


def test_oversized_state_refused_before_read_and_preserved(streamed, monkeypatch):
    s=streamed; path=s.x.root/'state.json'
    with path.open('r+b') as file:file.truncate(manage.STATE_LIMIT+1)
    inode=path.stat().st_ino; original=os.read
    def guarded(fd,size):
        assert os.fstat(fd).st_ino!=inode, 'oversized state must be rejected before reading'
        return original(fd,size)
    monkeypatch.setattr(audit.restore.os,'read',guarded)
    with pytest.raises(ValueError, match='超限'):
        with s.t.run():pytest.fail('oversized state accepted')
    assert path.stat().st_size==manage.STATE_LIMIT+1


@pytest.mark.parametrize('record', ['preferences', 'request', 'receipt', 'authorization'])
def test_other_records_keep_64k_bound(streamed, record):
    s=streamed; x=s.x
    paths={'preferences':x.root/'preferences.json',
           'request':x.base/('factory-target-'+s.t.request['request_id']+'.json'),
           'receipt':x.home/('service-restore-'+x.args.input_attempt+'.json'),
           'authorization':x.attempt/'authorization.json'}
    path=paths[record]; mode=path.stat().st_mode & 0o777
    raw=path.read_bytes()+b' '*65536;path.chmod(0o600);path.write_bytes(raw);path.chmod(mode)
    assert len(raw)>65536
    with pytest.raises(ValueError, match='超限'):
        with s.t.run():pytest.fail('small record oversized input accepted')
    assert path.read_bytes()==raw

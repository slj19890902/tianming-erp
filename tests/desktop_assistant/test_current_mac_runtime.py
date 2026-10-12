"""Temporary signed v3 records/packages; root, launchd and keychain are adapters."""
import sys

import pytest

if sys.platform != 'darwin':
    pytest.skip('Mac protected runtime and FD ownership integration', allow_module_level=True)

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json
import os
import shutil
from unittest.mock import Mock
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import current_mac_cutover_contract as current, current_mac_cutover as codec
from desktop_assistant import restore_provenance as history
from desktop_assistant.storage import read_json, write_json, sha, pack_tree
from tests.desktop_assistant.test_current_mac_cutover_contract import valid_body
from tests.desktop_assistant.test_mac_factory_runtime import (
    runtime, published, contract, protocol, boot, system, Manager,
    activated, root_case, target, case, recovery_case, host_store,
    signed_activation_protocol, native_fixture, execution_protocol,
    approve_update, admit, environment)

CAPABILITY = 'factory_current_mac_execution_v3'
V3_FILES = {'desktop_assistant/mac_published_runtime.py',
    'desktop_assistant/current_mac_cutover.py', 'desktop_assistant/current_mac_cutover_contract.py',
    'desktop_assistant/restore_provenance.py', 'desktop_assistant/release_request.py',
    'desktop_assistant/current_mac_publication.py', 'desktop_assistant/current_mac_worker_contract.py'}


@pytest.fixture(autouse=True)
def current_protocol(execution_protocol, monkeypatch):
    previous = native_fixture.pack_tree
    def pack(source, destination, meta, key):
        meta = deepcopy(meta)
        if meta.get('runtime_platform') == 'macos-arm64':
            meta['runtime_capabilities'][CAPABILITY] = 1
        return previous(source, destination, meta, key)
    monkeypatch.setattr(native_fixture, 'pack_tree', pack)
    monkeypatch.setattr(native_fixture.publication, 'SERVICE_RESTORE_FILES',
        native_fixture.publication.SERVICE_RESTORE_FILES | V3_FILES)


def publish_record(c):
    request = c.body['request']; prep = c.body['preparation']
    request['preparation_sha256'] = current.digest(prep)
    c.body['source_stop']['request_sha256'] = current.digest(request)
    raw = codec.sign_authorization(request, c.body['source_stop'], prep, c.t.c.key,
        now=current._time(c.body['issued_at']))
    value = current.make_record(request, c.network, raw,
        activated_at=current._time(c.body['issued_at']),
        operator_attestation={key: True for key in current.ATTESTATION})
    for name, value in ((contract.NAME, value),
            ('factory-target-'+request['request_id']+'.json', request),
            ('factory-network-'+request['request_id']+'.json', c.network)):
        path = c.x.base/name; path.write_bytes(current.canonical(value)); path.chmod(0o644)
    from desktop_assistant import current_mac_publication as gate
    claim = dict(type=gate.TYPE,attempt_id='a'*32,plan_sha256='b'*64,
        activation_sha256=sha(c.x.base/contract.NAME),request_sha256=current.digest(request),ready=True)
    path = c.x.base/gate.NAME;path.write_bytes(current.canonical(claim));path.chmod(0o644)


@pytest.fixture
def current_case(activated, monkeypatch):
    c = activated
    monkeypatch.setattr(current, 'ROOT', str(c.x.root))
    monkeypatch.setattr(system, 'PLISTS', c.x.base)
    monkeypatch.setattr(boot, 'protected', lambda path, *, directory=False, **kw:
                        None if directory else Path(path).read_bytes())
    c.body = valid_body(current); c.network = read_json(c.x.base/contract.NAME)['network']
    state = c.manager.state; prep = c.body['preparation']
    prep.update(target=deepcopy(c.t.request['target']), publisher_sha256=c.t.request['publisher_sha256'],
        context_sha256=sha(c.x.base/'keychain-context.json'), registry_sha256=sha(published.REGISTRY),
        state_sha256=sha(c.x.root/'state.json'), execution_package_sha256=c.x.id, monitor_package_sha256=c.x.id)
    prep['restore'].update(backup_sha256=state['restored_backup_sha256'],
        backup_created_at=current.normalize_timestamp(state['source_time']), restore_package_sha256=c.x.id,
        native_binding_sha256=current.digest(state['native_restore_binding']),
        restored_release_sha256=state[history.RELEASE], restored_schema_authority_sha256=state[history.AUTHORITY])
    prep['target_snapshot']['revision'] = state['native_restore_binding']['database_revision']
    # The restore fixture is created now; keep all public times ordered without
    # altering its original fractional timestamp or touching a real clock.
    now = contract._now().replace(microsecond=0) + timedelta(seconds=2)
    prep['backup'].update(created_at=current._stamp(now), verified_at=current._stamp(now))
    prep['prepared_at'] = current._stamp(now)
    c.body['request'].update(target=deepcopy(prep['target']), publisher_sha256=prep['publisher_sha256'],
        network_sha256=current.digest(c.network), execution_package_sha256=c.x.id, monitor_package_sha256=c.x.id,
        created_at=current._stamp(now), expires_at=current._stamp(now+timedelta(days=7)))
    c.body['source_stop'].update(audit_started_at=current._stamp(now), verified_at=current._stamp(now))
    c.body.update(issued_at=current._stamp(now), install_before=current._stamp(now+timedelta(hours=8)))
    monkeypatch.setattr(contract, '_now', lambda: now+timedelta(seconds=1))
    publish_record(c)
    return c


def test_v3_configuration_accepts_signed_current_mac_without_comparing_source_business(current_case):
    c = current_case; state_raw = (c.x.root/'state.json').read_bytes()
    result = runtime.configuration(c.x.root, c.x.id, c.t.c.public)
    assert result == {'network': c.network, 'activation_sha256': sha(c.x.base/contract.NAME),
        'package_sha256': c.x.id, 'request_id': c.body['request']['request_id']}
    assert (c.x.root/'state.json').read_bytes() == state_raw
    assert c.body['source_stop']['database_sha256'] != c.body['preparation']['target_snapshot']['database_sha256']


@pytest.mark.parametrize('fault',['missing','unfinished','wrong_activation','wrong_request','failure',
    'partial_failure','no_activation','completion_symlink'])
def test_v3_refuses_unconfirmed_root_publication(current_case,fault):
    from desktop_assistant import current_mac_publication as gate
    c=current_case;path=c.x.base/gate.NAME
    if fault=='missing':path.unlink()
    elif fault=='no_activation':(c.x.base/contract.NAME).unlink()
    elif fault=='completion_symlink':
        raw=path.read_bytes();other=c.x.base/'other-completion.json';other.write_bytes(raw);other.chmod(0o644)
        path.unlink();path.symlink_to(other)
    elif fault in ('failure','partial_failure'):
        failed=c.x.base/gate.FAILED;failed.write_bytes(b'{}' if fault=='failure' else b'')
        failed.chmod(0o644)
    else:
        value=read_json(path)
        if fault=='unfinished':value['ready']=False
        elif fault=='wrong_activation':value['activation_sha256']='0'*64
        else:value['request_sha256']='0'*64
        path.write_bytes(current.canonical(value))
    with pytest.raises((ValueError,OSError)):runtime.configuration(c.x.root,c.x.id,c.t.c.public)
    assert runtime.configured_mode(c.manager)=='attention'


def change(path, update):
    value = read_json(path); update(value); write_json(path, value)
    path.chmod(0o600 if path.name == 'state.json' else 0o644)


def service(c, package=None):
    identity = package or c.x.id
    meta = {'package':identity, 'context_sha256':sha(c.x.base/'keychain-context.json'),
            'publisher_sha256':sha(published.PUBLIC)}
    system.CONTROL.write_bytes(boot.canonical(system.record_for(meta, True))); system.CONTROL.chmod(0o644)
    system.PLIST.write_bytes(system._plist(identity, meta['publisher_sha256'])); system.PLIST.chmod(0o644)


def test_startup_authenticates_complete_history_and_current_monitor(current_case, monkeypatch):
    from desktop_assistant import release_request
    c = current_case; service(c)
    original = release_request.inspect_package
    inspected = []
    def inspect(path, public):
        inspected.append(Path(path)); return original(path, public)
    monkeypatch.setattr(release_request, 'inspect_package', inspect)
    # restore_provenance keeps its imported function; bind the same real verifier.
    monkeypatch.setattr(history, 'inspect_package', inspect)
    before = (c.x.root/'state.json').read_bytes()
    runtime.require_factory_service()
    wanted = {c.x.id, *[v for v in history.identities(c.manager.state).values() if v is not None]}
    assert {p.stem for p in inspected} == wanted
    assert (c.x.root/'state.json').read_bytes() == before


def test_status_never_scans_history_or_reads_credentials(current_case, monkeypatch):
    c = current_case
    def forbidden(*args, **kw): raise AssertionError('startup-only or secret IO')
    monkeypatch.setattr(history, 'verify', forbidden)
    monkeypatch.setattr('desktop_assistant.release_request.inspect_package', forbidden)
    monkeypatch.setattr('desktop_assistant.ai_config.load_openai_api_key', forbidden)
    monkeypatch.setattr('desktop_assistant.ai_config.load_deepseek_api_key', forbidden)
    monkeypatch.setattr(system, 'verify_keychain', forbidden)
    for _ in range(3):
        assert runtime.configured_mode(c.manager) == 'factory'


def test_business_and_backup_management_may_advance_after_install_window(current_case, monkeypatch):
    c = current_case
    change(c.x.root/'state.json', lambda v:v.update(manual_stop=False, operation='backup',
        last_backup='later.tmbackup', last_backup_stream={'later':'complete'}, backup_error=None,
        previous='e'*64))
    (c.x.root/'shared/new-business-attachment.txt').write_text('synthetic later business')
    monkeypatch.setattr(contract, '_now', lambda:current._time(c.body['issued_at'])+timedelta(days=500))
    assert runtime.configuration(c.x.root, c.x.id, c.t.c.public)['network'] == c.network


@pytest.mark.parametrize('fault', ['current','backup','binding-hash','binding-type','binding-package',
    'binding-backup','binding-context','binding-schema','binding-factory','history-release','history-authority',
    'missing-history','source-time'])
def test_original_restore_state_is_fixed(current_case, fault):
    c = current_case
    def mutate(v):
        if fault == 'current': v['current'] = 'f'*64
        elif fault == 'backup': v['restored_backup_sha256'] = 'f'*64
        elif fault == 'history-release': v[history.RELEASE] = 'f'*64
        elif fault == 'history-authority': v[history.AUTHORITY] = None
        elif fault == 'missing-history': v.pop(history.RELEASE)
        elif fault == 'source-time': v['source_time'] = '2020-01-01T00:00:00Z'
        else:
            key = {'binding-hash':'database_revision','binding-type':'type','binding-package':'package_sha256',
                'binding-backup':'backup_sha256','binding-context':'context_sha256',
                'binding-schema':'schema_migrated','binding-factory':'factory_cutover'}[fault]
            v['native_restore_binding'][key] = True if key in {'schema_migrated','factory_cutover'} else 'f'*64
    change(c.x.root/'state.json', mutate)
    with pytest.raises(ValueError): runtime.configuration(c.x.root, c.x.id, c.t.c.public)
    assert runtime.configured_mode(c.manager) == 'attention'


@pytest.mark.parametrize('fault', ['unknown','v2','signature','publisher','future','extra','duplicate','nan',
    'context','request','network','registry-context','registry-original','state-nan'])
def test_invalid_v3_never_falls_back_to_home(current_case, fault):
    c = current_case; path = c.x.base/contract.NAME
    if fault in {'unknown','v2','extra','signature','publisher','future'}:
        def mutate(v):
            if fault == 'unknown': v['type'] = 'unknown'
            elif fault == 'v2': v['type'] = 'tianming.factory-retained.activation.v2'
            elif fault == 'extra': v['extra'] = False
            elif fault == 'publisher': v['request']['publisher_sha256'] = 'f'*64
            elif fault == 'future': v['activated_at'] = current._stamp(current._time(c.body['issued_at'])+timedelta(hours=1))
            else:
                envelope = current.decode(v['authorization'].encode())
                envelope['body']['source_stop']['database_sha256'] = 'f'*64
                v['authorization'] = current.canonical(envelope).decode()
                v['authorization_sha256'] = boot.digest(v['authorization'].encode())
        change(path, mutate)
    elif fault in {'duplicate','nan'}:
        raw = path.read_bytes(); path.write_bytes(raw[:-1]+(b',"type":"duplicate"}' if fault=='duplicate' else b',"unknown":NaN}'))
    elif fault == 'context': change(c.x.base/'keychain-context.json', lambda v:v.update(installation_id='f'*32))
    elif fault == 'request': change(c.x.base/('factory-target-'+c.body['request']['request_id']+'.json'), lambda v:v.update(request_id='f'*32))
    elif fault == 'network': change(c.x.base/('factory-network-'+c.body['request']['request_id']+'.json'), lambda v:v.update(ERP_PORT='8001'))
    elif fault.startswith('registry-'):
        change(published.REGISTRY, lambda v:v.update(**({'context_sha256':'f'*64} if fault=='registry-context' else {'packages':['f'*64]})))
    else:
        p = c.x.root/'state.json'; p.write_bytes(p.read_bytes()[:-2]+b', "x":NaN}')
    with pytest.raises((ValueError, TypeError, InvalidSignature)): runtime.configuration(c.x.root, c.x.id, c.t.c.public)
    assert runtime.configured_mode(c.manager) == 'attention'


def test_later_execution_and_monitor_keep_original_restore_and_activation(current_case):
    c = current_case; state = c.manager.state
    raw = (c.x.base/contract.NAME).read_bytes(); identity, slot = approve_update(c)
    service(c, identity)
    for kind in ('server','migration'):
        assert published.resolve(c.x.root, identity, c.t.c.public, kind=kind)[0] == slot/kind
    runtime.require_factory_service()
    assert runtime.configuration(c.x.root, identity, c.t.c.public)['package_sha256'] == identity
    assert c.manager.state['native_restore_binding'] == state['native_restore_binding']
    assert (c.x.base/contract.NAME).read_bytes() == raw


@pytest.mark.parametrize('value', [None, False, True, 0, 2, '1'])
def test_new_execution_capability_is_exact(current_case, value):
    c = current_case; manifest = deepcopy(c.x.manifest)
    manifest['runtime_capabilities'][CAPABILITY] = value
    with pytest.raises(ValueError):
        runtime.require_execution(manifest, activation_raw=(c.x.base/contract.NAME).read_bytes())
    # Without supplied v3 authority, the old v1 call retains its old meaning.
    runtime.require_execution(manifest)


@pytest.mark.parametrize('missing', sorted(V3_FILES))
def test_v3_requires_its_complete_protocol_files(current_case, missing):
    c = current_case; manifest = deepcopy(c.x.manifest); manifest['files'].pop(missing)
    with pytest.raises(ValueError):
        runtime.require_execution(manifest, activation_raw=(c.x.base/contract.NAME).read_bytes())


@pytest.mark.parametrize('role', ['native','release','authority'])
@pytest.mark.parametrize('fault', ['missing','modified','mode','symlink','hardlink'])
def test_startup_refuses_untrusted_history_before_operational_credentials(current_case, monkeypatch, role, fault):
    c = current_case; service(c)
    identity = {'native':c.x.id,'release':c.manager.state[history.RELEASE],
                'authority':c.manager.state[history.AUTHORITY]}[role]
    assert identity is not None
    path = c.x.root/'packages'/(identity+'.zip')
    if fault == 'missing': path.rename(path.with_suffix('.retained'))
    elif fault == 'modified':
        with path.open('ab') as output: output.write(b'changed archive')
    elif fault == 'mode': path.chmod(0o644)
    elif fault == 'symlink':
        original = path.with_suffix('.retained'); path.rename(original); path.symlink_to(original)
    else: os.link(path, path.with_suffix('.retained'))
    first = Mock(side_effect=AssertionError('operational key read'))
    second = Mock(side_effect=AssertionError('operational key read'))
    monkeypatch.setattr('desktop_assistant.ai_config.load_openai_api_key', first)
    monkeypatch.setattr('desktop_assistant.ai_config.load_deepseek_api_key', second)
    reached = Mock(wraps=runtime._current_mac_history)
    monkeypatch.setattr(runtime, '_current_mac_history', reached)
    with pytest.raises((ValueError, OSError)):
        c.manager._environment(c.x.slot/'server')
    assert reached.call_count == 1 and not first.called and not second.called


@pytest.mark.parametrize('fault', ['disabled','wrong-control','wrong-plist','bad-monitor-tree','bad-monitor-package'])
def test_startup_binds_actual_monitor_without_freezing_historical_control(current_case, fault):
    c = current_case; service(c)
    if fault == 'disabled': change(system.CONTROL, lambda v:v.update(enabled=False))
    elif fault == 'wrong-control': change(system.CONTROL, lambda v:v.update(context_sha256='f'*64))
    elif fault == 'wrong-plist': system.PLIST.write_bytes(b'not the fixed monitor')
    elif fault == 'bad-monitor-tree': (c.x.slot/'migration'/runtime.MODULE).write_bytes(b'changed')
    else:
        with (c.x.slot/'package.zip').open('ab') as output: output.write(b'changed')
    with pytest.raises((ValueError, OSError)): runtime.require_factory_service()


def test_direct_v3_server_uses_original_admission_latch(current_case, monkeypatch):
    c = current_case; service(c)
    for key, value in environment(c).items(): monkeypatch.setenv(key, value)
    admit(c)
    runtime.require_application(c.x.slot/'server/app/main.py')
    assert runtime._admitted['factory'] is True
    monkeypatch.setenv('ERP_FACTORY_PACKAGE_SHA256', 'f'*64)
    with pytest.raises(ValueError): runtime.require_admitted_factory()


@pytest.mark.parametrize('fault', ['native','history','state','control','request'])
def test_late_change_cannot_escape_held_leases(current_case, monkeypatch, fault):
    c = current_case; service(c); original = history.verify
    def verify(values, paths, public):
        original(values, paths, public)
        if fault in {'native','history'}:
            identity = c.x.id if fault == 'native' else c.manager.state[history.RELEASE]
            with (c.x.root/'packages'/(identity+'.zip')).open('ab') as output: output.write(b'late-change')
        elif fault == 'state': change(c.x.root/'state.json', lambda v:v.update(last_backup='changed'))
        elif fault == 'control': change(system.CONTROL, lambda v:v.update(enabled=False))
        else:
            path = c.x.base/('factory-target-'+c.body['request']['request_id']+'.json')
            # Same JSON value still replaces the inode held by the authority lease.
            replacement = path.with_suffix('.late'); replacement.write_bytes(path.read_bytes()); replacement.chmod(0o644)
            replacement.replace(path)
    monkeypatch.setattr(history, 'verify', verify)
    with pytest.raises((ValueError, OSError)): runtime.require_factory_service()


def test_native_history_hash_is_not_a_substitute_for_original_signature(current_case):
    c = current_case; service(c)
    candidate = c.t.c.root/'wrong-publisher-native.zip'
    meta = deepcopy(c.x.manifest); meta.pop('files')
    pack_tree(c.t.c.root/'native-source-0', candidate, meta, Ed25519PrivateKey.generate())
    identity = sha(candidate); destination = c.x.root/'packages'/(identity+'.zip')
    shutil.copyfile(candidate, destination); destination.chmod(0o600)
    change(published.REGISTRY, lambda v:v.update(packages=sorted([*v['packages'], identity])))
    change(c.x.root/'state.json', lambda v:v['native_restore_binding'].update(package_sha256=identity))
    c.body['preparation']['restore'].update(restore_package_sha256=identity,
        native_binding_sha256=current.digest(c.manager.state['native_restore_binding']))
    publish_record(c)
    with pytest.raises(InvalidSignature): runtime.require_factory_service()


def test_current_monitor_requires_v3_even_when_execution_is_unchanged(current_case):
    c = current_case; old_state = c.manager.state
    c.x.manifest['runtime_capabilities'].pop(CAPABILITY)
    monitor, _ = approve_update(c)
    write_json(c.x.root/'state.json', old_state); (c.x.root/'state.json').chmod(0o600)
    service(c, monitor)
    assert runtime.configuration(c.x.root, c.x.id, c.t.c.public)['package_sha256'] == c.x.id
    with pytest.raises(ValueError, match='当前Mac业务'): runtime.require_factory_service()


def test_history_authority_absence_is_explicit_and_supported(current_case):
    c = current_case; service(c)
    change(c.x.root/'state.json', lambda v:v.update({history.AUTHORITY:None}))
    c.body['preparation']['restore'][history.AUTHORITY] = None
    publish_record(c)
    runtime.require_factory_service()


def test_complete_management_history_does_not_use_public_64k_limit(current_case):
    c = current_case
    change(c.x.root/'state.json', lambda v:v.update(backup_history=['synthetic-history']*10000))
    assert (c.x.root/'state.json').stat().st_size > current.LIMIT
    assert runtime.configured_mode(c.manager) == 'factory'


def test_original_contract_and_record_modules_remain_unchanged():
    # Source scope is independently reviewed against Git; this asserts the old
    # capability call remains valid without any v3 claim or runtime record IO.
    from tests.desktop_assistant.test_mac_factory_activation import caps
    manifest = caps(); manifest['files'].update({name:'a'*64 for name in runtime.EXECUTION_FILES})
    runtime.require_execution(manifest)

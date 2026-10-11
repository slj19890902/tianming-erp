"""No root or real NAS: explicit metadata/identity adapters around actual temp IO.

The OS ownership of BASE and the NFS mount are simulated. The service-owned
local directory modes, no-ACL checks, F_NOCACHE, cryptography and bytes are real.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.core import mac_service_keychain as keychain
from desktop_assistant import mac_backup_storage as s, mac_mount, mac_release_publish as publication
from desktop_assistant.storage import write_json

REAL_CHILD = s._child

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac native FD/ACL/F_NOCACHE tests')


@pytest.fixture
def storage(tmp_path, monkeypatch):
    base = tmp_path.resolve()/'system'; base.mkdir(mode=0o755)
    root = base/'instance'; root.mkdir(mode=0o700); (root/'control').mkdir(mode=0o700)
    mount = base/'archive'; mount.mkdir(); destination = mount/'backups'; destination.mkdir(mode=0o700)
    for key, value in [('BASE', base), ('MOUNT', mount), ('DESTINATION', destination), ('CONTRACT', base/'archive-contract.json')]:
        monkeypatch.setattr(s, key, value)
    monkeypatch.setattr(s, 'required', lambda: True)
    context_raw = b'{"synthetic_dedicated_uid":true}'
    context = {'uid': os.getuid()}
    contract = dict(type=s.CONTRACT_TYPE, context_sha256=s.digest(context_raw), mountpoint=str(mount),
        destination=str(destination), source='nas.example.invalid:/ERP-Archive', server_address='192.0.2.20',
        export='/ERP-Archive', version='4.0', port=2049, security='sys')
    records = {keychain.CONTEXT: context_raw, s.CONTRACT: s.canonical(contract)}
    monkeypatch.setattr(keychain, 'load_context', lambda: context)
    monkeypatch.setattr(publication, 'read_record', lambda path: records[path])
    real_checked = keychain._checked_path
    @contextmanager
    def root_owner_adapter(path, **kw):
        if path == base:
            assert kw == dict(uid=0, directory=True, mode=0o755)
            kw['uid'] = os.getuid()  # Explicit simulation of root-owned BASE only.
        with real_checked(path, **kw) as fd: yield fd
    monkeypatch.setattr(keychain, '_checked_path', root_owner_adapter)
    kernel = dict(fsid=[19, 31], filesystem='nfs', mountpoint=str(mount), source=contract['source'],
                  owner=0, flags=0x4000018, subtype=0)
    observation = dict(classification='direct_network_unbound', kernel=kernel,
        remote=dict(addresses=['192.0.2.20'], export='/ERP-Archive', server='nas.example.invalid',
                    version='4.0', port=2049, security='sys', tcp=True))
    monkeypatch.setattr(mac_mount, 'observe', lambda path: deepcopy(observation))
    monkeypatch.setattr(mac_mount, 'fd_identity', lambda fd: deepcopy(kernel))
    monkeypatch.setattr(mac_mount, 'local_addresses', lambda: {'127.0.0.1', '192.0.2.10'})
    monkeypatch.setattr(s, '_child', s.readback)  # Worker protocol tested separately below.
    final = destination/'20261011-120000-ab123456.tmbackup'
    final.write_bytes(b'fixture-backup\x00'*81)
    receipts = root/'control/backup-receipts'
    receipt_path = receipts/(final.stem+'.json')
    def verified():
        expected = s.digest(final.read_bytes())
        proof = s.after_write(s.before_write(destination), final, expected, final.stat().st_size)
        receipt = dict(path=str(final), sha256=expected, size=final.stat().st_size,
                       verified=True, storage='nas', database={'revision': 'synthetic'})
        return proof, receipt
    def published():
        proof, receipt = verified(); at = s.publish_receipt(receipt_path, receipt, proof)
        return {'last_backup': str(final), 'last_backup_at': at}, proof, receipt
    return SimpleNamespace(base=base, root=root, destination=destination, mount=mount, final=final,
        context=context, contract=contract, records=records, kernel=kernel, observation=observation,
        receipts=receipts, receipt_path=receipt_path, verified=verified, published=published)


def test_full_synthetic_readback_receipt_and_local_fast_path(storage, monkeypatch):
    storage.final.write_bytes(os.urandom(2*1024*1024+17))
    state, proof, receipt = storage.published()
    assert set(receipt) == s.RECEIPT_FIELDS and proof['sha256'] == hashlib.sha256(storage.final.read_bytes()).hexdigest()
    assert (storage.receipts.stat().st_mode & 0o777) == 0o700
    for path in storage.receipts.iterdir(): assert path.stat().st_mode & 0o777 == 0o600
    # Historical status must never inspect NAS, not even stat/open of final.
    storage.final.unlink()
    monkeypatch.setattr(mac_mount, 'observe', lambda *a: pytest.fail('status queried NAS'))
    monkeypatch.setattr(mac_mount, 'fd_identity', lambda *a: pytest.fail('status queried NAS FD'))
    monkeypatch.setattr(s, '_child', lambda *a: pytest.fail('status read whole backup'))
    assert s.verified_at(storage.root, state) == proof['verified_at']


@pytest.mark.parametrize('kind', ['proxy', 'local', 'unknown', 'unbound-address', 'self', 'owner', 'fd', 'permissions', 'link', 'changed'])
def test_unsafe_storage_rejected_before_any_probe(storage, monkeypatch, kind):
    before = set(storage.destination.iterdir())
    if kind in ('proxy', 'local', 'unknown'):
        storage.observation['classification'] = {'proxy':'loopback_proxy', 'local':'local_or_unmounted', 'unknown':'unknown'}[kind]
    if kind == 'unbound-address': storage.observation['remote']['addresses'] = ['192.0.2.21']
    if kind == 'self': monkeypatch.setattr(mac_mount, 'local_addresses', lambda: {'192.0.2.20'})
    if kind == 'owner': storage.kernel['owner'] = os.getuid()
    if kind == 'fd': monkeypatch.setattr(mac_mount, 'fd_identity', lambda fd: {**storage.kernel, 'fsid':[22, 31]})
    if kind == 'permissions': storage.destination.chmod(0o755)
    if kind == 'link':
        storage.destination.rename(storage.mount/'real'); storage.destination.symlink_to(storage.mount/'real', target_is_directory=True)
    if kind == 'changed':
        observations = iter([deepcopy(storage.observation), {'classification':'unknown'}])
        monkeypatch.setattr(mac_mount, 'observe', lambda path: next(observations))
    from desktop_assistant import nas_probe
    monkeypatch.setattr(nas_probe, '_write_probe', lambda path: pytest.fail('gate allowed probe'))
    with pytest.raises(ValueError, match='远端保存尚未确认'): nas_probe.probe(storage.destination)
    assert set(storage.destination.iterdir()) == before


@pytest.mark.parametrize('field,value', [('server_address','127.0.0.1'), ('server_address','::ffff:192.0.2.20'),
    ('server_address','0.0.0.0'), ('server_address','169.254.1.2'), ('context_sha256','0'*64), ('port',True),
    ('port',52100), ('version','3'), ('security','krb5'), ('mountpoint','/elsewhere')])
def test_root_contract_is_exact_and_bound(storage, field, value):
    storage.contract[field] = value; storage.records[s.CONTRACT] = s.canonical(storage.contract)
    with pytest.raises(ValueError): s.before_write(storage.destination)


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired(['ioreg'], 10), subprocess.CalledProcessError(1, ['ioreg'])])
def test_hardware_query_failure_is_unconfirmed_not_ui_exception(storage, monkeypatch, error):
    monkeypatch.setattr(s, '_contract', Mock(side_effect=error))
    assert s.verified_at(storage.root, {'last_backup':str(storage.final)}) is None
    with pytest.raises(ValueError, match='远端保存尚未确认'): s.before_write(storage.destination)


@pytest.mark.parametrize('kind', ['hash', 'size', 'symlink', 'hardlink', 'mount-change', 'replace-during-read'])
def test_readback_refuses_changed_final(storage, monkeypatch, kind):
    anchor = s.before_write(storage.destination)
    request = dict(challenge='1'*32, anchor=anchor, path=str(storage.final),
                   sha256=s.digest(storage.final.read_bytes()), size=storage.final.stat().st_size)
    if kind == 'hash': request['sha256'] = '0'*64
    if kind == 'size': request['size'] += 1
    if kind == 'symlink':
        old = storage.final.with_suffix('.original'); storage.final.rename(old); storage.final.symlink_to(old)
    if kind == 'hardlink': os.link(storage.final, storage.final.with_suffix('.link'))
    if kind == 'mount-change': monkeypatch.setattr(mac_mount, 'fd_identity', lambda fd: {})
    if kind == 'replace-during-read':
        real_read = os.read; replaced = False
        def read(fd, count):
            nonlocal replaced
            data = real_read(fd, count)
            if data and not replaced:
                replaced = True
                replacement = storage.final.with_suffix('.swap'); replacement.write_bytes(data)
                replacement.replace(storage.final)
            return data
        monkeypatch.setattr(os, 'read', read)
    with pytest.raises((ValueError, OSError)): s.readback(request)
    assert not storage.receipts.exists()


@pytest.mark.parametrize('field,value', [('verified_at',None), ('verified_at','2026-10-11T12:00:00'),
    ('verified_at','2999-01-01T00:00:00+00:00'), ('file_identity',{}), ('size',True), ('size',1.0),
    ('sha256','F'*64), ('path','/tmp/else.tmbackup'), ('contract_sha256','0'*64), ('type','other')])
def test_invalid_worker_result_and_direct_publication_never_publish(storage, monkeypatch, field, value):
    proof, receipt = storage.verified(); proof[field] = value
    def child(request): return dict(challenge=request['challenge'], proof=proof)
    monkeypatch.setattr(s, '_child', child)
    with pytest.raises((ValueError, TypeError)): s.after_write(s.before_write(storage.destination), storage.final, receipt['sha256'], receipt['size'])
    with pytest.raises((ValueError, TypeError)): s.publish_receipt(storage.receipt_path, receipt, proof)
    assert not storage.receipts.exists()


def test_old_proof_can_display_but_cannot_masquerade_as_fresh_worker(storage, monkeypatch):
    proof, receipt = storage.verified(); proof['verified_at'] = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    at = s.publish_receipt(storage.receipt_path, receipt, proof)
    assert s.verified_at(storage.root, dict(last_backup=str(storage.final),last_backup_at=at)) == at
    monkeypatch.setattr(s, '_child', lambda request: dict(challenge=request['challenge'], proof=proof))
    with pytest.raises(ValueError): s.after_write(s.before_write(storage.destination), storage.final, receipt['sha256'], receipt['size'])


@pytest.mark.parametrize('kind', ['receipt-bytes', 'missing', 'cross-context', 'future', 'state-time', 'fingerprint', 'permissions', 'file-acl', 'parent-acl', 'parent-link', 'parent-mode', 'during-read'])
def test_unsafe_local_proof_is_unconfirmed(storage, monkeypatch, kind):
    state, proof, receipt = storage.published()
    side = storage.receipt_path.with_name(storage.final.stem+'.mac-storage.json')
    if kind == 'receipt-bytes': storage.receipt_path.write_bytes(storage.receipt_path.read_bytes()+b' ')
    if kind == 'missing': side.unlink()
    if kind == 'cross-context': storage.records[keychain.CONTEXT] += b' '
    if kind in ('future','fingerprint'):
        value = json.loads(side.read_bytes())
        if kind == 'future': value['readback']['verified_at'] = state['last_backup_at'] = '2999-01-01T00:00:00+00:00'
        else: value['readback']['file_identity']['size'] = True
        side.write_bytes(s.canonical(value))
    if kind == 'state-time': state['last_backup_at'] = datetime.now(timezone.utc).isoformat()
    if kind == 'permissions': side.chmod(0o644)
    if kind in ('file-acl', 'parent-acl'):
        import pwd
        target = side if kind == 'file-acl' else storage.receipts
        # Real Darwin ACL; fixture owner only, no system or production paths.
        subprocess.run(['/bin/chmod','+a',f'user:{pwd.getpwuid(os.getuid()).pw_name} allow read,write',str(target)],check=True)
    if kind == 'parent-link':
        actual = storage.root/'control/real'; storage.receipts.rename(actual); storage.receipts.symlink_to(actual, target_is_directory=True)
    if kind == 'parent-mode': storage.receipts.chmod(0o755)
    if kind == 'during-read':
        real_read = os.read; once = False
        def read(fd, count):
            nonlocal once
            raw = real_read(fd, count)
            if raw and not once:
                once = True; storage.receipt_path.write_bytes(storage.receipt_path.read_bytes()+b' ')
            return raw
        monkeypatch.setattr(os, 'read', read)
    assert s.verified_at(storage.root, state) is None


@pytest.mark.parametrize('kind', ['missing-control', 'missing-instance', 'parent-mode', 'parent-link', 'acl', 'sidecar-failure', 'fsync-failure', 'directory-swap'])
def test_publication_failure_preserves_evidence_without_success(storage, monkeypatch, kind):
    proof, receipt = storage.verified()
    if kind == 'missing-control': (storage.root/'control').rmdir()
    if kind == 'missing-instance': (storage.root/'control').rmdir(); storage.root.rmdir()
    if kind == 'parent-mode': (storage.root/'control').chmod(0o755)
    if kind == 'parent-link':
        (storage.root/'control').rename(storage.root/'other'); (storage.root/'control').symlink_to(storage.root/'other', target_is_directory=True)
    if kind == 'acl':
        import pwd
        subprocess.run(['/bin/chmod','+a',f'user:{pwd.getpwuid(os.getuid()).pw_name} allow read,write',str(storage.root/'control')],check=True)
    if kind in ('sidecar-failure', 'directory-swap'):
        real_exclusive = s._exclusive
        def exclusive(directory, name, raw, uid):
            if name.endswith('.mac-storage.json'):
                if kind == 'sidecar-failure': raise OSError('synthetic sidecar failure')
                storage.receipts.rename(storage.root/'control/old-receipts'); storage.receipts.mkdir(mode=0o700)
            return real_exclusive(directory,name,raw,uid)
        monkeypatch.setattr(s, '_exclusive', exclusive)
    if kind == 'fsync-failure':
        real_fsync = os.fsync
        def fsync(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('synthetic directory fsync failure')
            return real_fsync(fd)
        monkeypatch.setattr(os, 'fsync', fsync)
    with pytest.raises((ValueError, OSError)): s.publish_receipt(storage.receipt_path, receipt, proof)
    assert storage.final.is_file()
    if kind == 'sidecar-failure': assert storage.receipt_path.is_file()
    if kind.startswith('missing'): assert not storage.receipts.exists()


def test_exclusive_no_overwrite_and_non_mac_six_field_compatibility(storage, monkeypatch):
    state, proof, receipt = storage.published(); original = storage.receipt_path.read_bytes()
    with pytest.raises(FileExistsError): s.publish_receipt(storage.receipt_path,receipt,proof)
    assert storage.receipt_path.read_bytes() == original
    monkeypatch.setattr(s, 'required', lambda: False)
    legacy = storage.receipts/'windows.json'; s.publish_receipt(legacy,receipt,None)
    assert set(json.loads(legacy.read_bytes())) == s.RECEIPT_FIELDS
    assert not legacy.with_name('windows.mac-storage.json').exists()
    assert s.before_write('/unknown') is None and s.after_write(None,None,None,None) is None


def test_worker_challenge_is_not_replayable(storage, monkeypatch):
    proof, receipt = storage.verified()
    monkeypatch.setattr(s, '_child', lambda request: dict(challenge='0'*32,proof=proof))
    with pytest.raises(ValueError): s.after_write(s.before_write(storage.destination),storage.final,receipt['sha256'],receipt['size'])


@pytest.mark.parametrize('target', ['final', 'receipt', 'sidecar'])
def test_fifo_is_rejected_without_waiting_for_writer(storage, target):
    state, proof, receipt = storage.published()
    path = storage.final if target == 'final' else storage.receipt_path
    if target == 'sidecar': path = storage.receipt_path.with_name(storage.final.stem+'.mac-storage.json')
    path.unlink(); os.mkfifo(path, 0o600)
    if target == 'final':
        with pytest.raises(ValueError):
            s.readback(dict(challenge='1'*32,anchor=s.before_write(storage.destination),
                            path=str(path),sha256=proof['sha256'],size=proof['size']))
    else: assert s.verified_at(storage.root,state) is None


def test_fixed_local_data_volume_is_checked(storage, monkeypatch):
    state, _, _ = storage.published()
    real_stat = Path.stat
    def stat(path, *a, **kw):
        if path == Path('/System/Volumes/Data'): return SimpleNamespace(st_dev=-1)
        return real_stat(path, *a, **kw)
    monkeypatch.setattr(Path, 'stat', stat)
    assert s.verified_at(storage.root,state) is None


def test_child_runs_once_with_isolated_environment_and_observes_interrupt(storage, monkeypatch):
    process = Mock(returncode=0)
    process.communicate.side_effect = [KeyboardInterrupt(), (b'{"finished":true}', b'')]
    popen = Mock(return_value=process); monkeypatch.setattr(s.subprocess,'Popen',popen)
    request = dict(challenge='1'*32)
    with pytest.raises(ValueError): REAL_CHILD(request)
    assert popen.call_count == 1
    args, kw = popen.call_args
    assert args[0][1:3] == ['-I','-B'] and args[0][-1] == '--readback'
    assert kw['close_fds'] and set(kw['env']) == {'HOME','PATH','LANG'}
    assert process.communicate.call_args_list[0].args == (s.canonical(request),)
    assert process.communicate.call_args_list[1].args == (None,)
    process.kill.assert_not_called(); process.terminate.assert_not_called()


def test_real_isolated_child_rejects_invalid_request_before_context_or_files():
    # Real new interpreter, not the in-process synthetic worker. No valid path,
    # root context or network request is supplied; it must fail before those.
    with pytest.raises(ValueError,match='远端保存尚未确认'): REAL_CHILD({'challenge':'invalid'})


@pytest.fixture
def real_manager(storage, monkeypatch):
    from tests.desktop_assistant import test_recovery as recovery
    from desktop_assistant.manager import Manager
    case = recovery.RecoveryTests(); case.setUp()
    try:
        shutil.copytree(case.manager.root, storage.root, dirs_exist_ok=True)
        storage.root.chmod(0o700); (storage.root/'control').chmod(0o700)
        manager = Manager(storage.root, case.public)  # Base class: NO TestManager storage adapter.
        monkeypatch.setattr(manager,'_process',lambda:None)
        monkeypatch.setattr(manager,'stop',Mock())
        monkeypatch.setattr(manager,'start',Mock())
        yield manager
    finally: case.tearDown()


def test_real_backup_pipeline_publishes_complete_mac_evidence(storage, real_manager):
    final = real_manager._backup_stopped('synthetic-passphrase',storage.destination)
    state = real_manager.state
    assert final != storage.final and final.is_file()
    assert s.verified_at(storage.root,state) == state['last_backup_at']
    receipt = json.loads((storage.receipts/(final.stem+'.json')).read_bytes())
    assert set(receipt) == s.RECEIPT_FIELDS and receipt['sha256'] == s.digest(final.read_bytes())
    assert list((storage.destination/'tianming-backup-work').iterdir()) == []


@pytest.mark.parametrize('failure', ['authenticated-source-swap', 'renamed-final-swap', 'final-fifo', 'worker', 'sidecar'])
def test_real_backup_failure_never_updates_success_and_keeps_job(storage, real_manager, monkeypatch, failure):
    from desktop_assistant import manager as manager_module
    original = real_manager.state
    if failure == 'authenticated-source-swap':
        actual = manager_module.decrypt_file
        def decrypt(path, dest, password):
            authenticated = actual(path,dest,password)
            path.write_bytes(b'changed-after-authentication')
            return authenticated
        monkeypatch.setattr(manager_module,'decrypt_file',decrypt)
    if failure in ('renamed-final-swap','final-fifo'):
        actual = Path.rename
        def rename(path, target):
            result = actual(path,target)
            if path.name.endswith('.tmbackup.pending'):
                target = Path(target)
                if failure == 'final-fifo': target.unlink(); os.mkfifo(target,0o600)
                else: target.write_bytes(b'changed-after-rename')
            return result
        monkeypatch.setattr(Path,'rename',rename)
    if failure == 'worker': monkeypatch.setattr(s,'_child',Mock(side_effect=ValueError('synthetic failed worker')))
    if failure == 'sidecar':
        actual = s._exclusive
        def exclusive(directory,name,raw,uid):
            if name.endswith('.mac-storage.json'): raise OSError('synthetic sidecar failure')
            return actual(directory,name,raw,uid)
        monkeypatch.setattr(s,'_exclusive',exclusive)
    with pytest.raises((ValueError,OSError)): real_manager._backup_stopped('synthetic-passphrase',storage.destination)
    assert real_manager.state == original
    jobs = list((storage.destination/'tianming-backup-work').iterdir())
    assert len(jobs) == 1 and (jobs[0]/'recovery.zip').is_file()
    assert (jobs[0]/'credentials.tmencrypted').is_file()
    assert not list(storage.receipts.glob('*.mac-storage.json'))
    real_manager.start.assert_not_called()


@pytest.mark.parametrize('entry', ['backup','pause','primitive','onboard-probe','prestop-probe'])
def test_unapproved_nas_rejected_before_stop_or_creation(storage, real_manager, monkeypatch, entry):
    from desktop_assistant import onboarding, nas_probe
    storage.observation['classification'] = 'loopback_proxy'
    before = real_manager.state
    actions = {'backup':lambda:real_manager.backup('synthetic-passphrase',storage.destination),
        'pause':lambda:real_manager.pause_after_backup('synthetic-passphrase',storage.destination),
        'primitive':lambda:real_manager._backup_stopped('synthetic-passphrase',storage.destination),
        'onboard-probe':lambda:onboarding._check_nas('synthetic-passphrase',storage.destination),
        'prestop-probe':lambda:nas_probe.check_before_stop(storage.destination)}
    with pytest.raises(ValueError): actions[entry]()
    real_manager.stop.assert_not_called(); real_manager.start.assert_not_called()
    assert real_manager.state == before and not (storage.destination/'tianming-backup-work').exists()


@pytest.mark.parametrize('stamp', ['2026-10-11T23:59:00+08:00', '2999-01-01T00:00:00+00:00'])
def test_old_mac_timestamp_never_resumes_or_reads_password(storage, real_manager, monkeypatch, stamp):
    from desktop_assistant import nightly
    state = real_manager.state; state.update(last_backup=str(storage.final),last_backup_at=stamp)
    write_json(storage.root/'state.json',state)
    write_json(storage.root/'preferences.json',dict(nas=str(storage.destination),protected_password='synthetic-reference'))
    storage.observation['classification'] = 'loopback_proxy'
    password = Mock(side_effect=AssertionError('must not read credential'))
    monkeypatch.setattr(nightly,'unprotect_backup',password)
    with pytest.raises(ValueError): nightly.run_once(real_manager,now=datetime(2026,10,11,23,5,tzinfo=timezone(timedelta(hours=8))),resume_when_stopped=True)
    password.assert_not_called(); real_manager.start.assert_not_called(); real_manager.stop.assert_not_called()
    assert 'last_backup_at' in real_manager.state and not (storage.destination/'tianming-backup-work').exists()


def test_gui_and_web_treat_old_receipt_as_unconfirmed(storage, real_manager, monkeypatch):
    from desktop_assistant.gui import App
    from app.services.managed_recovery_status import managed_recovery_status
    state = real_manager.state; state.update(last_backup=str(storage.final),last_backup_at='2026-10-11T23:59:00+08:00')
    write_json(storage.root/'state.json',state)
    _, receipt = storage.verified(); storage.receipts.mkdir(mode=0o700)
    storage.receipt_path.write_bytes(s.canonical(receipt)); storage.receipt_path.chmod(0o600)
    view = SimpleNamespace(manager=real_manager,busy=False,connection_status='',status=Mock(),action_buttons=[])
    App.refresh(view)
    assert '远端保存尚未确认' in view.status.set.call_args.args[0]
    monkeypatch.setenv('TM_ERP_CONTROL',str(storage.root/'control'))
    status = managed_recovery_status()
    assert status['status'] == 'receipt_unverified' and status['latest_backup']['available'] is True


def test_legacy_full_backup_restores_without_storage_contract(storage, monkeypatch):
    from tests.desktop_assistant import test_recovery as recovery
    from desktop_assistant.manager import Manager
    case = recovery.RecoveryTests(); case.setUp()
    try:
        backup = case.manager.backup('synthetic-passphrase',case.nas)
        monkeypatch.setattr(s,'_contract',Mock(side_effect=AssertionError('restore must not need new sidecar')))
        restored = Manager(case.root/'restored',case.public)
        restored.restore(backup,'synthetic-passphrase')
        from desktop_assistant.storage import database_info
        assert database_info(restored.root/'shared/data/carton_erp.sqlite3') == database_info(case.manager.root/'shared/data/carton_erp.sqlite3')
        assert (restored.root/'shared/data/drawing.pdf').read_bytes() == b'synthetic attachment'
    finally: case.tearDown()


@pytest.mark.parametrize('failure', ['preflight','worker','sidecar'])
def test_update_never_migrates_without_mac_backup_evidence(storage,real_manager,monkeypatch,failure):
    from desktop_assistant import migration, nas_probe
    original = real_manager.state
    candidate = dict(id='f'*64,revision='r2',migration=dict(policy='preserve_existing_facts_v1',
                     from_revision='r1',rollback_package_sha256=original['current']))
    # Only the candidate migration contract is synthetic; the stopped backup
    # and all Mac storage checks below use the actual production implementation.
    monkeypatch.setattr(real_manager,'stage_release',lambda package:candidate)
    monkeypatch.setattr(real_manager,'_check_dispatch_reader',lambda *a,**kw:None)
    monkeypatch.setattr(real_manager,'compatible',lambda *a,**kw:False)
    monkeypatch.setattr(nas_probe,'check_before_stop',lambda path:s.before_write(path))
    rehearse = Mock(); migrate = Mock()
    monkeypatch.setattr(migration,'rehearse',rehearse); monkeypatch.setattr(migration,'run_migration',migrate)
    if failure == 'preflight': storage.observation['classification'] = 'loopback_proxy'
    if failure == 'worker': monkeypatch.setattr(s,'_child',Mock(side_effect=ValueError('synthetic readback failed')))
    if failure == 'sidecar':
        actual = s._exclusive
        def exclusive(directory,name,raw,uid):
            if name.endswith('.mac-storage.json'): raise OSError('synthetic sidecar failed')
            return actual(directory,name,raw,uid)
        monkeypatch.setattr(s,'_exclusive',exclusive)
    with pytest.raises((ValueError,OSError)): real_manager._update_locked(Path('/unused-synthetic.zip'),'synthetic-passphrase',storage.destination)
    rehearse.assert_not_called(); migrate.assert_not_called()
    assert real_manager.state == original
    if failure == 'preflight': real_manager.stop.assert_not_called()


def test_onboard_and_finish_reject_before_original_marker_or_pending_clear(storage,real_manager,monkeypatch):
    from desktop_assistant import onboarding
    from desktop_assistant.manager import Manager
    storage.observation['classification'] = 'loopback_proxy'
    source=storage.base/'synthetic-original';source.mkdir()
    stop=Mock();monkeypatch.setattr(onboarding,'stop_original',stop)
    empty=Manager(storage.base/'empty',real_manager.public_key)
    with pytest.raises(ValueError,match='远端保存尚未确认'):
        onboarding.onboard(empty,source,Path('/unused-synthetic.zip'),'synthetic-passphrase',storage.destination)
    stop.assert_not_called();assert list(source.iterdir()) == [] and list((empty.root/'shared').iterdir()) == []
    state=real_manager.state;state.update(onboarding_pending=True,imported_source=str(source))
    write_json(storage.root/'state.json',state)
    with pytest.raises(ValueError,match='远端保存尚未确认'):
        onboarding.finish_onboarding(real_manager,'synthetic-passphrase',storage.destination)
    assert real_manager.state == state and list(source.iterdir()) == []
    real_manager.start.assert_not_called()


def test_probe_entry_works_from_nonrepository_cwd_and_isolates_environment(storage,monkeypatch):
    from desktop_assistant import nas_probe
    cwd = storage.base/'untrusted-cwd';cwd.mkdir()
    # A CWD package must neither supply nor intercept the isolated helper.
    shadow=cwd/'desktop_assistant';shadow.mkdir();(shadow/'__init__.py').write_text('raise RuntimeError("CWD shadow imported")')
    monkeypatch.chdir(cwd);monkeypatch.setenv('PYTHONPATH',str(cwd))
    actual = subprocess.run; results=[]
    def run(command,**kwargs):
        assert command[1:3] == ['-I','-B'] and Path(command[3]) == Path(nas_probe.__file__).resolve()
        assert set(kwargs['env']) == {'HOME','PATH','LANG'}
        value=actual(command,**kwargs);results.append(value);return value
    monkeypatch.setattr(nas_probe.subprocess,'run',run)
    with pytest.raises(ValueError,match='NAS无法完成读写验证'): nas_probe.check_before_stop(storage.destination)
    # No startup traceback or probe writes. A separate isolated import check
    # below distinguishes a usable import chain from a swallowed ImportError.
    assert len(results)==1 and results[0].returncode==2 and results[0].stderr==b''
    assert not list(storage.destination.glob('tianming-connectivity-*'))
    imported = actual([sys.executable,'-I','-B','-c',
        "import runpy,sys; from pathlib import Path; "
        "runpy.run_path(sys.argv[1],run_name='probe_import_check'); "
        "from desktop_assistant import mac_backup_storage as storage; "
        "assert Path(storage.__file__).resolve()==Path(sys.argv[1]).with_name('mac_backup_storage.py'); "
        "print('isolated-probe-imports-ok')",str(Path(nas_probe.__file__).resolve())],
        env={'HOME':str(storage.base/'home'),'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'},
        capture_output=True,timeout=10,check=True)
    assert imported.stdout.strip()==b'isolated-probe-imports-ok' and imported.stderr==b''


def test_mac_test_module_collects_without_unix_only_imports(monkeypatch):
    import builtins, runpy
    actual = builtins.__import__
    def portable_import(name, *args, **kwargs):
        if name in ('pwd','fcntl'): raise ImportError('unavailable on Windows: '+name)
        return actual(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',portable_import)
    collected = runpy.run_path(__file__,run_name='synthetic_windows_collection')
    assert callable(collected['test_full_synthetic_readback_receipt_and_local_fast_path'])

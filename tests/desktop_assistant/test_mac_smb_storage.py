"""Real temp-directory FDs; explicit synthetic UID/mount/session adapters."""
from copy import deepcopy
import os
from types import SimpleNamespace
import json
import sys

import pytest
from desktop_assistant import mac_smb_storage as location
from desktop_assistant import mac_backup_storage as storage
from tests.desktop_assistant import test_mac_smb_contract as fixtures
from tests.desktop_assistant.test_mac_backup_storage import storage as local_storage
from tests.desktop_assistant import test_mac_backup_storage as existing

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac native FD/ACL/F_NOCACHE tests')


@pytest.fixture
def mounted(tmp_path, monkeypatch):
    point = tmp_path / 'archive'
    destination = point / location.cfg.RELATIVE
    destination.mkdir(parents=True, mode=0o700)
    for path in (point, destination.parent, destination): path.chmod(0o700)
    monkeypatch.setattr(location.cfg, 'MOUNT', point)
    value = fixtures.value()
    value.update(mountpoint=str(point), destination=str(destination))
    observation = fixtures.observation()
    observation['kernel']['mountpoint'] = str(point)
    # Host identities are synthetic, filesystem FDs and namespace races are real.
    monkeypatch.setattr(location, '_dedicated', lambda uid: None)
    monkeypatch.setattr(location, '_local_parent', lambda path, info: None)
    real_directory = location._directory
    def directory(fd, uid):
        assert uid == 499
        result = real_directory(fd, os.getuid(), gid=os.getgid())
        return {**result, 'uid':499, 'gid':499}
    monkeypatch.setattr(location, '_directory', directory)
    monkeypatch.setattr(location.observer, 'observe', lambda *a, **kw: deepcopy(observation))
    monkeypatch.setattr(location.mounts, '_select', lambda *a: deepcopy(observation['kernel']))
    monkeypatch.setattr(location.mounts, 'fd_identity', lambda *a: deepcopy(observation['kernel']))
    monkeypatch.setattr(location.mounts, 'local_addresses', lambda: {'127.0.0.1'})
    return SimpleNamespace(point=point, destination=destination, value=value, observation=observation)


def test_mounted_location_holds_real_fds_without_writing(mounted):
    before = list(mounted.destination.iterdir())
    with location.open_location(mounted.value, context_sha=fixtures.CONTEXT, uid=499) as held:
        assert len(held.directory_identity) == 3
        assert os.fstat(held.destination_fd).st_ino == mounted.destination.stat().st_ino
        assert held.check() == mounted.observation
        fd = held.destination_fd
    with pytest.raises(OSError): os.fstat(fd)
    assert list(mounted.destination.iterdir()) == before


@pytest.mark.parametrize('kind', ['missing', 'link', 'permissions', 'local', 'guest', 'unencrypted'])
def test_bad_location_is_not_created_or_adopted(mounted, kind):
    if kind == 'missing': mounted.destination.rmdir()
    if kind == 'link':
        old = mounted.destination.with_name('original'); mounted.destination.rename(old)
        mounted.destination.symlink_to(old)
    if kind == 'permissions': mounted.destination.chmod(0o755)
    if kind == 'local': mounted.observation['kernel']['filesystem'] = 'apfs'
    if kind == 'guest': mounted.observation['session']['auth_type'] = 'guest'
    if kind == 'unencrypted': mounted.observation['share']['encrypted'] = False
    with pytest.raises((OSError, ValueError)):
        with location.open_location(mounted.value, context_sha=fixtures.CONTEXT, uid=499): pass
    if kind == 'missing': assert not mounted.destination.exists()


@pytest.mark.parametrize('kind', ['replace', 'session', 'contract', 'mount'])
def test_change_while_held_rejects_completion(mounted, kind):
    with pytest.raises(ValueError):
        with location.open_location(mounted.value, context_sha=fixtures.CONTEXT, uid=499):
            if kind == 'replace':
                mounted.destination.rename(mounted.destination.with_name('retained'))
                mounted.destination.mkdir(mode=0o700)
            if kind == 'session': mounted.observation['session']['uuid'] = '9'*32
            if kind == 'contract': mounted.value['username'] = 'replacement'
            if kind == 'mount': mounted.observation['kernel']['fsid'] = [44,55]


def test_existing_backup_consumer_accepts_smb_only_with_bound_location(mounted, monkeypatch):
    context = {'uid':499, 'gid':499}
    monkeypatch.setattr(storage, '_contract', lambda: (mounted.value, 'c'*64, context))
    monkeypatch.setattr(storage, 'required', lambda: True)
    anchor = storage.before_write(mounted.destination)
    assert anchor['context_sha256'] == fixtures.CONTEXT
    assert anchor['mount'] == mounted.observation
    assert len(anchor['directory_identity']) == 3
    with pytest.raises(ValueError): storage.before_write(mounted.point)


@pytest.fixture
def smb_backup(local_storage, mounted, monkeypatch):
    # Existing fixture provides genuine private local receipt IO with explicit
    # root identity adaptation. SMB connections remain synthetic throughout.
    mounted.value['context_sha256'] = local_storage.contract['context_sha256']
    local_storage.context.update(uid=499, gid=499)
    local_storage.records[storage.CONTRACT] = storage.canonical(mounted.value)
    original_check = storage._check_fd
    def check(fd, *, uid, directory, device=None):
        assert uid == 499
        return original_check(fd, uid=os.getuid(), directory=directory, device=device)
    monkeypatch.setattr(storage, '_check_fd', check)
    final = mounted.destination / '20261011-synthetic.tmbackup'
    final.write_bytes(os.urandom(1024*1024+17))
    return SimpleNamespace(**vars(mounted), records=local_storage, final=final)


@pytest.fixture
def smb_manager(smb_backup, monkeypatch):
    yield from existing.real_manager.__wrapped__(smb_backup.records, monkeypatch)


def proof_for(case):
    raw = case.final.read_bytes()
    anchor = storage.before_write(case.destination)
    return storage.after_write(anchor, case.final, storage.digest(raw), len(raw))


def test_smb_readback_and_historical_sidecar_without_network(smb_backup, monkeypatch):
    case = smb_backup
    proof = proof_for(case)
    assert proof['type'] == storage.SMB_PROOF_TYPE
    assert proof['sha256'] == storage.digest(case.final.read_bytes())
    receipt = dict(path=str(case.final), sha256=proof['sha256'], size=proof['size'],
                   verified=True, storage='nas', database={'revision':'synthetic'})
    receipt_path = case.records.receipts / (case.final.stem + '.json')
    when = storage.publish_receipt(receipt_path, receipt, proof)
    case.final.unlink()
    monkeypatch.setattr(location.observer, 'observe', lambda *a, **kw: pytest.fail('status touched NAS'))
    assert storage.verified_at(case.records.root, {'last_backup':str(case.final), 'last_backup_at':when}) == when


@pytest.mark.parametrize('kind', ['missing', 'uid', 'mode', 'extra', 'nfs-proof', 'file-device', 'file-inode'])
def test_smb_proof_cannot_omit_or_replace_directory_binding(smb_backup, kind):
    proof = proof_for(smb_backup)
    if kind == 'missing': proof.pop('directory_identity')
    if kind == 'uid': proof['directory_identity'][2]['uid'] = 501
    if kind == 'mode': proof['directory_identity'][2]['mode'] |= 0o077
    if kind == 'extra': proof['directory_identity'].append(proof['directory_identity'][0])
    if kind == 'nfs-proof': proof['type'] = storage.PROOF_TYPE
    if kind == 'file-device': proof['file_identity']['dev'] += 1
    if kind == 'file-inode': proof['file_identity']['ino'] = 0
    receipt = dict(path=str(smb_backup.final), sha256=proof['sha256'], size=proof['size'],
                   verified=True, storage='nas', database={})
    with pytest.raises(ValueError):
        storage.publish_receipt(smb_backup.records.receipts/(smb_backup.final.stem+'.json'), receipt, proof)
    assert not smb_backup.records.receipts.exists()


@pytest.mark.parametrize('kind', ['destination', 'session', 'contract', 'bytes'])
def test_independent_readback_rejects_late_changes(smb_backup, monkeypatch, kind):
    original = storage._child
    def child(request):
        if kind == 'destination':
            smb_backup.destination.rename(smb_backup.destination.with_name('old'))
            smb_backup.destination.mkdir(mode=0o700)
            smb_backup.final.write_bytes(b'wrong storage')
        if kind == 'session': smb_backup.observation['session']['uuid'] = '9'*32
        if kind == 'contract':
            value = {**smb_backup.value, 'username':'changed'}
            smb_backup.records.records[storage.CONTRACT] = storage.canonical(value)
        if kind == 'bytes': smb_backup.final.write_bytes(b'changed backup')
        return original(request)
    monkeypatch.setattr(storage, '_child', child)
    with pytest.raises(ValueError): proof_for(smb_backup)
    assert not smb_backup.records.receipts.exists()


def test_complete_encrypted_backup_uses_smb_binding(smb_backup, smb_manager):
    final = smb_manager._backup_stopped('synthetic-passphrase', smb_backup.destination)
    state = smb_manager.state
    assert state['last_backup'] == str(final)
    assert storage.verified_at(smb_backup.records.root, state) == state['last_backup_at']
    sidecar = json.loads((smb_backup.records.receipts/(final.stem+'.mac-storage.json')).read_bytes())
    assert sidecar['readback']['type'] == storage.SMB_PROOF_TYPE
    assert len(sidecar['readback']['directory_identity']) == 3
    assert list((smb_backup.destination/'tianming-backup-work').iterdir()) == []


def test_failed_smb_readback_preserves_backup_job_and_no_success(smb_backup, smb_manager, monkeypatch):
    before = smb_manager.state
    def fail(request): raise ValueError('synthetic readback failure')
    monkeypatch.setattr(storage, '_child', fail)
    with pytest.raises(ValueError): smb_manager._backup_stopped('synthetic-passphrase', smb_backup.destination)
    assert smb_manager.state == before
    jobs = list((smb_backup.destination/'tianming-backup-work').iterdir())
    assert len(jobs) == 1 and (jobs[0]/'recovery.zip').is_file()
    assert not smb_backup.records.receipts.exists()


@pytest.mark.parametrize('kind', ['local', 'guest', 'unencrypted', 'destination-link', 'contract-uid'])
def test_smb_refusal_happens_before_erp_stop(smb_backup, smb_manager, kind):
    before = smb_manager.state
    if kind == 'local': smb_backup.observation['kernel']['filesystem'] = 'apfs'
    if kind == 'guest': smb_backup.observation['session']['auth_type'] = 'guest'
    if kind == 'unencrypted': smb_backup.observation['share']['encrypted'] = False
    if kind == 'destination-link':
        old = smb_backup.destination.with_name('original'); smb_backup.destination.rename(old)
        smb_backup.destination.symlink_to(old)
    if kind == 'contract-uid':
        value = {**smb_backup.value, 'uid':501}
        smb_backup.records.records[storage.CONTRACT] = storage.canonical(value)
    with pytest.raises(ValueError): smb_manager.backup('synthetic-passphrase', smb_backup.destination)
    smb_manager.stop.assert_not_called()
    assert smb_manager.state == before
    assert not (smb_backup.destination/'tianming-backup-work').exists()


def test_exposed_identity_copy_cannot_change_held_baseline(mounted):
    with location.open_location(mounted.value, context_sha=fixtures.CONTEXT, uid=499) as held:
        copy = held.directory_identity
        copy[-1]['ino'] += 1
        observed = held.check(); observed['session']['uuid'] = '9'*32
        assert held.directory_identity != copy
        assert held.check() == mounted.observation

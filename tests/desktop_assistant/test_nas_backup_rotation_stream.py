"""Real SMB Open/SET_INFO wire structures, synthetic peer; no login/network."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest
from smbprotocol import open as smb
from smbprotocol.exceptions import ObjectNameNotFound, SMB2ErrorResponse
from smbprotocol.file_info import FileDispositionInformation
from smbprotocol.header import SMB2HeaderResponse

from desktop_assistant import mac_smb_stream_files_v2 as files
from desktop_assistant.mac_smb_stream_contract import digest as contract_digest
from desktop_assistant.mac_smb_stream_receipt import validate as existing_validate, receipt_name, matches_receipt_name
from desktop_assistant import nas_backup_rotation as rotation
from desktop_assistant import nas_backup_rotation_stream as stream
from desktop_assistant.storage import MAGIC
from tests.desktop_assistant.test_nas_backup_rotation import Adapter, report_for, pointer
from tests.desktop_assistant.test_mac_smb_stream_files import Peer as FilePeer, contract


def missing():
    response = SMB2HeaderResponse()
    response['status'] = 0xc0000034
    response['data'] = SMB2ErrorResponse().pack()
    return ObjectNameNotFound(response)


class Peer(FilePeer):
    def __init__(self):
        super().__init__()
        self.removal = True
        self.permissions = []

    def reply(self, request):
        command = request.COMMAND
        if command == 5 and request['create_disposition'].get_value() == 2:
            return super().reply(request)
        if command == 17 and request['file_info_class'].get_value() != FileDispositionInformation.INFO_CLASS:
            return super().reply(request)
        if command == 5:
            name = request['buffer_path'].get_value().decode('utf-16-le').rstrip('\0')
            if name not in self.nodes: raise missing()
            node = self.nodes[name]
            access, share = request['desired_access'].get_value(), request['share_access'].get_value()
            self.permissions.append((name, access, share))
            assert share == (3 if node['directory'] else 0 if access & 0x10000 else 1)
            assert request['create_disposition'].get_value() == 1
            assert request['create_options'].get_value() & 0x200000
            self.serial += 1
            file_id = struct.pack('<QQ', self.serial, 1)
            self.handles[file_id] = node
            result = smb.SMB2CreateResponse()
            result['create_action'], result['file_id'] = 1, file_id
            result['file_attributes'], result['end_of_file'] = node['attrs'], len(node['data'])
            self.events.append(('open', name))
        elif command == 17:
            node = self.handles[request['file_id'].get_value()]
            assert request['file_info_class'].get_value() == FileDispositionInformation.INFO_CLASS
            disposition = FileDispositionInformation()
            assert disposition.unpack(request['buffer'].get_value()) == b''
            assert disposition['delete_pending'].get_value() is True
            node['delete_pending'] = True
            self.events.append(('delete-held', node['name']))
            result = smb.SMB2SetInfoResponse()
        else:
            node = self.handles.get(request['file_id'].get_value()) if command == 6 else None
            response = super().reply(request)
            if node and node['delete_pending'] and self.removal: self.nodes.pop(node['name'])
            return response
        self.requests.append(request)
        self.hook(command, node, result)
        response = SMB2HeaderResponse()
        response['command'], response['session_id'], response['tree_id'] = command, 17, 23
        response['data'] = result.pack()
        return response


def setup():
    peer = Peer()
    value, versions = contract(), []
    for number in (10, 11, 12):
        p = pointer(number)
        filename = p['receipt'].replace('.smb-stream.json', '.tmbackup')
        data = MAGIC + bytes([number]) * 100
        node = peer.new_node('BoxERP\\mac-server-backups\\' + filename, data=data)
        byte_sha = hashlib.sha256(data).hexdigest()
        upload = {'type': files.UPLOAD, 'contract_sha256': contract_digest(value), 'connection_guid': 'a' * 32,
                  'filename': filename, 'bytes': len(data), 'sha256': byte_sha,
                  'directories': [{'index': peer.nodes[name]['index']} for name in files.DIRECTORIES],
                  'file': {'index': node['index']}, 'backup_authenticated': False}
        receipt = {'type': 'tianming.mac-smb-stream-backup-receipt.v1', 'context_sha256': value['context_sha256'],
                   'contract_sha256': contract_digest(value), 'control_sha256': 'c' * 64, 'package_sha256': 'd' * 64,
                   'filename': filename, 'sha256': byte_sha, 'size': len(data), 'raw_sha256': 'e' * 64,
                   'database': {'revision': 'fixture', 'counts': {'users': 1}},
                   'verified_at': datetime.now(timezone.utc).isoformat(),
                   'transfer': {'upload': upload, 'download': dict(upload, type=files.READBACK, connection_guid='b' * 32)},
                   'local_copy': {'path': '/Library/TianmingERP/instance/backups/' + filename,
                                  'sha256': byte_sha, 'size': len(data),
                                  'identity': {'dev': 1, 'ino': number, 'uid': os.geteuid(), 'gid': os.getegid(),
                                               'mode': 0o100600, 'nlink': 1, 'size': len(data), 'mtime_ns': 1, 'ctime_ns': 1}}}
        versions.append({'pointer': p, 'receipt': receipt, 'source_time': f'2026-10-{number}T01:02:03+08:00',
                         'recovery_sha256': 'f' * 64, 'contract_sha256': contract_digest(value)})
    adapter = Adapter(versions, kept=[versions[2]['pointer'], versions[1]['pointer']], retired=[versions[0]['pointer']])
    return peer, value, versions, adapter


def run(peer, value, adapter):
    with files.open_archive(peer.tree(), value) as archive: return stream.execute_next(adapter, archive)


def test_exact_retired_fileid_removed_survivors_pinned_and_queue_acknowledged():
    peer, value, versions, adapter = setup()
    result = run(peer, value, adapter)
    assert result['status'] == 'removed' and result['removed_bytes'] == versions[0]['receipt']['size']
    assert not adapter.index['retired']
    assert all('BoxERP\\mac-server-backups\\' + v['receipt']['filename'] in peer.nodes for v in versions[1:])
    assert any(access & 0x10000 and share == 0 for _, access, share in peer.permissions)
    assert not peer.handles


@pytest.mark.parametrize('kind', ['last_path', 'stream_receipt', 'stream_hash', 'rehearsal', 'active'])
def test_stable_active_reference_to_retired_file_never_deletes(kind):
    peer, value, versions, adapter = setup()
    target = versions[0]
    if kind == 'last_path': adapter.running_state['last_backup'] = '/cache/' + target['receipt']['filename'].upper()
    elif kind == 'stream_receipt': adapter.running_state['last_backup_stream'] = {'receipt': target['pointer']['receipt'].upper()}
    elif kind == 'stream_hash': adapter.running_state['last_backup_stream'] = {'receipt_sha256': target['pointer']['receipt_sha256']}
    elif kind == 'rehearsal': adapter.active['rehearsal_sha256'] = [target['receipt']['sha256']]
    else: adapter.active['active_paths'] = ['/mount/BoxERP/mac-server-backups/' + target['receipt']['filename']]
    with pytest.raises(ValueError): run(peer, value, adapter)
    assert not any(event[0] == 'delete-held' for event in peer.events)
    assert adapter.index['retired'] == [target['pointer']]


@pytest.mark.parametrize('kind', ['survivor_corrupt', 'target_identity', 'reparse', 'reference_change', 'index_change'])
def test_changed_byte_identity_or_authority_preserves_retirement_queue(kind):
    peer, value, versions, adapter = setup()
    target_path = 'BoxERP\\mac-server-backups\\' + versions[0]['receipt']['filename']
    if kind == 'survivor_corrupt': peer.nodes['BoxERP\\mac-server-backups\\' + versions[2]['receipt']['filename']]['data'][0] ^= 1
    elif kind == 'target_identity': peer.nodes[target_path]['index'] += 1
    elif kind == 'reparse': peer.nodes[target_path]['attrs'] |= 0x400
    else:
        def change(command, node, result):
            if command == 8:
                if kind == 'reference_change': adapter.running_state['last_backup'] = '/cache/' + versions[0]['receipt']['filename']
                else: adapter.index['generation'] += 1
        peer.hook = change
    with pytest.raises(ValueError): run(peer, value, adapter)
    assert target_path in peer.nodes and adapter.index['retired']
    assert not any(event[0] == 'delete-held' for event in peer.events)
    assert not peer.handles


def test_crash_after_remote_delete_before_cas_replays_authenticated_absence():
    peer, value, versions, adapter = setup()
    adapter.conflict = True
    with pytest.raises(ValueError): run(peer, value, adapter)
    assert adapter.index['retired'] and 'BoxERP\\mac-server-backups\\' + versions[0]['receipt']['filename'] not in peer.nodes
    adapter.conflict = False
    result = run(peer, value, adapter)
    assert result['status'] == 'already_absent' and result['removed_bytes'] == 0 and not adapter.index['retired']


def test_delete_close_without_remote_absence_is_not_acknowledged():
    peer, value, _, adapter = setup()
    peer.removal = False
    with pytest.raises(ValueError): run(peer, value, adapter)
    assert adapter.index['retired']


class RecordAdapter(rotation.MacStreamReceiptAdapter):
    """TEST ONLY replacement for root approval; owner/FD IO and validator real."""
    def __init__(self, root, bound):
        self.directory, self.bound = root, bound
        super().__init__(SimpleNamespace(root=Path('/Library/TianmingERP/instance')))
    @contextmanager
    def records(self):
        fd = os.open(self.directory, os.O_DIRECTORY | os.O_RDONLY)
        try: yield SimpleNamespace(validate=existing_validate, receipt_name=receipt_name, matches_receipt_name=matches_receipt_name), self.bound, SimpleNamespace(fd=fd), fd
        finally: os.close(fd)


def test_real_receipt_validator_owner_reader_publish_replay_and_conflict(tmp_path):
    from desktop_assistant import mac_backup_storage as records
    _, value, versions, _ = setup()
    version, receipt = versions[0], versions[0]['receipt']
    bound = {'value': value, 'control': {'context_sha256': value['context_sha256'], 'contract_sha256': contract_digest(value),
                                      'package_sha256': receipt['package_sha256']},
             'context': {'uid': os.geteuid(), 'gid': os.getegid()}, 'control_sha': receipt['control_sha256']}
    raw = records.canonical(receipt)
    p = {'receipt': receipt_name(receipt['filename']), 'receipt_sha256': hashlib.sha256(raw).hexdigest()}
    receipt_path = tmp_path / p['receipt']
    receipt_path.write_bytes(raw)
    receipt_path.chmod(0o600)
    adapter = RecordAdapter(tmp_path, bound)
    report = report_for(receipt, p['receipt_sha256'])
    adapter.publish_recovery(p, report)
    target = tmp_path / rotation.recovery_name(receipt,p['receipt_sha256'])
    before, inode = target.read_bytes(), target.stat().st_ino
    adapter.publish_recovery(p, report)
    assert target.read_bytes() == before and target.stat().st_ino == inode
    assert adapter.load_verified(p)['source_time'] == report['source_time']
    changed = deepcopy(report)
    changed['completed_at'] = '2026-10-13T13:00:00+08:00'
    with pytest.raises(ValueError, match='different recovery report'): adapter.publish_recovery(p, changed)
    assert target.read_bytes() == before


def test_real_fd_collector_reads_registration_and_staging_instead_of_accepting_empty_template(tmp_path):
    from desktop_assistant import mac_backup_storage as records
    _, value, versions, adapter = setup()
    bound = {'value': value, 'control': {'contract_sha256': contract_digest(value)},
             'context': {'uid': os.geteuid(), 'gid': os.getegid()}}
    producer = RecordAdapter(tmp_path, bound)
    state = adapter.state()
    for name, content in (('state.json', state), ('retention-jobs.json',
                           {'type': 'tianming.nas-registered-jobs.v1', 'contract_sha256': contract_digest(value), 'jobs': {}})):
        file = tmp_path / name
        file.write_bytes(records.canonical(content)); file.chmod(0o600)
    staging = tmp_path / 'staging'
    staging.mkdir(mode=0o700)
    assert producer.active_references(state)['uploads'] == []
    (staging / ('a' * 32)).mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unregistered staging'): producer.active_references(state)


def test_existing_ciphertext_fd_transfer_and_independent_authentication_do_not_build_or_stop(tmp_path):
    from desktop_assistant.storage import encrypt_file, sha
    from desktop_assistant.nas_full_backup_retention import digest
    import json, zipfile
    peer = Peer()
    value = contract()
    manifest = {'type': 'tianming.recovery.v1', 'created': '2026-10-13T01:02:03+08:00',
                'database': {'revision': 'fixture', 'counts': {'users': 1}}, 'files': {}}
    raw = tmp_path / 'local-authenticated.zip'
    with zipfile.ZipFile(raw, 'w') as archive: archive.writestr('manifest.json', json.dumps(manifest))
    source, downloaded, checked = (tmp_path / name for name in ('existing.tmbackup', 'download.tmbackup', 'checked.zip'))
    encrypt_file(raw, source, 'synthetic-transfer-password'); source.chmod(0o600)
    filename = '20261013-010203-abcdef01.tmbackup'
    with source.open('rb') as src, downloaded.open('x+b') as dst:
        downloaded.chmod(0o600)
        with files.open_archive(peer.tree(), value) as upload, files.open_archive(peer.tree(), value) as download:
            result = stream.transfer_existing_ciphertext(upload, download, src.fileno(), dst.fileno(), filename,
                                                          source.stat().st_size, sha(source), 'd' * 32)
    assert result['upload']['connection_guid'] != result['download']['connection_guid']
    local_proof = {'cipher_sha256': sha(source), 'raw_sha256': sha(raw), 'manifest_sha256': digest(manifest),
                   'database': manifest['database'], 'source_time': manifest['created']}
    assert stream.authenticate_existing_readback(downloaded, checked, 'synthetic-transfer-password', local_proof) == manifest
    assert sha(source) == sha(downloaded)


def test_control_bound_receipt_alias_does_not_accept_caller_chosen_names():
    from desktop_assistant.mac_smb_stream_receipt import reauthenticated_receipt_name
    value={'filename':'20261014-070840-4086632c.tmbackup','control_sha256':'a'*64}
    alias=reauthenticated_receipt_name(value)
    assert matches_receipt_name(value,alias)
    assert matches_receipt_name(value,receipt_name(value['filename']))
    assert not matches_receipt_name(value,alias.replace('a'*64,'b'*64))
    rotation._validate_pointer({'receipt':alias,'receipt_sha256':'c'*64})
    with pytest.raises(ValueError):rotation._validate_pointer({'receipt':alias.replace('a'*64,'a'*8),'receipt_sha256':'c'*64})


def test_recovery_legacy_name_only_reads_the_same_receipt_identity(tmp_path):
    from desktop_assistant import mac_backup_storage as records
    _,value,versions,_=setup();data=versions[0]['receipt']
    bound={'value':value,'control':{'context_sha256':value['context_sha256'],'contract_sha256':contract_digest(value),
        'package_sha256':data['package_sha256']},'context':{'uid':os.geteuid(),'gid':os.getegid()},'control_sha':data['control_sha256']}
    raw=records.canonical(data);p={'receipt':receipt_name(data['filename']),'receipt_sha256':hashlib.sha256(raw).hexdigest()}
    path=tmp_path/p['receipt'];path.write_bytes(raw);path.chmod(0o600)
    report=report_for(data,p['receipt_sha256'])
    legacy=tmp_path/('retention-recovery-'+data['sha256']+'.json');legacy.write_bytes(records.canonical(report));legacy.chmod(0o600)
    adapter=RecordAdapter(tmp_path,bound)
    assert adapter.load_verified(p)['source_time']==report['source_time']
    report['receipt_sha256']='0'*64;legacy.write_bytes(records.canonical(report));legacy.chmod(0o600)
    with pytest.raises(ValueError,match='not bound'):adapter.load_verified(p)


@pytest.mark.parametrize('pointer_alias',[False,True])
@pytest.mark.parametrize('reference_alias',[False,True])
@pytest.mark.parametrize('upper',[False,True])
def test_original_or_control_alias_reference_protects_same_cipher(pointer_alias,reference_alias,upper):
    from desktop_assistant.mac_smb_stream_receipt import reauthenticated_receipt_name
    peer,value,versions,adapter=setup();oldest=versions[0]
    original=oldest['pointer']['receipt'];alias=reauthenticated_receipt_name(oldest['receipt'])
    if pointer_alias:
        adapter.versions.pop(original);oldest['pointer']['receipt']=alias;adapter.versions[alias]=oldest
    reference=alias if reference_alias else original
    adapter.active['active_receipts']=[reference.upper() if upper else reference]
    with pytest.raises(ValueError):run(peer,value,adapter)
    assert not any(event[0]=='delete-held' for event in peer.events)
    assert 'BoxERP\\mac-server-backups\\'+oldest['receipt']['filename'] in peer.nodes
    assert adapter.index['retired']==[oldest['pointer']]


def test_unrelated_control_alias_does_not_claim_same_cipher():
    from desktop_assistant.mac_smb_stream_receipt import reauthenticated_receipt_name
    peer,value,versions,adapter=setup();oldest=versions[0]
    unrelated=dict(oldest['receipt'],control_sha256='0'*64)
    adapter.active['active_receipts']=[reauthenticated_receipt_name(unrelated)]
    assert run(peer,value,adapter)['status']=='removed'
    assert not adapter.index['retired']

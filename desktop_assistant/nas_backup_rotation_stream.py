"""One indexed retired SMB package, held-handle deletion on approved connection.

No sockets, credentials, tree creation, scans, directory removal or local-cache
fallback. The signed existing stream worker supplies its authenticated Archive.
Current/previous verified packages remain held without share-write/share-delete
while the one retired package is hashed and marked deleted on its SAME FileId.
"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import zipfile

from . import nas_backup_rotation as rotation


def transfer_existing_ciphertext(upload_archive, readback_archive, source_fd, destination_fd,
                                 filename, size, cipher_sha, attempt_id):
    """Approved background worker transfers an EXISTING authenticated ciphertext.

    No manager backup builder, database read, packing or stop/start. The existing
    files APIs bind private FDs, distinct approved connections, full bytes and
    directory/file identities. Descriptors remain caller-owned. Caller then
    authenticates this actual download and performs the isolated restore below.
    """
    from . import mac_smb_stream_backup as existing
    from . import mac_smb_stream_contract as contract
    from . import mac_smb_stream_files_v2 as files
    rotation.require(contract.digest(upload_archive.value) == contract.digest(readback_archive.value), 'transfer contract changed')
    before = files._local(source_fd, writable=False)
    rotation.require(before[1][0] == size, 'existing ciphertext size differs from local proof')
    upload = upload_archive.upload(source_fd, filename, size, cipher_sha, attempt_id)
    download = readback_archive.download(destination_fd, upload)
    transfer = {'upload': upload, 'download': download}
    existing._proofs(upload_archive.value, {'encrypted': Path(filename), 'size': size, 'sha256': cipher_sha}, transfer)
    rotation.require(files._local(source_fd, writable=False) == before, 'existing ciphertext changed during upload')
    return transfer


def authenticate_existing_readback(downloaded, raw_zip, password, local_proof):
    """Authenticate downloaded bytes against actual pre-cutover local proof.

    The signed owner loads that local proof through its existing protected report
    reader. No success Boolean is accepted. Full Manager.restore and compact
    capture must still run on this same download before rotation eligibility.
    """
    from .storage import decrypt_file, sha
    fields = {'cipher_sha256', 'raw_sha256', 'manifest_sha256', 'database', 'source_time'}
    rotation.require(isinstance(local_proof, dict) and set(local_proof) == fields
                     and all(rotation._sha(local_proof[k]) for k in ('cipher_sha256', 'raw_sha256', 'manifest_sha256')),
                     'existing local recovery proof incomplete')
    observed = decrypt_file(downloaded, raw_zip, password)
    rotation.require(observed == local_proof['cipher_sha256'] and sha(raw_zip) == local_proof['raw_sha256'],
                     'independent download differs from authenticated local ciphertext/archive')
    with zipfile.ZipFile(raw_zip) as archived: manifest = json.loads(archived.read('manifest.json'))
    rotation.require(rotation.digest(manifest) == local_proof['manifest_sha256']
                     and manifest.get('database') == local_proof['database']
                     and manifest.get('created') == local_proof['source_time'], 'original business point/manifest changed')
    return manifest


def _hold(archive, receipt, *, retire=False):
    from . import mac_smb_stream_contract as contract
    from . import mac_smb_stream_files_v2 as files
    from smbprotocol.open import Open
    rotation.require(receipt['contract_sha256'] == contract.digest(archive.value), 'remote contract differs')
    archive.check_directories()
    upload = receipt['transfer']['upload']
    rotation.require(upload['directories'] == [identity for _, identity in archive.directories], 'remote parent identity changed')
    path = contract.remote_path(archive.value, receipt['filename'])
    if not retire:
        handle, initial = archive._open(path)
    else:
        archive._live()
        handle = Open(archive.tree, path)
        try:
            handle.create(impersonation_level=2, desired_access=0x80 | 0x1 | 0x10000,
                          file_attributes=0x80, share_access=0, create_disposition=1,
                          create_options=0x200000 | 0x40, oplock_level=0)
            rotation.require(handle.connected and handle.create_action == 1, 'retired object not opened exclusively')
            initial = files._query(handle)
            handle._tianming_v2_held_identity = (handle.file_id, initial['index'])
        except BaseException:
            if handle.connected: handle.close()
            raise
    try:
        rotation.require(initial['directory'] is False and initial['size'] == receipt['size']
                         and files._identity(initial) == upload['file'], 'remote object identity changed')
        return handle, initial
    except BaseException:
        if handle.connected: handle.close()
        raise


def _hash_held(archive, handle, initial, receipt):
    from . import mac_smb_stream_files_v2 as files
    result, offset = hashlib.sha256(), 0
    chunk = min(files.CHUNK, archive.connection.max_read_size)
    rotation.require(type(chunk) is int and chunk > 0, 'invalid remote read limit')
    while offset < receipt['size']:
        archive._live()
        length = min(chunk, receipt['size'] - offset)
        data = handle.read(offset, length)
        rotation.require(type(data) is bytes and 0 < len(data) <= length, 'remote read incomplete')
        result.update(data)
        offset += len(data)
    final = files._query(handle)
    rotation.require(files._identity(final) == files._identity(initial) and final['size'] == initial['size']
                     and result.hexdigest() == receipt['sha256'], 'held ciphertext changed')


def _delete_held(archive, handle):
    # Same MS-FSCC disposition request as existing probe_v2, but authorized only
    # for a receipt/index-bound retired FileId opened with DELETE and share=0.
    from smbprotocol.file_info import FileDispositionInformation
    from smbprotocol.open import SMB2SetInfoRequest, SMB2SetInfoResponse
    info = FileDispositionInformation()
    info['delete_pending'] = True
    request = SMB2SetInfoRequest()
    request['file_id'] = handle.file_id
    request['info_type'] = info.INFO_TYPE
    request['file_info_class'] = info.INFO_CLASS
    request['buffer'] = info.pack()
    sent = archive.connection.send(request, sid=archive.tree.session.session_id, tid=archive.tree.tree_connect_id)
    response = archive.connection.receive(sent, resolve_symlinks=False)
    parsed = SMB2SetInfoResponse()
    rotation.require(parsed.unpack(response['data'].get_value()) == b'' and parsed['structure_size'].get_value() == 2,
                     'remote delete disposition not acknowledged')
    rotation.require(handle.close() is not None, 'remote delete close not confirmed')


def _absence(archive, receipt):
    from . import mac_smb_stream_contract as contract
    from smbprotocol.exceptions import ObjectNameNotFound
    try: handle, _ = archive._open(contract.remote_path(archive.value, receipt['filename']))
    except ObjectNameNotFound:
        archive.check_directories()
        return
    try: rotation.require(False, 'remote deletion absence not confirmed')
    finally:
        if handle.connected: handle.close()


def execute_next(adapter, archive):
    """Precise signed-worker hook: process at most one persistent retired entry.

    Remote byte IO occurs outside the manager lock; only final fresh reference /
    CAS checks and bounded SET_INFO/CLOSE occur under it. Service stays running.
    Failure or index change leaves retirement queued. Crash after deletion is
    idempotently completed by authenticated absence on the next run.
    """
    from . import mac_smb_stream_contract as contract
    from smbprotocol.exceptions import ObjectNameNotFound
    index_sha, index = adapter.read_index()
    if not index['retired']: return {'status': 'idle', 'removed_bytes': 0}
    rotation.require(len(index['kept']) == 2, 'two retained complete versions required')
    pointer = index['retired'][0]
    kept = [adapter.load_verified(p) for p in index['kept']]
    retired = adapter.load_verified(pointer)
    receipts = [v['receipt'] for v in (*kept, retired)]
    rotation.require(len({r['sha256'] for r in receipts}) == 3, 'distinct indexed versions required')
    rotation.require(index['contract_sha256'] == contract.digest(archive.value), 'approved remote archive differs')
    state = adapter.state()
    captured_roles = rotation.references(state)
    active = adapter.active_references(state)
    target_sha = retired['receipt']['sha256']
    rotation.require(not rotation._protected_version(retired, state, active), 'retired package remains runtime source')
    missing = False
    with ExitStack() as held:
        # Exact handles veto concurrent overwrite/rename/delete of BOTH survivors.
        for version in kept:
            handle, initial = _hold(archive, version['receipt'])
            held.callback(lambda h=handle: h.close() if h.connected else None)
            _hash_held(archive, handle, initial, version['receipt'])
        try:
            target, initial = _hold(archive, retired['receipt'], retire=True)
            held.callback(lambda: target.close() if target.connected else None)
            _hash_held(archive, target, initial, retired['receipt'])
        except ObjectNameNotFound:
            missing = True
        with adapter.manager.lock():
            fresh_sha, fresh = adapter.read_index()
            rotation.require(fresh_sha == index_sha and fresh == index, 'index changed during remote read')
            current = adapter.state()
            fresh_active = adapter.active_references(current)
            rotation.require(rotation.references(current) == captured_roles and fresh_active == active
                             and not rotation._protected_version(retired, current, fresh_active),
                             'runtime/backup references changed')
            archive.check_directories()
            if not missing: _delete_held(archive, target)
            _absence(archive, retired['receipt'])
            # CAS acknowledgement shares the already-held manager lock; no
            # nested lock helper. Queue survives any failure before this point.
            updated = dict(index, generation=index['generation'] + 1, retired=index['retired'][1:])
            saved = adapter.cas_index(index_sha, updated)
    return {'status': 'already_absent' if missing else 'removed', 'filename': retired['receipt']['filename'],
            'removed_bytes': 0 if missing else retired['receipt']['size'], 'index_sha256': saved}

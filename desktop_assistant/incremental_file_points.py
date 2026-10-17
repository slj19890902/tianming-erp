"""Encrypted content-addressed business files paired with online DB snapshots.

This is a maintenance format, not a legacy .tmbackup. Restore is deliberately
limited to a fresh, isolated directory; it never writes to the live instance.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from desktop_assistant import data_recovery
from desktop_assistant.storage import (decrypt_file, encrypt_file, extract_verified,
    sha, signed_release_manifest, database_info)

TYPE = 'tianming.incremental-file-set.v1'
HEX = re.compile('[0-9a-f]{64}')


def fingerprint(shared):
    inventory = data_recovery.shared_inventory(shared)
    result = {}
    for name in inventory:
        if data_recovery.included(Path(name)):
            s = (shared/name).stat()
            if s.st_nlink != 1:
                raise ValueError('Business file has an external hard-link alias')
            result[name] = [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
    return result


def safe_name(name):
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or p.as_posix() != name or '..' in p.parts
            or '\\' in name or not data_recovery.included(Path(name))):
        raise ValueError('Unsafe business-file member')
    return p


def read_set(store, descriptor, password, work):
    name = descriptor['name']
    if not re.fullmatch(r'[0-9a-f]{64}\.tma', name):
        raise ValueError('Invalid attachment-set identity')
    source = store/name
    if source.is_symlink() or sha(source) != descriptor['sha256']:
        raise ValueError('Attachment-set ciphertext changed')
    raw = work/'manifest.json'
    decrypt_file(source, raw, password)
    if sha(raw) != name[:-4] or raw.stat().st_size > 16*1024**2:
        raise ValueError('Attachment-set manifest identity changed')
    value = json.loads(raw.read_bytes())
    if value.get('type') != TYPE or not isinstance(value.get('files'), dict):
        raise ValueError('Attachment-set format mismatch')
    for relative, row in value['files'].items():
        safe_name(relative)
        if (not HEX.fullmatch(row['plain_sha256']) or not HEX.fullmatch(row['cipher_sha256'])
                or not isinstance(row['bytes'], int) or row['bytes'] < 0):
            raise ValueError('Invalid encrypted-object identity')
    return value


def capture(shared, store, password):
    """Verify every reused cipher; encrypt and independently read back changes."""
    shared, store = Path(shared), Path(store)
    before = fingerprint(shared)
    objects = store/'objects'
    objects.mkdir(mode=0o700, exist_ok=True)
    if objects.is_symlink():
        raise ValueError('Object store is a link')
    index_path = store/'object-index.json'
    index = json.loads(index_path.read_bytes()) if index_path.exists() else {}
    rows, reused, created = {}, 0, 0
    with tempfile.TemporaryDirectory(prefix='file-point-', dir=store) as temporary:
        work = Path(temporary)
        for number, (name, fp) in enumerate(before.items()):
            safe_name(name)
            source = shared/name
            # Reading source content again detects changes even when timestamps
            # were deliberately restored. The store never trusts a path cache.
            plain = sha(source)
            target = objects/(plain+'.tmobj')
            if target.exists():
                expected = index.get(plain)
                if target.is_symlink():
                    raise ValueError('Existing object lacks its verified cipher identity')
                if expected is None:
                    # A previous failed attempt can leave an authenticated object
                    # before the index commit. Recover it by full decryption.
                    check = work/(str(number)+'.orphan-check')
                    expected = decrypt_file(target, check, password)
                    if sha(check) != plain or check.stat().st_size != fp[2]:
                        raise ValueError('Unindexed encrypted object failed readback')
                    check.unlink()
                    index[plain] = expected
                if sha(target) != expected:
                    raise ValueError('Existing object ciphertext changed')
                cipher = expected
                reused += 1
            else:
                encrypted, check = work/(str(number)+'.cipher'), work/(str(number)+'.plain')
                encrypt_file(source, encrypted, password)
                cipher = decrypt_file(encrypted, check, password)
                if sha(check) != plain or check.stat().st_size != fp[2]:
                    raise ValueError('Changed-file independent readback failed')
                check.unlink()
                os.chmod(encrypted, 0o600)
                with encrypted.open('rb') as f: os.fsync(f.fileno())
                os.rename(encrypted, target)
                index[plain] = cipher
                created += 1
            rows[name] = dict(plain_sha256=plain, cipher_sha256=cipher, bytes=fp[2])
        if fingerprint(shared) != before:
            raise ValueError('Business files changed during incremental capture')
        value = dict(type=TYPE, files=rows)
        raw = work/'set.json'
        raw.write_bytes(json.dumps(value, sort_keys=True, separators=(',',':')).encode())
        identity = sha(raw)
        target = store/(identity+'.tma')
        if target.exists():
            descriptor = dict(name=target.name, sha256=sha(target))
            if read_set(store, descriptor, password, work) != value:
                raise ValueError('Existing attachment set does not match')
        else:
            encrypted = work/'set.cipher'
            encrypt_file(raw, encrypted, password)
            check = work/'set.check'
            cipher = decrypt_file(encrypted, check, password)
            if check.read_bytes() != raw.read_bytes():
                raise ValueError('Attachment-set readback failed')
            os.chmod(encrypted, 0o600)
            with encrypted.open('rb') as f: os.fsync(f.fileno())
            os.rename(encrypted, target)
            descriptor = dict(name=target.name, sha256=cipher)
        pending = work/'index.json'
        pending.write_bytes(json.dumps(index, sort_keys=True).encode())
        os.chmod(pending, 0o600)
        with pending.open('rb') as f: os.fsync(f.fileno())
        os.replace(pending, index_path)
        for directory in (objects,store):
            descriptor_fd=os.open(directory,os.O_RDONLY)
            try:os.fsync(descriptor_fd)
            finally:os.close(descriptor_fd)
    return descriptor, before, dict(new_objects=created, reused_objects=reused,
                                    files=len(rows), bytes=sum(r['bytes'] for r in rows.values()))


def garbage_collect(store, points, password):
    """Called only after a new paired point was verified and receipt persisted."""
    references = set()
    sets = {}
    for point in points:
        descriptor = point.get('attachment_set')
        if descriptor:
            sets[descriptor['name']] = descriptor
    if not sets:
        return []
    for descriptor in sets.values():
        with tempfile.TemporaryDirectory(prefix='verify-set-', dir=store) as temporary:
            value = read_set(store, descriptor, password, Path(temporary))
            for row in value['files'].values():
                obj = store/'objects'/(row['plain_sha256']+'.tmobj')
                if obj.is_symlink() or sha(obj) != row['cipher_sha256']:
                    raise ValueError('Referenced object changed; no rotation')
                references.add(obj.name)
    removed = []
    for p in (store/'objects').glob('*.tmobj'):
        if p.name not in references and HEX.fullmatch(p.stem) and not p.is_symlink():
            p.unlink(); removed.append(p.name)
    for p in store.glob('*.tma'):
        if p.name not in sets and HEX.fullmatch(p.stem) and not p.is_symlink():
            p.unlink(); removed.append(p.name)
    # Stale index entries are harmless and cannot authorize objects with a new
    # ciphertext hash. Keep them rather than risking a partial index rewrite.
    return removed


def restore_isolated(point, store, password, target, program, public_key):
    """Reconstruct authenticated DB and files, then validate native recovery.

    Refuses existing paths, live instance paths, and wrong bound programs.
    No service is started and no live database is opened for writing.
    """
    target, store = Path(target), Path(store)
    if (not target.is_absolute() or target.exists() or target.is_symlink()
            or target == Path('/Library/TianmingERP/instance/shared')
            or Path('/Library/TianmingERP').resolve() in target.resolve().parents):
        raise ValueError('Fresh isolated restore target required')
    package = signed_release_manifest(program, public_key)
    if sha(program) != point['program_sha256']:
        raise ValueError('Wrong bound signed program')
    with tempfile.TemporaryDirectory(prefix='paired-restore-', dir=target.parent) as temporary:
        work = Path(temporary)
        source = store/point['name']
        if source.is_symlink() or source.parent != store or sha(source) != point['sha256']:
            raise ValueError('Database point changed')
        decrypt_file(source, work/'database.zip', password)
        metadata = extract_verified(work/'database.zip', work/'payload')
        if (metadata.get('type') != data_recovery.DATABASE_ONLY_TYPE
                or metadata['program']['sha256'] != point['program_sha256']):
            raise ValueError('Database point format/binding mismatch')
        files = read_set(store, point['attachment_set'], password, work)
        shared = work/'payload/shared'
        for relative, row in files['files'].items():
            safe_name(relative)
            destination = shared/relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            obj = store/'objects'/(row['plain_sha256']+'.tmobj')
            if obj.is_symlink() or sha(obj) != row['cipher_sha256']:
                raise ValueError('Attachment ciphertext changed')
            decrypt_file(obj, destination, password)
            if sha(destination) != row['plain_sha256'] or destination.stat().st_size != row['bytes']:
                raise ValueError('Attachment plaintext changed')
        database = shared/data_recovery.DATABASE
        info = database_info(database)
        if info != metadata['database'] or info['revision'] != package['revision']:
            raise ValueError('Isolated database identity mismatch')
        from desktop_assistant.manager import (schema_contract_from_signed_release,
            validate_database_schema, rebind_pdf_sources)
        release, contract = schema_contract_from_signed_release(program, public_key)
        if release != package or not contract or contract.get('revision') != info['revision']:
            raise ValueError('Signed schema contract mismatch')
        validate_database_schema(database, contract)
        rebind_pdf_sources(database, metadata['source_shared'], shared, target)
        from desktop_assistant.preflight import inspect
        answer = inspect(database, shared, managed=True, recorded_root=target)
        if any(answer['counts'][k] for k in ('missing', 'external', 'hash_mismatch')):
            raise ValueError('Isolated business reference validation failed')
        if database_info(database) != info:
            raise ValueError('Database changed during isolated path rebinding')
        validate_database_schema(database, contract)
        os.rename(shared, target)
    return dict(verified=True, files=len(files['files']), database=info, service_started=False)

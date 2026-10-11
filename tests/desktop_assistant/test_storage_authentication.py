"""Real encrypted bytes; no operational backup, keychain or runtime is used."""
import os

import pytest

from desktop_assistant import storage

PASSWORD = 'synthetic-stream-authentication-password'


@pytest.fixture
def encrypted(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(os.urandom(2 * storage.CHUNK + 37))
    backup = tmp_path / 'complete.tmbackup'
    storage.encrypt_file(source, backup, PASSWORD)
    return source, backup


def test_authentication_discards_plaintext_and_uses_the_open_descriptor(encrypted, tmp_path):
    source, backup = encrypted
    before = {p.name for p in tmp_path.iterdir()}
    expected = storage.sha(backup)
    with backup.open('rb') as stream:
        stream.seek(23)  # The authentication API starts at the header itself.
        assert storage.authenticate_stream(stream, PASSWORD) == expected
        assert not stream.closed
    assert {p.name for p in tmp_path.iterdir()} == before
    restored = tmp_path / 'restored'
    assert storage.decrypt_file(backup, restored, PASSWORD) == expected
    assert restored.read_bytes() == source.read_bytes()
    # A held input is not replaced by a later path lookup.
    with backup.open('rb') as stream:
        backup.rename(tmp_path / 'original-encrypted')
        backup.write_bytes(b'unrelated replacement')
        assert storage.authenticate_stream(stream, PASSWORD) == expected
        assert storage.sha(backup) != expected


@pytest.mark.parametrize('damage', ['password', 'header', 'ciphertext', 'tag', 'truncated', 'appended'])
def test_discard_authentication_never_accepts_unauthenticated_bytes(encrypted, tmp_path, damage):
    _, backup = encrypted
    password = PASSWORD
    if damage == 'password':
        password = 'different-synthetic-password'
    else:
        raw = bytearray(backup.read_bytes())
        if damage == 'truncated': raw = raw[:len(storage.MAGIC) + 28]
        elif damage == 'appended': raw += b'extra bytes'
        else: raw[{'header': 0, 'ciphertext': len(storage.MAGIC) + 33, 'tag': -1}[damage]] ^= 1
        backup.write_bytes(raw)
    before = {p.name for p in tmp_path.iterdir()}
    with backup.open('rb') as stream, pytest.raises(ValueError):
        storage.authenticate_stream(stream, password)
    assert {p.name for p in tmp_path.iterdir()} == before
    destination = tmp_path / 'new-output'
    with pytest.raises(ValueError): storage.decrypt_file(backup, destination, password)
    assert not destination.exists()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'dangling', 'directory'])
@pytest.mark.parametrize('password', [PASSWORD, 'different-synthetic-password'])
def test_exclusive_output_failure_preserves_preexisting_content(encrypted, tmp_path, kind, password):
    _, backup = encrypted
    destination = tmp_path / 'already-owned'
    original = tmp_path / 'other-file'
    original.write_bytes(b'original must survive')
    if kind == 'symlink': destination.symlink_to(original)
    elif kind == 'dangling': destination.symlink_to(tmp_path / 'absent')
    elif kind == 'directory': destination.mkdir()
    else: destination.write_bytes(original.read_bytes())
    before = destination.lstat()
    with pytest.raises(FileExistsError):
        storage.decrypt_file(backup, destination, password)
    assert destination.lstat().st_ino == before.st_ino
    assert original.read_bytes() == b'original must survive'
    if kind in ('file', 'symlink'): assert destination.read_bytes() == original.read_bytes()
    if kind == 'directory': assert destination.is_dir()
    assert destination.is_symlink() == (kind in ('symlink', 'dangling'))


def test_failure_cleanup_does_not_delete_a_replacement(encrypted, tmp_path, monkeypatch):
    _, backup = encrypted
    destination = tmp_path / 'output'
    own_partial = tmp_path / 'own-partial'
    decrypt = storage._decrypt_stream
    def replaced(src, password, output=None):
        destination.rename(own_partial)
        destination.write_bytes(b'someone else owns this')
        return decrypt(src, 'different-synthetic-password', output)
    monkeypatch.setattr(storage, '_decrypt_stream', replaced)
    with pytest.raises(ValueError): storage.decrypt_file(backup, destination, PASSWORD)
    assert destination.read_bytes() == b'someone else owns this'
    assert own_partial.exists()


@pytest.mark.parametrize('plaintext', [b'', b'short'])
def test_small_and_empty_plaintext_still_require_gcm_authentication(tmp_path, plaintext):
    source, backup, restored = (tmp_path / name for name in ('source', 'backup', 'restored'))
    source.write_bytes(plaintext)
    storage.encrypt_file(source, backup, PASSWORD)
    with backup.open('rb') as stream:
        assert storage.authenticate_stream(stream, PASSWORD) == storage.sha(backup)
    assert storage.decrypt_file(backup, restored, PASSWORD) == storage.sha(backup)
    assert restored.read_bytes() == plaintext

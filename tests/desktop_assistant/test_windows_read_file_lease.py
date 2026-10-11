"""Native share restrictions use disposable files, never the factory database."""
import os
from pathlib import Path
import sys

import pytest

from desktop_assistant.windows_private_workspace import read_file_lease


@pytest.mark.skipif(sys.platform == 'win32', reason='non-Windows refusal')
def test_non_windows_lease_does_not_create_a_source(tmp_path):
    source=tmp_path/'missing.sqlite3'
    with pytest.raises(ValueError,match='Windows'):
        with read_file_lease(source):pytest.fail('must not yield')
    assert not source.exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='real Windows write/delete sharing; synthetic file')
def test_real_lease_blocks_writes_unlink_and_parent_rename_but_permits_reads(tmp_path):
    parent=tmp_path.resolve()/'leased-parent';parent.mkdir()
    database=parent/'synthetic.sqlite3';database.write_bytes(b'synthetic bytes only')
    with read_file_lease(database):
        assert database.read_bytes()==b'synthetic bytes only'
        with pytest.raises(OSError):
            with database.open('r+b'):pass
        with pytest.raises(OSError):database.unlink()
        with pytest.raises(OSError):parent.rename(parent.with_name('renamed'))
    with database.open('r+b') as stream:stream.write(b'S')
    assert database.read_bytes().startswith(b'Synthetic')


@pytest.mark.skipif(sys.platform != 'win32', reason='real Windows existing writer conflict; synthetic file')
def test_real_existing_writer_prevents_lease_and_cannot_be_mistaken_for_stopped(tmp_path):
    database=tmp_path.resolve()/'synthetic.sqlite3';database.write_bytes(b'synthetic')
    with database.open('r+b'):
        with pytest.raises(OSError):
            with read_file_lease(database):pytest.fail('writer must conflict')
    assert database.read_bytes()==b'synthetic'


@pytest.mark.skipif(sys.platform != 'win32', reason='real Windows lease with immutable SQLite WAL reader; synthetic database')
def test_real_immutable_sqlite_can_read_clean_wal_database_under_lease_without_sidecars(tmp_path):
    from contextlib import closing
    import sqlite3
    database=tmp_path.resolve()/'synthetic-wal.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()==('wal',)
        db.execute('CREATE TABLE synthetic_values(value TEXT)')
        db.execute("INSERT INTO synthetic_values VALUES('fictional-only')")
        db.commit()
    sidecars=[Path(str(database)+suffix) for suffix in ('-wal','-shm','-journal')]
    assert not any(path.exists() for path in sidecars)
    before=database.read_bytes()
    with read_file_lease(database):
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
            assert db.execute('SELECT value FROM synthetic_values').fetchall()==[('fictional-only',)]
            with pytest.raises(sqlite3.OperationalError):
                db.execute("INSERT INTO synthetic_values VALUES('must-not-write')")
        assert not any(path.exists() for path in sidecars)
    assert database.read_bytes()==before
    assert not any(path.exists() for path in sidecars)

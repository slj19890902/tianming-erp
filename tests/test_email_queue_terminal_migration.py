import hashlib
import sqlite3
import pytest
from alembic import command
from tests.test_email_automatic_sync_migration import _config


def test_terminal_migration_roundtrip_facts_and_guard(monkeypatch,tmp_path):
    path=tmp_path/'terminal.sqlite3'
    config=_config(monkeypatch,path)
    command.upgrade(config,'sprep0912')
    command.upgrade(config,'mq0912')
    command.downgrade(config,'sprep0912')
    command.upgrade(config,'mq0912')
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO email_intake_messages(id,mailbox_key,uid_validity,uid,message_id,subject,sender,received,body,status,notice,version) VALUES(1,'fixture','1',1,'','','','','','pending','',1)")
        db.execute("INSERT INTO email_intake_attachments(id,message_id,part_number,filename,sha256,content) VALUES(1,1,1,'fixture.pdf','hash',X'00')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO email_pdf_dispositions VALUES('orphan',999,'deleted',NULL,'2026-09-12',NULL,NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO email_pdf_dispositions VALUES('invalid',1,'pending',NULL,'2026-09-12',NULL,NULL)")
        db.execute("INSERT INTO email_pdf_dispositions VALUES('hash',1,'processed',NULL,'2026-09-12',NULL,NULL)")
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError,match='禁止有损降级'):
        command.downgrade(config,'sprep0912')
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]

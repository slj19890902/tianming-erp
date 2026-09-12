import sqlite3
import pytest
from alembic import command
from tests.test_email_automatic_sync_migration import _config


def test_pdf_cache_roundtrip_and_nonempty_downgrade_guard(monkeypatch,tmp_path):
    path=tmp_path/'queue.sqlite3'
    config=_config(monkeypatch,path)
    command.upgrade(config,'ss31v8x9z93')
    command.upgrade(config,'st32v8x9z94')
    command.downgrade(config,'ss31v8x9z93')
    command.upgrade(config,'st32v8x9z94')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO email_intake_messages(id,mailbox_key,uid_validity,uid,message_id,subject,sender,received,body,status,notice,version) VALUES(1,'fixture','1',1,'','','','','','pending','',1)")
        db.execute("INSERT INTO email_intake_attachments(id,message_id,part_number,filename,sha256,content) VALUES(1,1,1,'test.pdf','hash',X'00')")
        db.execute("INSERT INTO email_pdf_recognitions(sha256,attachment_id,status,recognized_at) VALUES('hash',1,'improve','2026-09-12')")
    with pytest.raises(RuntimeError,match='禁止降级'):
        command.downgrade(config,'ss31v8x9z93')
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert db.execute('SELECT count(*) FROM email_pdf_recognitions').fetchone()==(1,)

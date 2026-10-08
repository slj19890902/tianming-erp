from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from scripts.uat import run_task
from desktop_assistant.storage import sha


@pytest.fixture
def source(tmp_path):
    db=tmp_path/'source.sqlite3'
    with closing(sqlite3.connect(db)) as connection:
        connection.execute('create table evidence(id integer primary key, value text)')
        connection.execute("insert into evidence values(1,'source preserved')")
        connection.commit()
    return db


@pytest.mark.parametrize('code,keep',[(0,False),(1,False),(0,True)])
def test_task_only_recycles_successful_owned_copy(source,tmp_path,monkeypatch,code,keep):
    monkeypatch.setattr('desktop_assistant.cleanup.assert_idle',lambda p:None)
    before=sha(source)
    program="import os,sqlite3,sys; p=os.environ['ERP_DATABASE_PATH']; c=sqlite3.connect(p); c.execute('update evidence set value=?',('test only',)); c.commit(); c.close(); print('evidence retained'); sys.exit("+str(code)+")"
    result=run_task.run_task(source,tmp_path/'runs',[sys.executable,'-c',program],keep_copy=keep)
    assert result['returncode']==code and sha(source)==before
    job=Path(result['job'])
    assert 'evidence retained' in (job/'stdout.log').read_text()
    assert json.loads((job/'result.json').read_text())['status']==('passed' if code==0 else 'failed')
    assert (job/'copies').exists()==(code!=0 or keep)


def test_active_copy_is_preserved_with_receipt(source,tmp_path,monkeypatch):
    monkeypatch.setattr(run_task,'remove_owned_tree',lambda *a: (_ for _ in ()).throw(ValueError('active child')))
    result=run_task.run_task(source,tmp_path/'runs',[sys.executable,'-c','pass'])
    assert result['cleanup']=='retained' and result['cleanup_reason']=='active child'
    assert Path(result['copies']).exists()


def test_existing_roots_and_source_are_never_owned(source,tmp_path,monkeypatch):
    base=tmp_path/'runs';base.mkdir();foreign=base/'existing';foreign.mkdir();(foreign/'important.txt').write_text('keep')
    monkeypatch.setattr('desktop_assistant.cleanup.assert_idle',lambda p:None)
    run_task.run_task(source,base,[sys.executable,'-c','pass'])
    assert (foreign/'important.txt').read_text()=='keep'
    with pytest.raises(ValueError):run_task.run_task(source,run_task.PROJECT/'data',['unused'])


def test_pytest_setup_preserves_managed_isolation(source,tmp_path,monkeypatch):
    monkeypatch.setattr('desktop_assistant.cleanup.assert_idle',lambda p:None)
    paths = json.dumps(sys.path)
    program=(f'import sys; sys.path[:0]={paths}; '
             'from types import SimpleNamespace; import tests.conftest as c; '
             'from app.core.uat_isolation import validate_uat_environment; '
             'c.pytest_configure(None); validate_uat_environment(); '
             'c.pytest_sessionfinish(SimpleNamespace(exitstatus=0),0)')
    result=run_task.run_task(source,tmp_path/'runs',[sys.executable,'-c',program])
    assert result['returncode']==0, (Path(result['job'])/'stderr.log').read_text()
    assert result['cleanup']=='removed'

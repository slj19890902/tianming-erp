import json
from pathlib import Path
import pytest
from desktop_assistant import task_retention as r
from desktop_assistant.storage import write_json


@pytest.fixture
def scene(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'assert_idle',lambda p:None)
    monkeypatch.setattr('desktop_assistant.cleanup.assert_idle',lambda p:None)
    base=tmp_path/'managed';base.mkdir();job=base/('task-'+'a'*32);copies=job/'copies';copies.mkdir(parents=True);(copies/'data.txt').write_text('disposable fixture')
    report=dict(schema='tianming.test-task.v1',job=str(job),copies=str(copies),source=str(tmp_path/'source.sqlite3'),status='passed',keep_copy=False,manual_acceptance_pending=False,copy_identity=r.copy_identity(copies))
    write_json(job/'result.json',report)
    return base,job,copies,report


def test_default_dry_run_explicit_apply_and_replay_leave_report(scene):
    base,job,copies,v=scene
    assert r.reconcile(base)['items'][0]['status']=='eligible' and copies.exists()
    assert r.reconcile(base,apply=True)['items'][0]['status']=='removed' and not copies.exists()
    assert (job/'result.json').exists() and r.reconcile(base,apply=True)['items']==[]


@pytest.mark.parametrize('issue',['failed','keep','pending','changed','unknown','active','source','link','race'])
def test_guards_keep_all_unproved_or_changed_tasks(scene,issue,monkeypatch):
    base,job,copies,v=scene
    if issue in ('failed','keep','pending'):
        v.update({'failed':{'status':'failed'},'keep':{'keep_copy':True},'pending':{'manual_acceptance_pending':True}}[issue]);write_json(job/'result.json',v)
    elif issue=='changed':(copies/'data.txt').write_text('unreviewed change')
    elif issue=='unknown':(base/('task-'+'b'*32)).mkdir()
    elif issue=='active':monkeypatch.setattr(r,'assert_idle',lambda p:(_ for _ in ()).throw(ValueError('active')))
    elif issue=='source':
        other=base/('task-'+'b'*32);other.mkdir();write_json(other/'result.json',{**v,'job':str(other),'copies':str(other/'copies'),'status':'running','source':str(copies/'data.txt')})
    elif issue=='link':(copies/'link').symlink_to(base)
    elif issue=='race':monkeypatch.setattr(r,'assert_idle',lambda p:write_json(job/'result.json',{**v,'status':'running'}))
    out=r.reconcile(base,apply=True);assert copies.exists() and all(x['status']!='removed' for x in out['items'])

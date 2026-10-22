import importlib.util
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch
import shutil
import zipfile
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.api import home_collaboration as api
from app.api.deps import get_db, get_current_user
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.user import User
from app.models.audit import OperationLog
from app.models.home_collaboration import HomeMemo, SharedFile, SharedFileVersion, HomeCollaborationMutation
from desktop_assistant.preflight import inspect


@pytest.fixture
def scene(tmp_path, monkeypatch):
    database=tmp_path/'data/carton_erp.sqlite3';database.parent.mkdir()
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path/'data/private_uploads'))
    engine=create_sqlite_engine(database);Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as db:
        for i,role in [(1,'sales'),(2,'workshop'),(3,'admin')]:
            db.add(User(id=i,username='test'+str(i),real_name='测试'+str(i),password_hash='not-a-login',role=role,is_active=True))
        db.commit()
    app=FastAPI();app.include_router(api.router,prefix='/api/home-tools')
    def session():
        with factory() as db:yield db
    def actor(x_test_actor:int|None=Header(None)):
        if not x_test_actor:raise HTTPException(401,'未登录')
        with factory() as db:return db.get(User,x_test_actor)
    app.dependency_overrides[get_db]=session;app.dependency_overrides[get_current_user]=actor
    with TestClient(app,raise_server_exceptions=False) as client:
        client.headers['X-Test-Actor']='1'
        yield SimpleNamespace(**locals())
    engine.dispose()


def memo(**kw):
    return dict(id=str(uuid4()),request_key=uuid4().hex,expected_version=0,text='核对送货安排',**kw)


def upload(c, content=b'%PDF-1.4\nexample', name='共享资料.pdf', mime='application/pdf', **fields):
    return c.post('/api/home-tools/files',files={'file':(name,content,mime)},data={'request_key':uuid4().hex,**fields})


def test_memo_ownership_replay_conflict_completion_and_privacy(scene):
    c=scene.client;p=memo();a=c.post('/api/home-tools/memos',json=p);assert a.status_code==200
    assert c.post('/api/home-tools/memos',json=p).json()==a.json()
    assert c.post('/api/home-tools/memos',json={**p,'text':'改过'}).status_code==409
    assert c.get('/api/home-tools/memos').json()['active_count']==1
    c.headers['X-Test-Actor']='2'
    assert c.get('/api/home-tools/memos').json()['total']==0
    assert c.post('/api/home-tools/memos',json={**p,'request_key':uuid4().hex,'expected_version':1}).status_code==404
    c.headers['X-Test-Actor']='1'
    q={**p,'request_key':uuid4().hex,'expected_version':1,'completed':True}
    done=c.post('/api/home-tools/memos',json=q);assert done.status_code==200
    assert c.get('/api/home-tools/memos').json()['completed_count']==1
    assert c.get('/api/home-tools/memos?completed=true').json()['items'][0]['completed']
    assert c.post('/api/home-tools/memos',json={**q,'request_key':uuid4().hex}).status_code==409
    assert c.post('/api/home-tools/memos',json={**q,'request_key':uuid4().hex,'expected_version':2,'completed':False}).status_code==200
    with scene.factory() as db:
        assert db.scalar(select(func.count()).select_from(HomeMemo))==1
        assert all(p['text'] not in row.details for row in db.scalars(select(OperationLog)))


def test_memo_failed_commit_keeps_existing_text(scene,monkeypatch):
    c=scene.client;p=memo();assert c.post('/api/home-tools/memos',json=p).status_code==200
    def fail(*args,**kw):raise RuntimeError('audit failure')
    monkeypatch.setattr(api,'_commit',fail)
    assert c.post('/api/home-tools/memos',json={**p,'request_key':uuid4().hex,'expected_version':1,'text':'不应保存'}).status_code==500
    assert c.get('/api/home-tools/memos').json()['items'][0]['text']==p['text']


def test_files_versions_cross_staff_permissions_recycle_and_restore(scene):
    c=scene.client;v1=b'%PDF-1.4\nfirst';v2=b'%PDF-1.4\nsecond'
    r=upload(c,v1);assert r.status_code==200,r.text
    row=r.json();fid=row['id'];url='/api/home-tools/files/'+fid
    c.headers['X-Test-Actor']='2'
    assert c.get('/api/home-tools/files').json()['items'][0]['name']=='共享资料.pdf'
    assert c.get(url+'/content').content==v1
    assert c.post(url+'/state',json=dict(request_key=uuid4().hex,expected_version=1,deleted=True)).status_code==403
    r=upload(c,v2,file_id=fid,expected_version=1);assert r.status_code==200,r.text
    assert r.json()['content_version']==2
    assert c.get(url+'/content?version=1').content==v1
    assert c.get(url+'/content?preview=true').headers['content-disposition'].startswith('inline')
    assert upload(c,file_id=fid,expected_version=1).status_code==409
    assert c.get(url+'/content').content==v2
    c.headers['X-Test-Actor']='1'
    payload=dict(request_key=uuid4().hex,expected_version=2,deleted=True)
    deleted=c.post(url+'/state',json=payload);assert deleted.status_code==200
    assert c.post(url+'/state',json=payload).json()==deleted.json()
    assert c.get('/api/home-tools/files').json()['total']==0
    assert c.get('/api/home-tools/files?deleted=true').json()['total']==1
    c.headers['X-Test-Actor']='2'
    assert c.get('/api/home-tools/files?deleted=true').json()['total']==0
    assert c.get(url+'/content').status_code==404
    assert c.get(url+'/versions').status_code==404
    c.headers['X-Test-Actor']='3'
    assert c.get(url+'/versions').json()['total']==2
    assert c.post(url+'/state',json=dict(request_key=uuid4().hex,expected_version=3,deleted=False)).status_code==200
    assert c.get(url+'/content').content==v2
    assert inspect(scene.database,scene.tmp_path)['counts']==dict(ok=2,missing=0,external=0,hash_mismatch=0)


def test_upload_idempotency_different_bytes_and_failure_preserve_original(scene,monkeypatch):
    c=scene.client;key=uuid4().hex;r=upload(c,request_key=key);row=r.json();assert r.status_code==200
    assert upload(c,request_key=key).json()==row
    assert upload(c,b'%PDF-1.4\nchanged',request_key=key).status_code==409
    def fail(*a,**k):raise RuntimeError('commit failed')
    monkeypatch.setattr(api,'_commit',fail)
    assert upload(c,b'%PDF-1.4\nnew',file_id=row['id'],expected_version=1).status_code==500
    assert c.get('/api/home-tools/files/'+row['id']+'/content').content==b'%PDF-1.4\nexample'
    with scene.factory() as db:
        assert db.scalar(select(func.count()).select_from(SharedFileVersion))==1
        assert db.scalar(select(func.count()).select_from(SharedFile))==1


@pytest.mark.parametrize('name,mime,member',[('表.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','xl/workbook.xml'),
    ('说明.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document','word/document.xml')])
def test_office_content_downloads_exact_bytes_not_inline(scene,name,mime,member):
    out=BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr('[Content_Types].xml','<Types/>');z.writestr(member,'<document/>')
    raw=out.getvalue();r=upload(scene.client,raw,name,mime);assert r.status_code==200,r.text
    data=scene.client.get('/api/home-tools/files/'+r.json()['id']+'/content?preview=true')
    assert data.content==raw and data.headers['content-disposition'].startswith('attachment')
    assert data.headers['cache-control']=='private, no-store'
    assert data.headers['x-content-type-options']=='nosniff'


@pytest.mark.parametrize('name,raw,mime',[('a.html',b'<script>bad</script>','text/html'),('a.pdf',b'<html/>','application/pdf'),
    ('a.png',b'%PDF-1.4','image/png'),('a.docx',b'PK\x03\x04invalid','application/vnd.openxmlformats-officedocument.wordprocessingml.document')])
def test_invalid_upload_cannot_create_reference(scene,name,raw,mime):
    assert upload(scene.client,raw,name,mime).status_code==422
    assert scene.client.get('/api/home-tools/files').json()['total']==0


def test_missing_changed_original_and_real_backup_copy(scene,tmp_path):
    c=scene.client;row=upload(c).json();restored=tmp_path/'restored'
    shutil.copytree(scene.tmp_path/'data',restored/'data')
    assert inspect(restored/'data/carton_erp.sqlite3',restored)['counts']['ok']==1
    with scene.factory() as db:reference=db.scalar(select(SharedFileVersion.reference))
    path=api.resolve_stored_reference(reference);path.write_bytes(b'%PDF-1.4\nbroken!')
    assert c.get('/api/home-tools/files/'+row['id']+'/content').status_code==409
    assert inspect(scene.database,scene.tmp_path)['counts']['hash_mismatch']==1
    path.unlink()
    assert inspect(scene.database,scene.tmp_path)['counts']['missing']==1


def test_anonymous_and_bounded_pagination(scene):
    c=scene.client;c.headers.pop('X-Test-Actor')
    for path in ('memos','files','files/'+str(uuid4())+'/content'):
        assert c.get('/api/home-tools/'+path).status_code==401
    c.headers['X-Test-Actor']='1'
    assert c.get('/api/home-tools/files?limit=500').status_code==422
    assert c.get('/api/home-tools/memos?page=0').status_code==422
    assert c.post('/api/home-tools/memos',json={**memo(),'text':'   '}).status_code==422


def test_migration_roundtrip_keeps_original_schema_and_refuses_new_facts(tmp_path):
    source=Path(__file__).parents[1]/'alembic/versions/ez1022hc_home_collaboration.py'
    spec=importlib.util.spec_from_file_location('home_tools_migration',source);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    engine=create_sqlite_engine(tmp_path/'schema.sqlite3')
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE users(id INTEGER PRIMARY KEY)');db.exec_driver_sql('INSERT INTO users VALUES(1)')
        db.exec_driver_sql('CREATE TRIGGER protect_user BEFORE DELETE ON users BEGIN SELECT RAISE(ABORT,"retain"); END')
        before=db.exec_driver_sql('SELECT name,sql FROM sqlite_master ORDER BY name').all()
        with patch.object(m,'op',Operations(MigrationContext.configure(db))):
            m.upgrade();m.downgrade();assert db.exec_driver_sql('SELECT name,sql FROM sqlite_master ORDER BY name').all()==before
            m.upgrade();db.exec_driver_sql("INSERT INTO home_memos VALUES('test',1,'retain',0,0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
            with pytest.raises(RuntimeError,match='禁止有损降级'):m.downgrade()
            assert db.exec_driver_sql('SELECT text FROM home_memos').scalar_one()=='retain'
    engine.dispose()

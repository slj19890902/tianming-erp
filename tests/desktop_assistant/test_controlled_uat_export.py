"""Private SQLite/FD fixtures only; never load installed identity or data."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from desktop_assistant import uat_export_contract as c, uat_export as w, mac_uat_export as broker
from desktop_assistant import mac_keychain_install as boot, mac_service_manage as manage
from tests.desktop_assistant.test_uat_export_sanitize import snapshot, PASSWORD, MAIL_SECRET


@pytest.fixture
def scene(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve(); tmp_path.chmod(0o700)
    base = tmp_path/'system'; base.mkdir(mode=0o755)
    root = base/'instance'; root.mkdir(mode=0o700)
    for folder in ('shared','staging','control'): (root/folder).mkdir(mode=0o700)
    data = root/'shared/data'; data.mkdir(mode=0o700)
    (data/'layout_runtime').mkdir(mode=0o700)
    (data/'layout_runtime/twin_layout_v1.json').write_text('{"floors":[{"id":"real-runtime-map"}]}')
    seed = tmp_path/'seed'; seed.mkdir(mode=0o700)
    db = snapshot(seed); db.rename(data/'carton_erp.sqlite3'); db = data/'carton_erp.sqlite3'
    with sqlite3.connect(db) as conn:
        for table in ('customers','products','sales_orders'):
            conn.execute('CREATE TABLE '+table+'(id INTEGER PRIMARY KEY)')
        conn.execute('CREATE TABLE alembic_version(version_num TEXT)')
        conn.execute("INSERT INTO alembic_version VALUES('ez1022hc')")
        conn.execute('CREATE TABLE pdf_order_training_samples(id INTEGER PRIMARY KEY,file_path TEXT,file_sha256 TEXT)')
    home = tmp_path/'recipient'; home.mkdir(mode=0o700)
    (home/'ERP-Migration').mkdir(mode=0o700)
    output = home/'ERP-Migration/isolated-sources/delivery-fulfillment-20261022'
    output.parent.mkdir(mode=0o700); output.mkdir(mode=0o700)
    for key,value in {'BASE':base,'ROOT':root,'RECIPIENT_HOME':home,'RECIPIENT_UID':os.getuid(),
                      'OUTPUT':output}.items(): monkeypatch.setattr(c,key,value)
    who = {'uid':os.getuid(),'gid':os.getgid(),'name':'kimiyf','home':str(home)}
    monkeypatch.setattr(c,'recipient',lambda **kwargs:dict(who))
    source = {'application_package_sha256':'a'*64,'application_git_sha':'b'*40,'revision':'ez1022hc',
        'state_sha256':'c'*64,'process_sha256':'d'*64,'environment_sha256':'e'*64,
        'map_relative':'data/layout_runtime/twin_layout_v1.json','pid':1234,'process_created':time.time()-30}
    now = int(time.time())
    plan = {'type':c.PLAN_TYPE,'task':c.TASK,'attempt_id':'1'*32,'created_at':now,'expires_at':now+3600,
        'code_manifest_sha256':'2'*64,'package_sha256':'3'*64,'context_sha256':'4'*64,
        'publisher_sha256':'5'*64,'registry_sha256':'6'*64,'supervisor_package_sha256':'7'*64,
        'control_sha256':'8'*64,'plist_sha256':'9'*64,'recipient':who,'source':source,
        'output':str(c.output('1'*32)),'policy':c.POLICY}
    context = {'uid':os.getuid(),'gid':os.getgid()}
    # Native ownership/ACL is not simulated as a successful cross-UID proof.
    # Pure path/FD/capture logic executes with actual temporary ownership.
    monkeypatch.setattr(boot,'no_acl',lambda fd:None)
    old = os.umask(0o077)
    try: yield {'root':root,'db':db,'source':source,'plan':plan,'context':context,'output':output}
    finally: os.umask(old)


def do_capture(a):
    return w.capture(a['plan'],a['context'],a['source'],lambda:dict(a['source']))


def test_online_capture_preserves_source_has_real_map_and_only_clean_payload(scene):
    a=scene; before=a['db'].read_bytes(); report=do_capture(a)
    payload=c.staging(a['plan']['attempt_id'])/'payload'
    receipt=json.loads((payload/'receipt.json').read_bytes())
    assert a['db'].read_bytes()==before
    assert set(p.name for p in payload.iterdir())=={'database.sqlite3','layout.json','receipt.json'}
    assert receipt['snapshot_sha256']==hashlib.sha256((payload.parent/'source.sqlite3').read_bytes()).hexdigest()
    assert json.loads((payload/'layout.json').read_text())['floors'][0]['id']=='real-runtime-map'
    assert PASSWORD.encode() not in (payload/'database.sqlite3').read_bytes()
    assert MAIL_SECRET.encode() not in (payload/'database.sqlite3').read_bytes()
    assert report['receipt_sha256']==c.digest((payload/'receipt.json').read_bytes())
    assert receipt['source_business_written'] is False and receipt['service_stopped'] is False
    assert receipt['pii_anonymized'] is False


def test_registered_pdf_is_hashed_copied_and_rebound_only_in_private_copy(scene):
    a=scene; folder=a['root']/'shared/data/pdf_training_samples'; folder.mkdir(mode=0o700)
    pdf=folder/'sample.pdf'; pdf.write_bytes(b'%PDF-synthetic-original')
    with sqlite3.connect(a['db']) as conn:
        conn.execute('INSERT INTO pdf_order_training_samples VALUES(1,?,?)',(str(pdf),c.digest(pdf.read_bytes())))
    before=a['db'].read_bytes(); do_capture(a)
    payload=c.staging(a['plan']['attempt_id'])/'payload'
    with sqlite3.connect(payload/'database.sqlite3') as conn:
        rebound=conn.execute('SELECT file_path FROM pdf_order_training_samples').fetchone()[0]
    assert rebound==str(c.output(a['plan']['attempt_id'])/'originals/data/pdf_training_samples/sample.pdf')
    assert (payload/'originals/data/pdf_training_samples/sample.pdf').read_bytes()==pdf.read_bytes()
    assert a['db'].read_bytes()==before
    assert len(json.loads((payload/'receipt.json').read_text())['path_rebindings'])==1


@pytest.mark.parametrize('mutation',[
    lambda p:p.update(output='/tmp/arbitrary'), lambda p:p.update(task='other'),
    lambda p:p.update(attempt_id='../escape'), lambda p:p.update(policy='unknown'),
    lambda p:p.update(expires_at=int(time.time())-1), lambda p:p['recipient'].update(uid=0),
    lambda p:p.update(extra_script='id'), lambda p:p['source'].update(map_relative='../environment.json'),
])
def test_plan_rejects_unbound_values(scene,mutation):
    plan=deepcopy(scene['plan']); mutation(plan)
    with pytest.raises(ValueError): c.validate_plan(plan)


def test_request_and_capability_are_exact_and_not_generic_manage_actions(scene):
    plan=scene['plan']; request={k:plan[k] for k in c.REQUEST_FIELDS-{'action','challenge'}}
    request.update(action='export-uat',challenge='f'*32,plan=plan,plan_sha256=c.digest(c.canonical(plan)))
    c.validate_request(request)
    with pytest.raises(ValueError): c.validate_request({**request,'destination':'/tmp'})
    with pytest.raises(ValueError): c.require_capability({'runtime_capabilities':{c.CAPABILITY:True},'files':dict.fromkeys(c.FILES)})
    c.require_capability({'runtime_capabilities':{c.CAPABILITY:1},'files':dict.fromkeys(c.FILES)})
    assert not c.ACTIONS & manage.system.MANAGE_ACTIONS


def test_capture_refuses_source_cas_change_before_any_staging(scene):
    a=scene; stale={**a['source'],'state_sha256':'f'*64}
    with pytest.raises(ValueError,match='来源身份'): w.capture(a['plan'],a['context'],stale,lambda:stale)
    assert not c.staging(a['plan']['attempt_id']).exists()


def test_late_source_change_leaves_private_work_but_no_completion(scene):
    a=scene
    with pytest.raises(ValueError,match='来源身份变化'):
        w.capture(a['plan'],a['context'],a['source'],lambda:{**a['source'],'pid':9999})
    assert not (c.staging(a['plan']['attempt_id'])/'payload/receipt.json').exists()
    assert not c.output(a['plan']['attempt_id']).exists()


def test_map_change_during_capture_fails_closed(scene,monkeypatch):
    from desktop_assistant import uat_export_sanitize as sanitize
    original=sanitize.sanitize
    def changed(source,target):
        result=original(source,target)
        (scene['root']/'shared'/scene['source']['map_relative']).write_text('{"changed":true}')
        return result
    monkeypatch.setattr(sanitize,'sanitize',changed)
    with pytest.raises(ValueError,match='地图变化'): do_capture(scene)
    assert not (c.staging(scene['plan']['attempt_id'])/'payload/receipt.json').exists()


@pytest.mark.parametrize('ref',['../../data/session_secret.key','data/session_secret.key','/etc/passwd',
                              'private:../session_secret.key','data/private_uploads/../session_secret.key'])
def test_attachment_allowlist_never_reads_secret_or_external_paths(ref):
    with pytest.raises(ValueError): w._source_relative(ref)


def test_duplicate_attempt_preserves_previous_bytes(scene):
    do_capture(scene); payload=c.staging(scene['plan']['attempt_id'])/'payload'
    before={p.name:p.read_bytes() for p in payload.iterdir()}
    with pytest.raises(FileExistsError): do_capture(scene)
    assert {p.name:p.read_bytes() for p in payload.iterdir()}==before


def test_source_link_and_replaced_parent_are_rejected(scene):
    root=scene['root']; target=root/'shared/linked'; target.symlink_to(scene['db'])
    with pytest.raises(OSError):
        with c.regular(target,uid=os.getuid()): pass
    folder=root/'shared/check'; folder.mkdir(); file=folder/'one'; file.write_bytes(b'one')
    with pytest.raises(ValueError,match='变化'):
        with c.regular(file,uid=os.getuid()):
            folder.rename(folder.with_name('moved')); folder.mkdir(); (folder/'one').write_bytes(b'one')


def test_online_snapshot_allows_concurrent_business_writes(scene):
    a=scene; before=sqlite3.connect(a['db']); before.execute('PRAGMA journal_mode=WAL'); before.commit()
    from desktop_assistant.data_recovery import sqlite_snapshot
    destination=c.staging('2'*32); destination.mkdir(mode=0o700)
    running=threading.Event(); stop=threading.Event(); errors=[]
    def writer():
        try:
            with sqlite3.connect(a['db']) as conn:
                running.set()
                for index in range(50):
                    if stop.is_set(): break
                    conn.execute('UPDATE orders SET quantity=quantity+1,amount=amount+2 WHERE id=1'); conn.commit()
                    time.sleep(.001)
        except Exception as error: errors.append(error)
    thread=threading.Thread(target=writer); thread.start(); running.wait(2)
    try: report=sqlite_snapshot(a['db'],destination/'copy.db')
    finally: stop.set(); thread.join(); before.close()
    assert not errors and report['revision']=='ez1022hc'
    with sqlite3.connect(destination/'copy.db') as conn:
        quantity,amount=conn.execute('SELECT quantity,amount FROM orders').fetchone()
        assert amount-5030==2*(quantity-800)


def test_broker_rejects_manifest_unapproved_member_before_output(scene):
    a=scene; capture=do_capture(a); payload=c.staging(a['plan']['attempt_id'])/'payload'
    receipt=json.loads((payload/'receipt.json').read_bytes()); receipt['files']['environment.json']={'sha256':'f'*64,'bytes':0}
    raw=c.canonical(receipt); capture={**capture,'receipt_sha256':c.digest(raw),'files':capture['files']+1}
    with pytest.raises(ValueError,match='未授权'): broker._receipt(raw,a['plan'],capture)
    assert not c.output(a['plan']['attempt_id']).exists()


def _local_broker(scene,monkeypatch):
    # Run genuine FD copying as this test UID. Native root/UID transition is a
    # separate external acceptance gate; no elevated test commands are used.
    def directory(fd,path):
        info=os.fstat(fd)
        assert stat.S_ISDIR(info.st_mode)
        if path==c.OUTPUT or c.OUTPUT in path.parents: assert info.st_mode&0o022==0
    monkeypatch.setattr(broker,'_directory_ok',directory)
    monkeypatch.setattr(boot,'protected',lambda *args,**kwargs:b'')
    def private(fd):
        info=os.fstat(fd)
        assert info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700
    monkeypatch.setattr(broker,'private_directory',private)
    monkeypatch.setattr(broker,'validate',lambda *args:(scene['plan'],{'context':scene['context']}))
    monkeypatch.setattr(broker,'worker',lambda *args:{'source':scene['source']})
    @contextmanager
    def guard(*args): yield
    monkeypatch.setattr(broker,'final_source_guard',guard)


def test_broker_copies_only_verified_payload_and_never_overwrites(scene,monkeypatch):
    a=scene; capture=do_capture(a); _local_broker(a,monkeypatch)
    result=broker.deliver(a['plan'],{'context':a['context']},capture)
    output=c.output(a['plan']['attempt_id'])
    assert result['credential_sanitized'] is True and result['pii_anonymized'] is False
    assert set(p.name for p in output.iterdir())=={'database.sqlite3','layout.json','receipt.json'}
    assert all(p.stat().st_mode&0o077==0 for p in output.iterdir())
    assert PASSWORD.encode() not in (output/'database.sqlite3').read_bytes()
    with pytest.raises(FileExistsError): broker.deliver(a['plan'],{'context':a['context']},capture)


def test_broker_late_hash_failure_never_transfers_root_directory(scene,monkeypatch):
    a=scene; capture=do_capture(a); _local_broker(a,monkeypatch)
    source=c.staging(a['plan']['attempt_id'])/'payload/layout.json'; source.write_bytes(b'changed')
    chowns=[]; monkeypatch.setattr(os,'fchown',lambda fd,uid,gid:chowns.append((fd,uid)))
    # Make fixed parents first; their ownership setup is unrelated to delivery.
    with broker.output_parent(): pass
    chowns.clear()
    with pytest.raises(ValueError,match='复制摘要'): broker.deliver(a['plan'],{'context':a['context']},capture)
    assert not chowns


def test_parent_link_fails_without_capture(scene,monkeypatch):
    a=scene; _local_broker(a,monkeypatch)
    a['output'].rmdir(); a['output'].parent.rmdir(); a['output'].parent.symlink_to(a['root'])
    with pytest.raises(OSError):
        with broker.output_parent(): pass


@pytest.mark.parametrize('fault',['missing','wrong-file-hash','wrong-field'])
def test_clean_verification_and_manifest_hash_are_bound_before_output(scene,monkeypatch,fault):
    a=scene; capture=do_capture(a); payload=c.staging(a['plan']['attempt_id'])/'payload'
    receipt=json.loads((payload/'receipt.json').read_bytes())
    if fault=='missing': receipt['sanitization']={}
    elif fault=='wrong-file-hash': receipt['files']['database.sqlite3']['sha256']='f'*64
    else: receipt['sanitization']['policy']='unknown'
    raw=c.canonical(receipt); (payload/'receipt.json').write_bytes(raw)
    capture={**capture,'receipt_sha256':c.digest(raw)}
    with pytest.raises(ValueError,match='去凭据复核'):
        broker.deliver(a['plan'],{'context':a['context']},capture)
    assert not c.output(a['plan']['attempt_id']).exists()
    assert not (c.BASE/('uat-export-delivery-'+a['plan']['attempt_id'])).exists()


def test_atomic_publish_refuses_concurrent_destination_without_exposure(scene,monkeypatch):
    a=scene; capture=do_capture(a); _local_broker(a,monkeypatch)
    original=broker.publish_directory
    def collision(source,parent,name):
        os.mkdir(name,0o700,dir_fd=parent)
        return original(source,parent,name)
    monkeypatch.setattr(broker,'publish_directory',collision)
    with pytest.raises(OSError): broker.deliver(a['plan'],{'context':a['context']},capture)
    assert list(c.output(a['plan']['attempt_id']).iterdir())==[]
    private=c.BASE/('uat-export-delivery-'+a['plan']['attempt_id'])
    assert stat.S_IMODE(private.stat().st_mode)==0o700
    assert (private/'payload/database.sqlite3').is_file()


def test_final_source_guard_locks_existing_fd_and_rejects_changed_records(scene):
    a=scene; plan=deepcopy(a['plan']); meta={'context':a['context']}
    records={'state.json':'state_sha256','control/process.json':'process_sha256','shared/environment.json':'environment_sha256'}
    for path,key in records.items():
        raw=b'fixed-'+key.encode(); (a['root']/path).write_bytes(raw); plan['source'][key]=c.digest(raw)
    (a['root']/'control/operation.lock').write_bytes(b'0'); plan['source']['pid']=os.getpid()
    import fcntl
    with broker.final_source_guard(plan,meta):
        with (a['root']/'control/operation.lock').open('rb') as stream:
            with pytest.raises(BlockingIOError): fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    (a['root']/'state.json').write_bytes(b'changed')
    with pytest.raises(ValueError,match='来源身份变化'):
        with broker.final_source_guard(plan,meta): pass


def test_shared_original_requires_correct_root_hash_and_size(scene):
    a=scene; private=a['root']/'shared/data/private_uploads/shared_files'; private.mkdir(parents=True)
    item=private/'one.pdf'; item.write_bytes(b'original')
    with sqlite3.connect(a['db']) as db:
        db.execute('CREATE TABLE shared_file_versions(id INTEGER PRIMARY KEY,reference TEXT,sha256 TEXT,size INTEGER)')
        db.execute('INSERT INTO shared_file_versions VALUES(1,?,?,?)',('private:shared_files/one.pdf',c.digest(item.read_bytes()),999))
    with pytest.raises(ValueError,match='冻结大小'): do_capture(a)


def test_worker_recipient_check_does_not_read_private_home(scene,monkeypatch):
    # Inspect the real contract function with controlled console/pwd facts.
    monkeypatch.undo()
    user=SimpleNamespace(pw_name=c.RECIPIENT_NAME,pw_dir=str(c.RECIPIENT_HOME),pw_gid=20)
    monkeypatch.setattr(c.pwd,'getpwuid',lambda _:user)
    real_stat=os.stat
    monkeypatch.setattr(os,'stat',lambda path,*args,**kwargs:SimpleNamespace(st_uid=501) if str(path)=='/dev/console' else real_stat(path,*args,**kwargs))
    monkeypatch.setattr(os,'open',lambda *args,**kwargs:pytest.fail('worker must not traverse recipient home'))
    assert c.recipient(check_home_acl=False)['home_acl_sha256']==c.digest(c.HOME_DENY_DELETE_ACL)


@pytest.mark.parametrize('fault',['none','capability','extra-input','wrong-code'])
def test_export_authentication_retains_kernel_and_signed_entry_guards(scene,monkeypatch,fault):
    plan=scene['plan']; request={k:plan[k] for k in c.REQUEST_FIELDS-{'action','challenge'}}
    request.update(action='inspect-uat',challenge='f'*32)
    if fault=='extra-input': request['destination']='/tmp/arbitrary'
    context={'uid':499,'gid':499}; repo=Path(manage.__file__).resolve().parent.parent
    monkeypatch.setattr(manage.sys,'flags',SimpleNamespace(isolated=True))
    monkeypatch.setattr(manage.sys,'dont_write_bytecode',True)
    monkeypatch.setattr(manage.keychain,'selected',lambda:True)
    monkeypatch.setattr(manage.keychain,'load_context',lambda:context)
    monkeypatch.setattr(boot,'_actual',lambda:boot._expected(499,499))
    meta={'context':context,'public':b'public',**{k:request[k] for k in ('context_sha256','publisher_sha256','registry_sha256')}}
    monkeypatch.setattr(manage.system,'metadata',lambda package:meta)
    manifest={'runtime_capabilities':{'service_manage_v1':1,'system_nightly_v1':1,c.CAPABILITY:1},
              'files':dict.fromkeys(c.FILES)}
    if fault=='capability': manifest['runtime_capabilities'].pop(c.CAPABILITY)
    monkeypatch.setattr(manage.published,'resolve',lambda *args,**kwargs:(repo,manifest))
    script=repo/manage.system.MANAGE_ENTRY
    if fault=='wrong-code': script=repo/'wrong.py'
    if fault=='none': assert manage.authenticated(request,script,repo/'runtime/bin/python3.12')==(context,b'public')
    else:
        with pytest.raises(ValueError): manage.authenticated(request,script,repo/'runtime/bin/python3.12')


def test_source_inspection_binds_runtime_storage_and_never_reads_credentials(scene,monkeypatch):
    a=scene; uid=a['context']['uid']; source=a['source']; root=a['root']; tree=root/'approved-server'
    process={'pid':os.getpid(),'created':time.time()+1,'exe':str(tree/'runtime/bin/python3.12'),'nonce':'f'*32}
    (root/'control/process.json').write_bytes(c.canonical(process))
    (root/'control'/('f'*32+'.ready')).write_text(str(os.getpid()))
    env={'ERP_FILE_STORAGE_DIR':'${SHARED}/data/private_uploads','ERP_SECRET_KEY_FILE':'${SHARED}/data/session_secret.key'}
    (root/'shared/environment.json').write_bytes(c.canonical(env))
    monkeypatch.setattr(manage,'record',lambda *args:(b'current-state',{'current':source['application_package_sha256']}))
    monkeypatch.setattr(w.published,'resolve',lambda *args,**kwargs:(tree,{'git_sha':source['application_git_sha']}))
    from desktop_assistant.manager import Manager
    proc=SimpleNamespace(uids=lambda:SimpleNamespace(real=uid,effective=uid))
    monkeypatch.setattr(Manager,'_recorded_process',lambda *args:(proc,process))
    result=w.inspect_source(a['context'],b'public',{'root':object()},lambda:None)
    assert result['map_relative']=='data/layout_runtime/twin_layout_v1.json'
    for name in ('ERP_FILE_STORAGE_DIR','ERP_LEGACY_UPLOAD_DIR','ERP_PDF_TRAINING_DIR','ERP_INVOICE_ATTACHMENT_DIR','ERP_SUPPLIER_INVOICE_ATTACHMENT_DIR'):
        (root/'shared/environment.json').write_bytes(c.canonical({**env,name:'/tmp/other'}))
        with pytest.raises(ValueError,match='原件运行路径'): w.inspect_source(a['context'],b'public',{'root':object()},lambda:None)
    (root/'shared/environment.json').write_bytes(c.canonical({**env,'ERP_SECRET_KEY':'synthetic-blocked'}))
    with pytest.raises(ValueError,match='内联凭据'): w.inspect_source(a['context'],b'public',{'root':object()},lambda:None)


@pytest.mark.parametrize('extra',[['--observer-uid','0'],['--file','/tmp/arbitrary'],['--management-action','backup']])
def test_admin_cli_rejects_generic_inputs_before_authority_or_io(monkeypatch,extra):
    monkeypatch.setattr(boot.sys,'argv',['installer','uat-export-apply','--export-attempt','a'*32,
        '--approved-sha256','b'*64,*extra])
    monkeypatch.setattr(boot,'verify_code',lambda *args,**kwargs:pytest.fail('unexpected privileged entry'))
    with pytest.raises(SystemExit,match='未确认完成'): boot.main()

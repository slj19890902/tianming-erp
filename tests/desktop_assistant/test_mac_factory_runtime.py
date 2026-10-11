"""Real signed synthetic activation; OS/root identity remains an explicit adapter."""
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest

from desktop_assistant import mac_factory_runtime as runtime, mac_published_runtime as published
from desktop_assistant import factory_activation_contract as contract, factory_cutover as protocol
from desktop_assistant import mac_service_manage as manage, mac_system_service as system
from desktop_assistant import mac_keychain_install as boot, service_supervisor
from desktop_assistant.manager import Manager
from desktop_assistant.storage import read_json,write_json,sha,pack_tree,extract_verified
from app.core import home_rehearsal
from tests.desktop_assistant.test_mac_factory_activation import (
    root_case,target,case,recovery_case,host_store,signed_activation_protocol,native_fixture)

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac protected runtime admission')
TEST_PYTHON=sys.executable
START=Manager.start


@pytest.fixture(autouse=True)
def execution_protocol(signed_activation_protocol,monkeypatch):
    monkeypatch.setattr(native_fixture.publication,'SERVICE_RESTORE_FILES',
        native_fixture.publication.SERVICE_RESTORE_FILES|runtime.EXECUTION_FILES)
    monkeypatch.setattr(runtime,'_admitted',None)


@pytest.fixture
def activated(root_case,monkeypatch):
    c=root_case
    # The activation fixture's OS service is synthetic. Never inspect the host
    # installation, including a real retained root-only provisioning failure.
    monkeypatch.setattr(system,'CONTROL',c.x.base/'system-service.json')
    c.run()
    c.manager=Manager(c.x.root,c.t.c.public)
    monkeypatch.setattr(runtime,'__file__',str(c.x.slot/'server'/runtime.MODULE))
    # Same synthetic OS identity as the full restore/activation fixture. Signed
    # source, protected FD modes and complete published trees are really checked.
    monkeypatch.setattr(runtime,'_identity',lambda:c.x.context)
    return c


def environment(c):
    value=runtime.configuration(c.x.root,c.x.id,c.t.c.public)
    return {**value['network'],'ERP_FACTORY_ACTIVATION_SHA256':value['activation_sha256'],
        'ERP_FACTORY_PACKAGE_SHA256':c.x.id,'ERP_DATABASE_PATH':str(c.x.root/'shared/data/carton_erp.sqlite3'),
        'TM_ERP_CONTROL':str(c.x.root/'control'),'TM_ERP_NONCE':'d'*32,'ERP_WORKERS':'1','ERP_ENVIRONMENT':'production'}


def admit(c):
    runtime.admit_server(script=c.x.slot/'server'/runtime.ENTRY,executable=c.x.slot/'server/runtime/bin/python3.12')


def test_real_activation_drives_manager_overlay_without_rewriting_source(activated,monkeypatch):
    c=activated;source=c.x.root/'shared/environment.json';before=source.read_bytes()
    first=Mock(return_value='sk-synthetic-openai-credential');second=Mock(return_value='sk-synthetic-deepseek-credential')
    monkeypatch.setattr('desktop_assistant.ai_config.load_openai_api_key',first)
    monkeypatch.setattr('desktop_assistant.ai_config.load_deepseek_api_key',second)
    actual=c.manager._environment(c.x.slot/'server')
    assert all(actual[k]==v for k,v in read_json(c.x.base/contract.NAME)['network'].items())
    assert actual['ERP_FACTORY_ACTIVATION_SHA256']==sha(c.x.base/contract.NAME)
    assert actual['ERP_FACTORY_PACKAGE_SHA256']==c.x.id and actual['ERP_WORKERS']=='1'
    assert actual['DEEPSEEK_API_KEY']=='sk-synthetic-deepseek-credential' and 'OPENAI_API_KEY' not in actual
    first.assert_called_once_with(c.x.root);second.assert_called_once_with(c.x.root)
    assert source.read_bytes()==before and c.manager.state['manual_stop'] is True
    assert runtime.configured_mode(c.manager)=='factory'


def test_absent_activation_keeps_home_and_does_not_read_operational_keys(root_case,monkeypatch):
    c=root_case;manager=Manager(c.x.root,c.t.c.public)
    first=Mock(side_effect=AssertionError('credential read'));second=Mock(side_effect=AssertionError('credential read'))
    monkeypatch.setattr('desktop_assistant.ai_config.load_openai_api_key',first)
    monkeypatch.setattr('desktop_assistant.ai_config.load_deepseek_api_key',second)
    actual=manager._environment(c.x.slot/'server')
    assert actual['ERP_HOME_REHEARSAL']=='1' and actual['ERP_BIND_HOST']=='127.0.0.1'
    assert runtime.configured_mode(manager)=='home' and not first.called and not second.called


@pytest.mark.parametrize('fault',['record-mode','record-link','record-hardlink','record-fifo','record-shape','record-signature',
    'context','publisher','registry-context','registry-removes-original','target-host','original-network',
    'original-request','state-current','state-backup','state-binding','future-time'])
def test_invalid_activation_refuses_factory_and_status_is_attention(activated,monkeypatch,fault):
    c=activated;path=c.x.base/contract.NAME
    if fault=='record-mode':path.chmod(0o666)
    elif fault=='record-link':path.rename(path.with_name('preserved.json'));path.symlink_to(path.with_name('preserved.json'))
    elif fault=='record-hardlink':os.link(path,path.with_name('preserved.json'))
    elif fault=='record-fifo':path.rename(path.with_name('preserved.json'));os.mkfifo(path)
    elif fault=='record-shape':write_json(path,{'type':'not authority'})
    elif fault=='record-signature':
        value=read_json(path);auth=json.loads(value['authorization']);auth['body']['source']['source_sha256']='f'*64
        value['authorization']=boot.canonical(auth).decode();value['authorization_sha256']=boot.digest(value['authorization'].encode());write_json(path,value)
    elif fault=='context':
        value=read_json(c.x.base/'keychain-context.json');value['installation_id']='f'*32;write_json(c.x.base/'keychain-context.json',value)
    elif fault=='publisher':(c.x.base/'publisher-public.pem').write_bytes(b'not a public key')
    elif fault.startswith('registry-'):
        p=c.x.base/'published.json';v=read_json(p)
        if fault=='registry-context':v['context_sha256']='f'*64
        else:v['packages']=['f'*64]
        write_json(p,v)
    elif fault=='target-host':monkeypatch.setattr(protocol,'local_target',lambda *a:{**c.t.request['target'],'host_sha256':'f'*64})
    elif fault=='original-network':
        p=c.x.base/('factory-network-'+c.t.request['request_id']+'.json');v=read_json(p);v['ERP_PORT']='8001';write_json(p,v)
    elif fault=='original-request':
        p=c.x.base/('factory-target-'+c.t.request['request_id']+'.json');v=read_json(p);v['package_sha256']='f'*64;write_json(p,v)
    elif fault.startswith('state-'):
        p=c.x.root/'state.json';v=read_json(p)
        if fault=='state-current':v['current']='f'*64
        elif fault=='state-backup':v['restored_backup_sha256']='f'*64
        else:v['native_restore_binding']['factory_cutover']=True
        write_json(p,v);p.chmod(0o600)
    elif fault=='future-time':monkeypatch.setattr(contract,'_now',lambda:c.t.now-timedelta(days=1))
    with pytest.raises(Exception) as error:runtime.configuration(c.x.root,c.x.id,c.t.c.public)
    assert not isinstance(error.value,AssertionError)
    assert runtime.configured_mode(c.manager)=='attention'
    assert service_supervisor.mode_status(c.manager)=={'configured_mode':'attention','home_rehearsal':None}


def test_historical_install_window_does_not_expire_a_running_factory(activated,monkeypatch):
    c=activated;monkeypatch.setattr(contract,'_now',lambda:c.t.now+timedelta(days=100))
    assert runtime.configuration(c.x.root,c.x.id,c.t.c.public)['network']['ERP_HOME_REHEARSAL']=='0'


def test_pinned_server_admission_and_application_allow_only_bound_factory_environment(activated,monkeypatch):
    c=activated
    for k,v in environment(c).items():monkeypatch.setenv(k,v)
    admit(c)
    runtime.require_application(c.x.slot/'server/app/main.py')
    runtime.require_admitted_factory()
    assert not home_rehearsal.enabled()
    # Deliberately no network call. The normal provider gate is now reachable.
    home_rehearsal.require_external_access()


@pytest.mark.parametrize('fault',['mode','network','activation-hash','package','database','control','workers','uat','nonce',
                                  'script','python','module','missing-admission','fork','late-environment','wrong-app'])
def test_direct_or_mutated_server_cannot_gain_factory_access(activated,monkeypatch,fault):
    c=activated
    for k,v in environment(c).items():monkeypatch.setenv(k,v)
    mutations={'mode':('ERP_HOME_REHEARSAL','1'),'network':('ERP_PORT','8001'),
        'activation-hash':('ERP_FACTORY_ACTIVATION_SHA256','f'*64),'package':('ERP_FACTORY_PACKAGE_SHA256','f'*64),
        'database':('ERP_DATABASE_PATH','/tmp/foreign.sqlite3'),'control':('TM_ERP_CONTROL','/tmp/control'),
        'workers':('ERP_WORKERS','2'),'uat':('ERP_UAT_ROOT','/tmp/uat'),'nonce':('TM_ERP_NONCE','invalid')}
    if fault in mutations:monkeypatch.setenv(*mutations[fault])
    if fault=='module':monkeypatch.setattr(runtime,'__file__',str(c.x.base/'unapproved.py'))
    if fault in mutations or fault in {'script','python','module'}:
        with pytest.raises(ValueError):
            runtime.admit_server(script=c.x.base/'bad.py' if fault=='script' else c.x.slot/'server'/runtime.ENTRY,
                executable=TEST_PYTHON if fault=='python' else c.x.slot/'server/runtime/bin/python3.12')
        assert runtime._admitted is None
    else:
        if fault!='missing-admission':admit(c)
        if fault=='fork':runtime._admitted['pid']=-1
        if fault=='late-environment':monkeypatch.setenv('ERP_PORT','8001')
        if fault=='wrong-app':
            with pytest.raises(ValueError):runtime.require_application(c.x.base/'app/main.py')
        else:
            with pytest.raises(ValueError):home_rehearsal.require_external_access()


def test_direct_application_import_refuses_before_legacy_and_database_imports():
    script='''
import sys
from app.core import mac_service_keychain
mac_service_keychain.selected=lambda:True
try: import app.main
except ValueError:
    assert 'main' not in sys.modules and 'app.core.database' not in sys.modules
    print('refused-before-application-imports')
else: raise AssertionError('direct application accepted')
'''
    result=subprocess.run([TEST_PYTHON,'-c',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert 'refused-before-application-imports' in result.stdout


def test_home_guard_cannot_be_disabled_after_it_was_installed(monkeypatch):
    monkeypatch.setattr(home_rehearsal,'_installed',True);monkeypatch.setenv('ERP_HOME_REHEARSAL','0')
    assert home_rehearsal.enabled()
    with pytest.raises(ValueError,match='家庭预演'):home_rehearsal.require_external_access()


def approve_update(c, *, retain_execution=True):
    source=c.t.c.root/'native-source-0'
    meta=deepcopy(c.x.manifest);meta.pop('files');meta['version']='new-signed-native'
    if not retain_execution:meta['runtime_capabilities'].pop('factory_execution_v1')
    package=c.t.c.root/'new-native.zip';pack_tree(source,package,meta,c.t.c.key)
    identity=sha(package);c.manager.stage_release(package)
    slot=c.x.base/'published'/identity;slot.mkdir();shutil.copyfile(package,slot/'package.zip')
    for kind in ('server','migration'):extract_verified(package,slot/kind,c.t.c.public)
    for relative,name in native_fixture.publication.MANAGED.items():
        link=slot/'server'/relative;link.parent.mkdir(parents=True,exist_ok=True);link.symlink_to(c.x.root/'shared'/name)
    for p in slot.rglob('*'):
        if not p.is_symlink():p.chmod(0o755 if p.is_dir() or str(p).endswith('/runtime/bin/python3.12') else 0o644)
    slot.chmod(0o755)
    path=c.x.base/'published.json';value=read_json(path);value['packages']=sorted([c.x.id,identity]);write_json(path,value)
    state=c.manager.state;state['previous']=state['current'];state['current']=identity;state['operation']='update'
    write_json(c.x.root/'state.json',state);(c.x.root/'state.json').chmod(0o600)
    return identity,slot


def test_later_signed_approved_release_keeps_original_cutover_authority(activated):
    c=activated;before=(c.x.base/contract.NAME).read_bytes();identity,slot=approve_update(c)
    for kind in ('server','migration'):
        assert published.resolve(c.x.root,identity,c.t.c.public,kind=kind)[0]==slot/kind
    value=runtime.configuration(c.x.root,identity,c.t.c.public)
    assert value['package_sha256']==identity and value['request_id']==c.t.request['request_id']
    assert (c.x.base/contract.NAME).read_bytes()==before
    assert c.manager.state['native_restore_binding']['package_sha256']==c.x.id


def test_approved_update_cannot_downgrade_factory_execution_protocol(activated):
    c=activated;identity,_=approve_update(c,retain_execution=False)
    for kind in ('server','migration'):
        with pytest.raises(ValueError,match='正式执行协议'):
            published.resolve(c.x.root,identity,c.t.c.public,kind=kind)


def test_actual_manager_start_builds_factory_child_only_after_checks(activated,monkeypatch):
    c=activated
    monkeypatch.setattr('desktop_assistant.ai_config.load_openai_api_key',lambda _:None)
    monkeypatch.setattr('desktop_assistant.ai_config.load_deepseek_api_key',lambda _:None)
    import desktop_assistant.manager as module
    class Probe:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def connect_ex(self,address):assert address==(environment(c)['ERP_BIND_HOST'],int(environment(c)['ERP_PORT']));return 1
    monkeypatch.setattr(module.socket,'socket',Probe)
    calls=[]
    class Child:
        pid=12345
        def poll(self):return None
    def popen(argv,**kw):
        assert argv==[str(c.x.slot/'server/runtime/bin/python3.12'),'-I','-B',str(c.x.slot/'server'/runtime.ENTRY)]
        assert kw['env']['ERP_HOME_REHEARSAL']=='0' and kw['env']['ERP_FACTORY_ACTIVATION_SHA256']==sha(c.x.base/contract.NAME)
        assert kw['cwd']==c.x.slot/'server'
        (c.x.root/'control'/(kw['env']['TM_ERP_NONCE']+'.ready')).write_text('synthetic-ready')
        calls.append(kw['env']);return Child()
    monkeypatch.setattr(module.subprocess,'Popen',popen)
    monkeypatch.setattr(module.psutil,'Process',lambda pid:Mock(create_time=lambda:123.0))
    START(c.manager)
    assert len(calls)==1 and c.manager.state['manual_stop'] is True


def test_public_status_distinguishes_factory_without_reading_credentials(activated,monkeypatch):
    c=activated
    monkeypatch.setattr('desktop_assistant.ai_config.load_openai_api_key',Mock(side_effect=AssertionError('secret read')))
    monkeypatch.setattr('desktop_assistant.ai_config.load_deepseek_api_key',Mock(side_effect=AssertionError('secret read')))
    assert service_supervisor.mode_status(c.manager)=={'configured_mode':'factory','home_rehearsal':False}
    with manage.instance(c.x.context) as (folders,_,__):
        value=manage.snapshot(c.manager,folders['root'],c.x.context)
    assert value['configured_mode']=='factory' and value['manual_stop'] and not value['running']


@pytest.mark.parametrize('fault',['pending','migration','failed','reader','provenance','schema'])
def test_direct_server_entry_preserves_maintenance_schema_and_reader_gates(activated,monkeypatch,fault):
    c=activated
    for k,v in environment(c).items():monkeypatch.setenv(k,v)
    state=c.manager.state
    if fault=='pending':state['onboarding_pending']=True
    elif fault=='migration':state['operation']='migration_running'
    elif fault=='failed':state['operation']='update_failed_reader_contract'
    if fault in {'pending','migration','failed'}:
        write_json(c.x.root/'state.json',state);(c.x.root/'state.json').chmod(0o600)
    elif fault=='reader':monkeypatch.setattr(Manager,'_check_dispatch_reader',Mock(side_effect=ValueError('synthetic reader failure')))
    elif fault=='provenance':monkeypatch.setattr(Manager,'_check_provenance_reader',Mock(side_effect=ValueError('synthetic provenance failure')))
    else:monkeypatch.setattr(Manager,'compatible',lambda *a:False)
    with pytest.raises(ValueError):admit(c)
    assert runtime._admitted is None


def test_supervisor_refreshes_mode_even_when_manual_stop_status_unchanged(tmp_path,monkeypatch):
    from tests.desktop_assistant.test_service_supervisor import Manager as FakeManager
    import threading
    manager=FakeManager(tmp_path);manager.state['manual_stop']=True
    stopping=threading.Event();modes=iter(['home','factory','factory','factory'])
    monkeypatch.setattr(runtime,'configured_mode',lambda _:next(modes))
    writes=[];original=service_supervisor.write_json
    def write(path,value):
        writes.append(value);original(path,value)
        if len(writes)==2:stopping.set()
    monkeypatch.setattr(service_supervisor,'write_json',write)
    service_supervisor.supervise(manager,stopping,interval=0)
    assert writes[0]['status']==writes[1]['status']=='manually-stopped'
    assert writes[0]['home_rehearsal'] is True and writes[1]['home_rehearsal'] is False


def test_stop_remains_available_with_broken_activation_status(monkeypatch,tmp_path):
    from tests.desktop_assistant.test_service_supervisor import Manager as FakeManager
    manager=FakeManager(tmp_path)
    monkeypatch.setattr(runtime,'configured_mode',lambda _:'attention')
    service_supervisor.stop_owned(manager)
    assert manager.stops==1 and service_supervisor.mode_status(manager)['home_rehearsal'] is None


def test_failed_readmission_cannot_leave_previous_process_authority(activated,monkeypatch):
    c=activated
    for k,v in environment(c).items():monkeypatch.setenv(k,v)
    admit(c)
    with pytest.raises(ValueError):runtime.admit_server(script=c.x.base/'foreign.py',executable=c.x.slot/'server/runtime/bin/python3.12')
    assert runtime._admitted is None
    with pytest.raises(ValueError):home_rehearsal.require_external_access()


@pytest.mark.parametrize('fault',[None,'database','control','workers','mode','credential'])
def test_managed_home_admission_stays_bound_and_cannot_promote(root_case,monkeypatch,fault):
    c=root_case;manager=Manager(c.x.root,c.t.c.public)
    monkeypatch.setattr(runtime,'__file__',str(c.x.slot/'server'/runtime.MODULE))
    monkeypatch.setattr(runtime,'_identity',lambda:c.x.context)
    values=manager._environment(c.x.slot/'server')
    values.update(TM_ERP_CONTROL=str(c.x.root/'control'),TM_ERP_NONCE='d'*32)
    for k,v in values.items():monkeypatch.setenv(k,v)
    admit(c)
    assert runtime._admitted['factory'] is False
    if fault is None:
        runtime.require_application(c.x.slot/'server/app/main.py')
        assert home_rehearsal.enabled()
    else:
        mutations={'database':('ERP_DATABASE_PATH','/tmp/foreign.db'),'control':('TM_ERP_CONTROL','/tmp/control'),
            'workers':('ERP_WORKERS','2'),'mode':('ERP_HOME_REHEARSAL','0'),'credential':('OPENAI_API_KEY','synthetic-unapproved')}
        monkeypatch.setenv(*mutations[fault])
        with pytest.raises(ValueError):runtime.require_application(c.x.slot/'server/app/main.py')


def test_cached_application_cannot_skip_admission_when_lifespan_starts(tmp_path):
    script='''
import asyncio,os,sys
from pathlib import Path
root=Path(sys.argv[1]);db=root/'synthetic.sqlite3'
os.environ.update(ERP_DATABASE_PATH=str(db),ERP_ENVIRONMENT='test',ERP_HOME_REHEARSAL='1',
    ERP_SECRET_KEY='synthetic-lifespan-key-for-no-real-data',ERP_FILE_STORAGE_DIR=str(root/'uploads'),
    ERP_PDF_TRAINING_DIR=str(root/'pdf'))
os.environ.pop('ERP_UAT_ROOT',None)
from app.core.home_rehearsal import install_network_guard
install_network_guard()
from app import main as application
from app.core import mac_service_keychain
mac_service_keychain.selected=lambda:True
assert not db.exists()
async def check():
    try:
        async with application.phase2_lifespan(None):
            raise AssertionError('cached application bypassed entry')
    except ValueError as e:
        assert '专用应用' in str(e)
asyncio.run(check())
assert not db.exists() and not (root/'pdf').exists()
print('cached-application-refused-before-storage')
'''
    result=subprocess.run([TEST_PYTHON,'-c',script,str(tmp_path)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert 'cached-application-refused-before-storage' in result.stdout


@pytest.mark.parametrize('fault',['disabled','unregistered','unscheduled','canary'])
def test_factory_start_cannot_bypass_system_disable_or_background_keychain(activated,monkeypatch,fault):
    c=activated
    for k,v in environment(c).items():monkeypatch.setenv(k,v)
    value={'registered':True,'enabled':True,'scheduled_backups':True}
    if fault!='canary':value[{'disabled':'enabled','unregistered':'registered','unscheduled':'scheduled_backups'}[fault]]=False
    monkeypatch.setattr(system,'management_service_state',lambda:value)
    if fault=='canary':monkeypatch.setattr(system,'verify_keychain',Mock(side_effect=ValueError('synthetic canary failure')))
    first=Mock(return_value=None);second=Mock(return_value=None)
    monkeypatch.setattr('desktop_assistant.ai_config.load_openai_api_key',first)
    monkeypatch.setattr('desktop_assistant.ai_config.load_deepseek_api_key',second)
    with pytest.raises(ValueError):c.manager._environment(c.x.slot/'server')
    first.assert_not_called();second.assert_not_called()
    with pytest.raises(ValueError):admit(c)
    assert runtime._admitted is None


def test_running_process_is_not_accepted_after_system_disable(activated,monkeypatch):
    c=activated
    monkeypatch.setattr(system,'management_service_state',lambda:{'registered':True,'enabled':False,'scheduled_backups':False})
    process=Mock(return_value=(None,{'exe':str(c.x.slot/'server/runtime/bin/python3.12')}))
    monkeypatch.setattr(Manager,'_process',process)
    with pytest.raises(ValueError):START(c.manager)
    process.assert_not_called()

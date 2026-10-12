"""Synthetic worker IO and real cold-backup audit; OS identity is adapted.

Never an installed instance, real operational keychain, launchd or NAS.
Transport/update unit adapters are distinguished from the real GCM/ZIP/SQLite
audit and original temporary Ed25519 key used by the integration below.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone
import fcntl
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock,patch

import pytest

from desktop_assistant import current_mac_worker as worker,current_mac_worker_contract as c
from desktop_assistant import current_mac_cutover as codec,current_mac_cutover_contract as v3
from desktop_assistant import mac_service_manage as manage,mac_service_restore as restore
from desktop_assistant import mac_keychain_install as boot,current_mac_target_audit as audit
from desktop_assistant.storage import write_json,sha
from tests.desktop_assistant.test_current_mac_worker_contract import chain
from tests.desktop_assistant.test_current_mac_cutover_contract import h,valid_network,valid_body,at


@pytest.fixture
def synthetic(chain,tmp_path,monkeypatch):
    if sys.platform!='darwin':pytest.skip('Darwin file/ACL adapters; no privileged identity')
    p,q,r,hsh,raw=deepcopy(chain)
    base=tmp_path.resolve()/'base';base.mkdir(mode=0o755);base.chmod(0o755)
    root=base/'instance';root.mkdir(mode=0o700)
    for name in ('releases','shared','control','backups','packages','staging'):(root/name).mkdir(mode=0o700)
    lock=root/'control/operation.lock';lock.write_bytes(b'0');lock.chmod(0o600)
    slot=base/('current-mac-cutover-'+p['update']['attempt_id']);slot.mkdir(mode=0o755);slot.chmod(0o755)
    original_lease=restore.Lease
    def lease(path,**kwargs):
        if kwargs['uid']==0:kwargs['uid']=os.getuid()
        return original_lease(path,**kwargs)
    monkeypatch.setattr(restore,'Lease',lease);monkeypatch.setattr(manage,'Lease',lease)
    monkeypatch.setattr(c,'BASE',str(base))
    for module in (worker,manage):monkeypatch.setattr(module,'BASE',base);monkeypatch.setattr(module,'ROOT',root)
    monkeypatch.setattr(worker,'_entry',lambda:None)
    monkeypatch.setattr(worker.time,'time',lambda:p['update']['created_at']+7)
    monkeypatch.setattr(worker,'datetime',SimpleNamespace(now=lambda tz:at()))
    context={'uid':os.getuid(),'gid':os.getgid()}
    roles={'execution':({'tree':tmp_path/'published/migration'},None,None)}
    auth=Mock(return_value=(context,b'synthetic public',roles));monkeypatch.setattr(worker,'authenticated',auth)
    monkeypatch.setattr(boot,'_actual',lambda:boot._expected(499,499))
    network_raw=c.canonical(valid_network())
    for plan in p.values():plan['network_file_sha256']=c.raw_digest(network_raw)
    def synchronize():
        for phase in c.PHASES:
            hsh[phase]=c.raw_digest(c.canonical(p[phase]))
            q[phase]['plan_sha256']=r[phase]['plan_sha256']=hsh[phase]
        previous=c.canonical(r['update'])
        p['prepare']['update_result_file_sha256']=c.raw_digest(previous)
        hsh['prepare']=c.raw_digest(c.canonical(p['prepare']))
        q['prepare']['plan_sha256']=r['prepare']['plan_sha256']=hsh['prepare']
        for phase in c.PHASES:
            path=slot/(phase+'-plan.json');path.write_bytes(c.canonical(p[phase]));path.chmod(0o644)
        path=slot/'update-result.json';path.write_bytes(previous);path.chmod(0o644)
        path=slot/'network.json';path.write_bytes(network_raw);path.chmod(0o644)
    synchronize()
    calls=[]
    @contextmanager
    def fake_audit(*args,**kwargs):
        calls.append((args,kwargs))
        prep=deepcopy(r['prepare']['result']['preparation'])
        if kwargs.get('expected_preparation') is not None:assert prep==kwargs['expected_preparation']
        active=True
        def check():
            assert active
            return deepcopy(prep)
        yield prep,check
        check();active=False
    monkeypatch.setattr(audit,'verified_current_mac_backup',fake_audit)
    def run(phase='prepare',fd=None):
        return worker.run({**q[phase],'lock_fd':fd},script='synthetic',executable='synthetic')
    return SimpleNamespace(base=base,root=root,slot=slot,p=p,q=q,r=r,hsh=hsh,
        context=context,roles=roles,auth=auth,calls=calls,sync=synchronize,run=run,lock=lock)


def test_prepare_uses_exact_prior_root_reply_and_full_audit_context(synthetic):
    s=synthetic;result=s.run()
    assert result['result']['preparation']==s.r['prepare']['result']['preparation']
    assert s.calls[0][0]==(s.p['prepare']['execution_package_sha256'],
        s.p['prepare']['monitor_package_sha256'],s.p['prepare']['restore_attempt_id'])
    assert not s.calls[0][1].get('inherited_lock') and len(s.calls)==1
    assert not (s.base/'factory-activation.json').exists()


@pytest.mark.parametrize('artifact',['prepare-plan.json','network.json','update-plan.json','update-result.json'])
def test_root_file_symlink_refused_before_audit(synthetic,artifact):
    s=synthetic;path=s.slot/artifact;raw=path.read_bytes();other=s.base/('other-'+artifact)
    other.write_bytes(raw);other.chmod(0o644);path.unlink();path.symlink_to(other)
    with pytest.raises((ValueError,OSError)):s.run()
    assert not s.calls


def test_prior_update_reply_mutation_is_not_reinterpreted(synthetic):
    s=synthetic;path=s.slot/'update-result.json'
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError):s.run()
    assert not s.calls


def test_root_plan_change_during_audit_refuses_output(synthetic,monkeypatch):
    s=synthetic
    @contextmanager
    def changed(*args,**kwargs):
        path=s.slot/'prepare-plan.json';path.write_bytes(path.read_bytes()+b' ')
        prep=s.r['prepare']['result']['preparation']
        yield prep,lambda:prep
    monkeypatch.setattr(audit,'verified_current_mac_backup',changed)
    with pytest.raises(ValueError):s.run()
    assert (s.slot/'prepare-plan.json').read_bytes().endswith(b' ')


@pytest.mark.parametrize('argv',[
    ['--attempt','a'*32,'--plan','a'*64,'--phase','prepare','--challenge','b'*32],
    ['--attempt','a'*32,'--plan-sha256','a'*64,'--phase','prepare','--phase','activate'],
    ['--attempt','a'*32,'--plan-sha256','a'*64,'--phase','prepare','--challenge','b'*32,'--path','/tmp'],
    ['--help']])
def test_cli_never_accepts_abbreviation_extra_path_or_duplicate_flags(argv):
    with pytest.raises(ValueError):worker._arguments(argv)


def test_fixed_cli_has_no_password_or_filesystem_argument():
    args=worker._arguments(['--attempt','a'*32,'--plan-sha256','b'*64,
        '--phase','activate','--challenge','c'*32,'--lock-fd','11'])
    assert set(args)==c.REQUEST_FIELDS and args['lock_fd']==11


@pytest.mark.parametrize('fault',['none','wrong-state','bad-receipt','update-failure'])
def test_update_constructs_result_before_original_manager_lock_releases(synthetic,monkeypatch,fault):
    from desktop_assistant import manager as module,mac_smb_stream_target as stream
    from desktop_assistant import mac_smb_stream_receipt as receipts,credential_store
    s=synthetic;state=dict(current=s.p['update']['current_package_sha256'],manual_stop=True)
    write_json(s.root/'state.json',state);(s.root/'state.json').chmod(0o600)
    s.p['update']['state_before_sha256']=sha(s.root/'state.json');s.sync()
    destination=Path('tianming-smb-stream:'+h(700))
    write_json(s.root/'preferences.json',dict(nas=str(destination),protected_password='tm-keychain-v1:backup:'+'1'*32))
    (s.root/'preferences.json').chmod(0o600)
    monkeypatch.setattr(stream,'approved_destination',lambda ctx:destination)
    unprotect=Mock(return_value='synthetic-backup-secret')
    monkeypatch.setattr(credential_store,'unprotect_backup',unprotect)
    def stopped(self):
        assert self.state['manual_stop'] is True
        return self.state
    events=[]
    original=module.Manager
    class FakeUpdate(original):
        _stopped_update_state=stopped
        def update_stopped(self,package,password,nas):
            assert password=='synthetic-backup-secret' and nas==destination
            with self.lock():
                events.append('inside-original-lock')
                archive=self.root/'backups/full.tmbackup';archive.write_bytes(b'synthetic full archive');archive.chmod(0o600)
                receipts_dir=self.root/'control/backup-receipts';receipts_dir.mkdir(mode=0o700)
                receipt_path=receipts_dir/'receipt.json'
                write_json(receipt_path,dict(sha256=h(912) if fault=='bad-receipt' else sha(archive),
                    size=archive.stat().st_size));receipt_path.chmod(0o600)
                final=self.state
                final.update(current=s.p['update']['execution_package_sha256'],previous=state['current'],
                    operation='update',update_backup=str(archive),last_backup=str(archive),last_backup_at='verified',
                    last_backup_stream=dict(path=str(archive),receipt='receipt.json',receipt_sha256=sha(receipt_path)))
                write_json(self.root/'state.json',final)
                (self.root/'state.json').chmod(0o600)  # Simulate the fixed entry's umask 077.
                if fault=='update-failure':raise ValueError('synthetic update failure; keep archive/state')
            events.append('after-original-lock')
    monkeypatch.setattr(module,'Manager',FakeUpdate)
    monkeypatch.setattr(receipts,'verified_at',lambda root,state:state.get('last_backup_at'))
    @contextmanager
    def retained(root,ctx,name):
        with restore.Lease(root.path/'backups'/name,uid=ctx['uid'],gid=ctx['gid'],mode=0o600) as held:
            yield held
    monkeypatch.setattr(receipts,'copy_lease',retained)
    original_reply=worker._reply
    def checked_reply(*args):
        with s.lock.open('rb') as contender:
            with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
        events.append('reply-under-lock')
        return original_reply(*args)
    monkeypatch.setattr(worker,'_reply',checked_reply)
    if fault=='wrong-state':
        write_json(s.root/'state.json',{**state,'manual_stop':False});(s.root/'state.json').chmod(0o600)
    if fault!='none':
        with pytest.raises(ValueError):s.run('update')
        assert 'reply-under-lock' not in events and not (s.base/'factory-activation.json').exists()
        if fault=='wrong-state':unprotect.assert_not_called();assert not list((s.root/'backups').iterdir())
        else:
            unprotect.assert_called_once();assert (s.root/'backups/full.tmbackup').read_bytes()==b'synthetic full archive'
            assert (s.root/'state.json').exists()
        return
    result=s.run('update')
    assert events==['inside-original-lock','reply-under-lock','after-original-lock']
    assert result['result']['backup_sha256']==hashlib.sha256(b'synthetic full archive').hexdigest()
    assert 'synthetic-backup-secret' not in str(result) and result['manual_stop'] is True


@pytest.fixture
def authentication(synthetic,monkeypatch):
    from desktop_assistant import mac_system_service as system,mac_published_runtime as published
    from desktop_assistant import mac_smb_stream_runtime as stream
    from app.core import mac_service_keychain as keychain
    s=synthetic;plan=s.p['prepare'];context={'uid':499,'gid':499}
    monkeypatch.setattr(keychain,'selected',lambda:True)
    monkeypatch.setattr(keychain,'load_context',lambda:context)
    roles={};by_id={}
    for role in ('execution','monitor','transport'):
        identity=plan[role+'_package_sha256'];tree=s.base/'published'/identity/'migration'
        meta=dict(package=identity,context=context,public=b'synthetic original public',tree=tree,
            **{k:plan[k] for k in ('context_sha256','publisher_sha256','registry_sha256')})
        manifest={'runtime_capabilities':{worker.CAPABILITY:1,audit.CAPABILITY:1,
            audit.EXECUTION_CAPABILITY:1,'service_manage_v1':1,'system_nightly_v1':1},
            'files':{k:'synthetic hash' for k in (worker.MODULE,worker.CONTRACT_MODULE,
                'desktop_assistant/manager.py','desktop_assistant/mac_service_manage.py',
                system.MANAGE_ENTRY,system.MANAGE_MODULE,audit.MODULE)}}
        roles[role]=(meta,tree,manifest);by_id[identity]=roles[role]
    monkeypatch.setattr(system,'metadata',lambda identity:by_id[identity][0])
    monkeypatch.setattr(published,'resolve',lambda root,identity,public,kind:by_id[identity][1:])
    plist=b'synthetic fixed plist';monkeypatch.setattr(system,'_plist',lambda *a:plist)
    control=boot.canonical(system.record_for(roles['monitor'][0],True))
    plan['system_control_sha256']=boot.digest(control);plan['system_plist_sha256']=boot.digest(plist)
    records={system.CONTROL:control,system.PLIST:plist}
    monkeypatch.setattr(system.publication,'read_record',lambda path:records[path])
    monkeypatch.setattr(boot,'protected',lambda *a,**kw:None)
    service_state={'registered':True,'enabled':True,'scheduled_backups':True}
    monkeypatch.setattr(system,'management_service_state',lambda:service_state)
    binding=SimpleNamespace(context=context,control={'package_sha256':plan['transport_package_sha256'],
        'contract_sha256':plan['transport_contract_sha256']},control_sha=plan['transport_control_sha256'],check=Mock())
    monkeypatch.setattr(stream,'load_binding',lambda:binding)
    home=s.base/'home';home.mkdir(mode=0o700)
    original=home/('service-restore-'+plan['restore_attempt_id']+'.json')
    original.write_bytes(b'{"synthetic_original":true}');original.chmod(0o600)
    plan['restore_receipt_sha256']=sha(original)
    current_lease=restore.Lease
    def owned(path,**kwargs):
        if kwargs['uid']==499:kwargs['uid']=os.getuid()
        if kwargs.get('gid')==499:kwargs['gid']=os.getgid()
        return current_lease(path,**kwargs)
    monkeypatch.setattr(restore,'Lease',owned)
    # synthetic fixture replaced authentication; load the original implementation
    # from its source in an isolated module namespace without executing main.
    import importlib.util
    spec=importlib.util.spec_from_file_location('synthetic_worker_auth',Path(worker.__file__))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.BASE=s.base;module.ROOT=s.root;module._entry=lambda:None
    tree=roles['execution'][1];module.__file__=str(tree/worker.MODULE)
    executable=tree/'runtime/bin/python3.12';monkeypatch.setattr(sys,'executable',str(executable))
    def authenticate():
        return module.authenticated(s.q['prepare'],plan,script=module.__file__,executable=sys.executable)
    return SimpleNamespace(s=s,plan=plan,context=context,roles=roles,module=module,records=records,
        service_state=service_state,binding=binding,original=original,authenticate=authenticate)


def test_authentication_requires_fixed_execution_original_receipt_and_enabled_monitor(authentication):
    a=authentication;context,public,roles=a.authenticate()
    assert context==a.context and roles.keys()=={'execution','monitor','transport'}
    assert public==b'synthetic original public';a.binding.check.assert_called_once()


@pytest.mark.parametrize('fault',['wrong-script','wrong-interpreter','worker-cap-bool','audit-cap-missing',
    'wrong-registry','public-divergence','monitor-disabled','transport-changed','restore-changed','factory-record'])
def test_authentication_rejects_execution_or_root_evidence_substitution(authentication,fault,monkeypatch):
    a=authentication
    if fault=='wrong-script':a.module.__file__=str(a.s.base/'unapproved.py')
    elif fault=='wrong-interpreter':monkeypatch.setattr(sys,'executable',str(a.s.base/'unapproved-python'))
    elif fault=='worker-cap-bool':a.roles['execution'][2]['runtime_capabilities'][worker.CAPABILITY]=True
    elif fault=='audit-cap-missing':a.roles['execution'][2]['runtime_capabilities'].pop(audit.CAPABILITY)
    elif fault=='wrong-registry':a.roles['monitor'][0]['registry_sha256']=h(911)
    elif fault=='public-divergence':a.roles['transport'][0]['public']=b'other public'
    elif fault=='monitor-disabled':a.service_state['enabled']=False
    elif fault=='transport-changed':a.binding.control['contract_sha256']=h(912)
    elif fault=='restore-changed':a.original.write_bytes(b'{"substituted":true}')
    else:(a.s.base/'factory-activation.json').write_bytes(b'{}')
    with pytest.raises(ValueError):a.authenticate()


# Reuse the existing complete real signed/GCM/ZIP/SQLite fixture, keeping its
# OS/root/keychain/SMB observation adapters explicit and its five roles distinct.
from tests.desktop_assistant.test_current_mac_target_audit import target,case,recovery_case,host_store


@pytest.mark.parametrize('fault',['none','unlocked-parent','writable-parent','bad-signature','expired','backup-mutated'])
def test_prepare_and_activate_real_current_backup_and_signature_keep_parent_lock(target,monkeypatch,fault):
    t=target;base=t.x.base;root=t.x.root
    for module in (worker,):monkeypatch.setattr(module,'BASE',base);monkeypatch.setattr(module,'ROOT',root)
    monkeypatch.setattr(c,'BASE',str(base));monkeypatch.setattr(worker,'_entry',lambda:None)
    monkeypatch.setattr(worker,'authenticated',lambda *a,**kw:(t.x.context,t.c.public,{}))
    original_reply=worker._reply
    def adapted_reply(*args):
        with patch.object(boot,'_actual',lambda:boot._expected(499,499)):
            return original_reply(*args)
    monkeypatch.setattr(worker,'_reply',adapted_reply)
    original_lease=restore.Lease
    def lease(path,**kwargs):
        if kwargs['uid']==0:kwargs['uid']=os.getuid()
        return original_lease(path,**kwargs)
    monkeypatch.setattr(restore,'Lease',lease)
    with t.run() as (prep,check):assert check()==prep
    now=int(datetime.now(timezone.utc).timestamp());attempt='9'*32
    common={k:h(200+i) for i,k in enumerate(sorted(c.COMMON)) if k.endswith('_sha256')}
    common.update(type=c.PLAN,attempt_id=attempt,phase='update',created_at=now-10,expires_at=now+3600,
                  uid=499,gid=499,restore_attempt_id=t.x.args.input_attempt)
    for k in ('context_sha256','publisher_sha256','registry_sha256','execution_package_sha256',
              'monitor_package_sha256','system_control_sha256','system_plist_sha256'):common[k]=prep[k]
    for k in ('transport_package_sha256','transport_control_sha256','transport_contract_sha256'):common[k]=prep['backup'][k]
    common['restore_receipt_sha256']=prep['restore']['restore_receipt_sha256']
    network=c.canonical(valid_network());common['network_file_sha256']=c.raw_digest(network)
    common['network_sha256']=v3.digest(valid_network())
    old=dict(common,current_package_sha256=t.roles['cold'],state_before_sha256=h(900))
    old_raw=c.canonical(old);old_sha=c.raw_digest(old_raw)
    previous=dict(type=c.REPLY,attempt_id=attempt,phase='update',plan_sha256=old_sha,challenge='8'*32,
        identity=boot._expected(499,499),manual_stop=True,process_stopped=True,home_rehearsal=True,borrowed_lock=False,
        result=dict(state_before_sha256=h(900),state_after_sha256=prep['state_sha256'],
            previous_package_sha256=t.roles['cold'],current_package_sha256=t.roles['execution'],
            backup_sha256=prep['backup']['sha256'],backup_receipt_sha256=prep['backup']['receipt_sha256'],nas_independent_readback=True))
    previous_raw=c.canonical(previous)
    plan=dict(common,phase='prepare',update_result_file_sha256=c.raw_digest(previous_raw),
        state_stopped_sha256=prep['state_sha256'],backup_sha256=prep['backup']['sha256'],
        backup_receipt_sha256=prep['backup']['receipt_sha256'],request_id='7'*32)
    slot=base/('current-mac-cutover-'+attempt);slot.mkdir(mode=0o755);slot.chmod(0o755)
    def publish(name,raw):
        path=slot/name;path.write_bytes(raw);path.chmod(0o644)
    for name,raw in [('update-plan.json',old_raw),('update-result.json',previous_raw),
                     ('prepare-plan.json',c.canonical(plan)),('network.json',network)]:publish(name,raw)
    request=dict(type=c.REQUEST,attempt_id=attempt,phase='prepare',plan_sha256=c.raw_digest(c.canonical(plan)),challenge='6'*32,lock_fd=None)
    result=worker.run(request,script='synthetic',executable='synthetic')
    prep,target_request=result['result']['preparation'],result['result']['request']
    source=valid_body(v3)['source_stop'];source['request_sha256']=v3.digest(target_request)
    stamp=v3._stamp(datetime.now(timezone.utc));source['audit_started_at']=source['verified_at']=stamp
    authorization=codec.sign_authorization(target_request,source,prep,t.c.key,now=v3._time(stamp))
    if fault=='bad-signature':
        import base64
        damaged=v3.decode(authorization)
        signature=bytearray(base64.b64decode(damaged['signature']));signature[0]^=1
        damaged['signature']=base64.b64encode(signature).decode()
        authorization=v3.canonical(damaged)
    plan={k:v for k,v in plan.items() if k!='update_result_file_sha256'}
    plan.update(phase='activate',preparation_file_sha256=c.raw_digest(c.canonical(prep)),preparation_sha256=v3.digest(prep),
        request_file_sha256=c.raw_digest(c.canonical(target_request)),request_sha256=v3.digest(target_request),
        authorization_file_sha256=c.raw_digest(authorization),operator_attestation={k:True for k in v3.ATTESTATION})
    for name,raw in [('activate-plan.json',c.canonical(plan)),('preparation.json',c.canonical(prep)),
                     ('request.json',c.canonical(target_request)),('authorization.json',authorization)]:publish(name,raw)
    request.update(phase='activate',plan_sha256=c.raw_digest(c.canonical(plan)))
    if fault=='expired':
        from datetime import timedelta
        future=v3._time(stamp)+timedelta(hours=8)
        monkeypatch.setattr(worker,'datetime',SimpleNamespace(now=lambda tz:future))
    elif fault=='backup-mutated':
        with t.copy.open('ab') as out:out.write(b'synthetic tampering')
    state_before=(root/'state.json').read_bytes()
    with (root/'control/operation.lock').open('r+b' if fault=='writable-parent' else 'rb') as held:
        if fault!='unlocked-parent':fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
        request['lock_fd']=held.fileno()
        if fault=='none':activated=worker.run(request,script='synthetic',executable='synthetic')
        else:
            from cryptography.exceptions import InvalidSignature
            with pytest.raises((ValueError,InvalidSignature)):
                worker.run(request,script='synthetic',executable='synthetic')
        with (root/'control/operation.lock').open('rb') as contender:
            if fault=='unlocked-parent':fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
            else:
                with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert (root/'state.json').read_bytes()==state_before
    assert not (base/'factory-activation.json').exists()
    if fault=='none':
        assert codec.verify_authorization(authorization,t.c.public,target_request)['preparation']==prep
        assert activated['result']['record']['target_snapshot']==prep['target_snapshot']
        assert activated['borrowed_lock'] is True

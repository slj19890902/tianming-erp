"""Synthetic NFS/root/UID adapters; real files, hashes, GCM, ZIP and signatures.

No real mount, DPAPI, installed instance, service operation or production data.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_assistant import mounted_nas_proof as m, current_mac_worker_contract as c
from desktop_assistant import current_mac_cutover_contract as v3
from desktop_assistant import current_mac_plan_install as plans
from desktop_assistant import current_mac_activation_install as activation
from desktop_assistant import mac_system_upgrade as root, current_mac_target_audit as audit
from desktop_assistant.storage import sha, read_json, write_json
from tests.desktop_assistant.test_current_mac_worker_contract import chain
from tests.desktop_assistant.test_current_mac_target_audit import target
from tests.desktop_assistant.test_mac_service_restore import case, recovery_case, host_store, PASSWORD
from tests.desktop_assistant.test_current_mac_plan_install import staged
from tests.desktop_assistant.test_current_mac_dispatch import scene


def local_proof(state,source,shared=1,database_sha='d'*64):
    return dict(type='tianming.local-code-update.v1',storage='local',independent_device=False,
        nas_verified=False,backup_sha256=sha(source),backup_bytes=source.stat().st_size,
        current=state['current'],previous=state['previous'],database_sha256=database_sha,
        shared_files=shared,manual_stop=True,restored_verified=True,factory_activated=False)


@pytest.fixture
def mounted_scene(tmp_path,monkeypatch):
    base=tmp_path/'base';base.mkdir(mode=0o755);instance=base/'instance';instance.mkdir(mode=0o700)
    (instance/'backups').mkdir(mode=0o700);(instance/'staging').mkdir(mode=0o700)
    private=tmp_path/'private-user-home';private.mkdir(mode=0o700)
    mount=private/'ZSPACE';mount.mkdir();archive=mount/'sata1-18015598002/BoxERP/backups/mac-mini'
    destination=archive/('mounted-'+'1'*32);destination.mkdir(parents=True)
    source=instance/'backups/full.tmbackup';source.write_bytes(b'encrypted synthetic exact backup');source.chmod(0o600)
    nas=destination/source.name;nas.write_bytes(source.read_bytes())
    state=dict(current='a'*64,previous='b'*64,operation='update',manual_stop=False,pid=123,update_backup=str(source))
    proof=local_proof(state,source);state['local_code_update']=proof
    state_path=instance/'state.json';state_path.write_bytes(c.canonical(state));state_path.chmod(0o600)
    folder=instance/'staging'/('local-code-update-'+'2'*32);folder.mkdir(mode=0o700)
    proof_path=folder/'verified.json';proof_raw=c.canonical(proof);proof_path.write_bytes(proof_raw);proof_path.chmod(0o600)
    for module in (m,):
        monkeypatch.setattr(module,'BASE',base);monkeypatch.setattr(module,'ROOT',instance)
    for key,name in (('CONTROL','mounted-nas-service.json'),('CONTRACT','mounted-nas-contract.json'),
                     ('RECEIPT','mounted-nas-receipt.json')):monkeypatch.setattr(m,key,base/name)
    monkeypatch.setattr(m,'MOUNT',mount);monkeypatch.setattr(m,'ARCHIVE',archive)
    original_pin=root.Pin
    def pin(path,**kwargs):
        kwargs['uid']=os.getuid();kwargs['gid']=os.getgid();return original_pin(path,**kwargs)
    monkeypatch.setattr(root,'Pin',pin);monkeypatch.setattr(root,'DATA',tmp_path)
    monkeypatch.setattr(root.boot,'no_acl',lambda fd:None);monkeypatch.setattr(root,'_entry',lambda:None)
    # Root ownership is adapted only; no-follow traversal and all hashes remain.
    monkeypatch.setattr(root.boot,'protected',lambda path,**kw:b'' if kw.get('directory') else Path(path).read_bytes())
    monkeypatch.setattr(m,'_original_proof',lambda st:(m.local_update(st)[0],source,proof_path,proof_raw))
    monkeypatch.setattr(activation,'verify_code',lambda:'c'*64)
    lock=base/'publication.lock';lock.write_bytes(b'0')
    @contextmanager
    def publication():
        with lock.open('rb') as fd:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:yield
            finally:fcntl.flock(fd,fcntl.LOCK_UN)
    monkeypatch.setattr(root,'_publication',publication)
    monkeypatch.setattr(root.system,'metadata',lambda identity:dict(context={'uid':499,'gid':499},
        context_sha256='e'*64,publisher_sha256='f'*64))
    monkeypatch.setattr(root.system,'_program_manifest',lambda meta:dict(
        runtime_capabilities={m.CAPABILITY:1},files={m.MODULE:'0'*64}))
    observation=dict(classification='direct_network_unbound',kernel=dict(filesystem='nfs',
        mountpoint=str(mount),source=m.SOURCE,fsid=[7,8],owner=501,flags=0,subtype=0),
        remote=dict(addresses=[m.NAS_ADDRESS],export='/极空间-个人'))
    monkeypatch.setattr(m.mounts,'observe',lambda path:deepcopy(observation))
    monkeypatch.setattr(m.mounts,'fd_identity',lambda fd:deepcopy(observation['kernel']))
    return SimpleNamespace(base=base,root=instance,private=private,source=source,nas=nas,
        state=state,state_path=state_path,proof=proof,proof_raw=proof_raw,observation=observation)


def test_mounted_approval_truthful_readback_without_service_or_permissions(mounted_scene):
    s=mounted_scene;before=s.state_path.read_bytes();mode=s.private.stat().st_mode
    bound=m.approve(s.nas,'9'*64)
    assert bound['receipt']['verification_kind']=='mounted_filesystem'
    assert bound['receipt']['sha256']==sha(s.source)==sha(s.nas)
    assert 'transfer' not in bound['receipt'] and 'connection_guid' not in str(bound['receipt'])
    assert s.state_path.read_bytes()==before and s.private.stat().st_mode==mode
    plan=dict(transport_package_sha256='9'*64,transport_control_sha256=bound['control_sha'],
        transport_contract_sha256=bound['control']['contract_sha256'],
        backup_receipt_sha256=bound['control']['receipt_sha256'])
    with m.broker(plan) as fd:
        assert os.pread(fd,3,0)==b'enc'
        assert fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY


def zspace_proxy(scene):
    scene.observation['classification'] = 'loopback_proxy'
    scene.observation['remote'] = dict(addresses=['127.0.0.1'],
        export='/极空间-个人', server='极空间.local', version='4.0',
        port=52100, security='sys', tcp=True)


def test_zspace_proxy_preserves_actual_transport_and_full_backup_proof(mounted_scene):
    s = mounted_scene; zspace_proxy(s)
    original = s.state_path.read_bytes()
    bound = m.approve(s.nas, '9'*64)
    assert bound['receipt']['mount'] == s.observation
    assert bound['receipt']['mount']['classification'] == 'loopback_proxy'
    assert bound['receipt']['mount']['remote']['addresses'] == ['127.0.0.1']
    assert bound['receipt']['verification_kind'] == 'mounted_filesystem'
    assert bound['receipt']['sha256'] == sha(s.source) == sha(s.nas)
    assert s.state_path.read_bytes() == original
    plan = dict(transport_package_sha256='9'*64,
        transport_control_sha256=bound['control_sha'],
        transport_contract_sha256=bound['control']['contract_sha256'],
        backup_receipt_sha256=bound['control']['receipt_sha256'])
    with m.broker(plan) as fd:
        assert os.pread(fd, 3, 0) == b'enc'
    s.observation['remote']['port'] = 52101
    with pytest.raises(ValueError):
        with m.broker(plan): pytest.fail('changed proxy admitted')


@pytest.mark.parametrize('field,value', [
    ('addresses',['127.0.0.2']), ('addresses',['127.0.0.1','172.16.3.96']),
    ('server','other.local'), ('export','/other'), ('version','3'),
    ('port',52101), ('security','none'), ('tcp',False),
    ('owner',0), ('classification','direct_network_unbound'),
])
def test_unapproved_proxy_never_creates_mount_approval(mounted_scene, field, value):
    s = mounted_scene; zspace_proxy(s)
    if field == 'owner': s.observation['kernel'][field] = value
    elif field == 'classification': s.observation[field] = value
    else: s.observation['remote'][field] = value
    with pytest.raises(ValueError): m.approve(s.nas, '9'*64)
    assert not m.CONTROL.exists() and not m.RECEIPT.exists()


@pytest.mark.parametrize('fault',['truncated','different','source','local-filesystem','alias','symlink-file','symlink-parent'])
def test_wrong_or_incomplete_mount_never_creates_approval(mounted_scene,fault):
    s=mounted_scene
    if fault=='truncated':s.nas.write_bytes(s.nas.read_bytes()[:-1])
    elif fault=='different':s.nas.write_bytes(b'x'*s.source.stat().st_size)
    elif fault=='source':s.observation['kernel']['source']='other:/export'
    elif fault=='local-filesystem':s.observation['kernel']['filesystem']='apfs'
    elif fault=='alias':s.observation['remote']['addresses']=['127.0.0.1']
    elif fault=='symlink-file':s.nas.unlink();s.nas.symlink_to(s.source)
    else:
        real=s.nas.parent.with_name('real');s.nas.parent.rename(real);s.nas.parent.symlink_to(real)
    with pytest.raises((ValueError,OSError)):m.approve(s.nas,'9'*64)
    assert not m.CONTROL.exists() and not m.RECEIPT.exists()


@pytest.mark.parametrize('fault',['nas-changed','root-receipt-changed','mount-changed'])
def test_broker_rejects_changed_approved_object(mounted_scene,fault):
    s=mounted_scene;b=m.approve(s.nas,'9'*64)
    if fault=='nas-changed':s.nas.write_bytes(b'x'*s.nas.stat().st_size)
    elif fault=='root-receipt-changed':m.RECEIPT.write_bytes(m.RECEIPT.read_bytes()+b' ')
    else:s.observation['kernel']['fsid']=[9,10]
    p=dict(transport_package_sha256='9'*64,transport_control_sha256=b['control_sha'],
        transport_contract_sha256=b['control']['contract_sha256'],backup_receipt_sha256=b['control']['receipt_sha256'])
    with pytest.raises(ValueError):
        with m.broker(p):pytest.fail('changed proof admitted')


def test_distinct_mounted_plan_has_no_fabricated_update_link(chain):
    original=chain[0]['prepare'];plan=deepcopy(original);plan['type']=c.MOUNTED_PLAN
    plan.pop('update_result_file_sha256');plan['local_update_receipt_sha256']='1'*64
    c.validate_plan(plan)
    with pytest.raises(ValueError):c.validate_plan({**plan,'update_result_file_sha256':'2'*64})
    with pytest.raises(ValueError):c.validate_plan({**plan,'type':c.PLAN})
    assert v3.BACKUP_FIELDS=={'sha256','size','created_at','verified_at','receipt_sha256',
        'transport_control_sha256','transport_contract_sha256','transport_package_sha256',
        'release_sha256','schema_authority_sha256','credentials_sha256'}


@pytest.fixture
def checkpoint_target(target,monkeypatch):
    t=target;monkeypatch.setattr(m,'ROOT',t.x.root);monkeypatch.setattr(m,'BASE',t.x.base)
    state=t.manager.state
    proof=local_proof(state,t.copy,shared=len(t.cold['manifest']['files']),
        database_sha=sha(t.x.root/'shared/data/carton_erp.sqlite3'))
    state['local_code_update']=proof;write_json(t.x.root/'state.json',state)
    (t.x.root/'state.json').chmod(0o600)
    folder=t.x.root/'staging'/('local-code-update-'+'9'*32);folder.mkdir(mode=0o700)
    path=folder/'verified.json';path.write_bytes(c.canonical(proof));path.chmod(0o600)
    monkeypatch.setattr(m,'_original_proof',lambda st:(m.local_update(st)[0],t.copy,path,path.read_bytes()))
    return t


def test_fresh_checkpoint_real_restore_keeps_program_and_old_proof(checkpoint_target):
    t=checkpoint_target;state=(t.x.root/'state.json').read_bytes();original=deepcopy(t.manager.state['local_code_update'])
    proof=m.build_checkpoint(t.manager,PASSWORD,sha(t.x.root/'state.json'),lambda:None)
    assert proof['release_sha256']==t.manager.state['current']!=t.manager.state['previous']
    assert sha(Path(proof['local_path']))==proof['sha256'] and Path(proof['proof_path']).exists()
    assert (t.x.root/'state.json').read_bytes()==state and t.manager.state['local_code_update']==original
    assert t.copy.exists() and t.manager.state['update_backup']==str(t.copy)


@pytest.mark.parametrize('fault',['not-stopped','state-sha','changed-shared'])
def test_checkpoint_never_certifies_changed_or_running_source(checkpoint_target,monkeypatch,fault):
    t=checkpoint_target
    if fault=='not-stopped':
        state=t.manager.state;state['manual_stop']=False;write_json(t.x.root/'state.json',state)
    expected=sha(t.x.root/'state.json') if fault!='state-sha' else '0'*64
    if fault=='changed-shared':
        original=t.manager._build_backup_payload
        def changed(*args,**kw):
            payload=original(*args,**kw)
            (t.x.root/'shared/data/current-only.txt').write_bytes(b'changed after checkpoint')
            return payload
        monkeypatch.setattr(t.manager,'_build_backup_payload',changed)
    with pytest.raises(ValueError):m.build_checkpoint(t.manager,PASSWORD,expected,lambda:None)
    assert t.manager.state['current']==t.roles['execution']


def test_content_audit_accepts_current_release_and_rejects_changed_shared(checkpoint_target):
    from desktop_assistant.storage import decrypt_file,extract_verified
    t=checkpoint_target;p=m.build_checkpoint(t.manager,PASSWORD,sha(t.x.root/'state.json'),lambda:None)
    raw=t.x.root/'staging/readback-mounted.zip';decrypt_file(Path(p['local_path']),raw,PASSWORD)
    payload=t.x.root/'staging/payload-mounted';metadata=extract_verified(raw,payload)
    current=t.manager.state['current'];manifest=t.manager.manifest(current)
    archive=t.x.root/'packages'/(current+'.zip')
    audit._content(payload,metadata,t.x.root,t.manager.state,manifest,archive,t.c.public,p['sha256'],backup_release=current)
    (t.x.root/'shared/data/current-only.txt').write_bytes(b'live change during NAS upload')
    with pytest.raises(ValueError):
        audit._content(payload,metadata,t.x.root,t.manager.state,manifest,archive,t.c.public,p['sha256'],backup_release=current)


@pytest.mark.parametrize('phase',['approve','broker'])
def test_parent_directory_replacement_cannot_redirect_approved_path(mounted_scene,monkeypatch,phase):
    s=mounted_scene
    def replace():
        old=s.nas.parent; moved=old.with_name('old-directory')
        old.rename(moved);old.mkdir();(old/s.nas.name).write_bytes(b'x'*s.source.stat().st_size)
    if phase=='approve':
        original=m.full_hash;done=False
        def hash_then_replace(fd,size):
            nonlocal done
            result=original(fd,size)
            if not done:replace();done=True
            return result
        monkeypatch.setattr(m,'full_hash',hash_then_replace)
        with pytest.raises(ValueError):m.approve(s.nas,'9'*64)
        assert not m.CONTROL.exists()
    else:
        b=m.approve(s.nas,'9'*64)
        p=dict(transport_package_sha256='9'*64,transport_control_sha256=b['control_sha'],
            transport_contract_sha256=b['control']['contract_sha256'],backup_receipt_sha256=b['control']['receipt_sha256'])
        with pytest.raises(ValueError):
            with m.broker(p) as fd:
                replace();assert os.pread(fd,3,0)==b'enc'


def test_historical_backup_error_is_preserved_only_for_mounted_admission(checkpoint_target):
    t=checkpoint_target;state=t.manager.state;state['backup_error']='historical SMB path paused'
    write_json(t.x.root/'state.json',state);(t.x.root/'state.json').chmod(0o600)
    with pytest.raises(ValueError):audit._stopped(t.manager,state,t.roles['execution'])
    audit._stopped(t.manager,state,t.roles['execution'],mounted=True)
    assert t.manager.state['backup_error']==state['backup_error']
    state['backup_work_retained']='unresolved work'
    with pytest.raises(ValueError):audit._stopped(t.manager,state,t.roles['execution'],mounted=True)


@pytest.mark.parametrize('admin_reapproved', [False, True])
def test_completed_checkpoint_enters_prepare_without_update_records(staged,monkeypatch,admin_reapproved):
    """Root stage IO uses real exclusive files/pins/flock; worker is synthetic."""
    t=staged;s=t.s;root_path=s.base/'instance'
    for module in (m,):
        monkeypatch.setattr(module,'BASE',s.base);monkeypatch.setattr(module,'ROOT',root_path)
    # The source state already points at the execution package, no update occurs.
    state=dict(current=s.plans['update']['execution_package_sha256'],previous='8'*64,
        operation='update',manual_stop=True,pid=None)
    t.state.write_bytes(c.canonical(state));t.state.chmod(0o600)
    monkeypatch.setattr(m,'_original_proof',lambda state:None)
    checkpoint=dict(type='tianming.current-mac-local-checkpoint.v1',
        local_path=str(root_path/'backups/fresh.tmbackup'),
        proof_path=str(root_path/'staging'/('mounted-checkpoint-'+'5'*32)/'verified.json'),
        sha256='a'*64,size=17,raw_sha256='b'*64,created_at='2026-10-12T02:00:00Z',
        database={},shared_manifest_sha256='c'*64,release_sha256=state['current'],
        state_sha256=sha(t.state),local_update_receipt_sha256='d'*64)
    def dispatch(raw,*,verify_code):
        plan=c.decode(raw)
        if plan['phase']=='checkpoint':
            reply=deepcopy(s.replies['prepare'])
            reply.update(phase='checkpoint',plan_sha256=c.raw_digest(raw),result={'checkpoint':checkpoint})
            return reply
        reply=t.dispatch(raw,verify_code=verify_code)
        prep=reply['result']['preparation']
        prep['backup'].update(sha256=plan['backup_sha256'],receipt_sha256=plan['backup_receipt_sha256'])
        reply['result']['request']=v3.make_request(prep,plan['network_sha256'],plan['publisher_sha256'],
            request_id=plan['request_id'],now=t.stamp)
        return reply
    monkeypatch.setattr(plans.activation.dispatch,'dispatch',dispatch)
    p=s.plans['update']
    raw=plans.checkpoint_plan(p['attempt_id'],state['current'],p['monitor_package_sha256'],
        p['transport_package_sha256'],p['restore_attempt_id'],sha(t.restore),t.network)
    before=t.state.read_bytes();plans.checkpoint(raw,t.network)
    assert t.state.read_bytes()==before and (s.slot/'checkpoint-completed.json').exists()
    assert not (s.slot/'update-plan.json').exists() and not (s.slot/'update-result.json').exists()
    contract_raw=b'{"approved":"mounted"}'
    for module,key,name,contents in ((m,'CONTROL','mounted-control.json',b'mounted control'),
                                   (m,'CONTRACT','mounted-contract.json',contract_raw)):
        path=s.base/name;path.write_bytes(contents);path.chmod(0o644);monkeypatch.setattr(module,key,path)
    bound=dict(contract=dict(checkpoint_attempt_id=p['attempt_id'],
        checkpoint_result_sha256=sha(s.slot/'checkpoint-result.json'),local_update_receipt_sha256='d'*64),
        control=dict(package_sha256=p['transport_package_sha256'],receipt_sha256='e'*64,
            contract_sha256=c.raw_digest(contract_raw)),receipt=dict(sha256=checkpoint['sha256']),
        control_sha=sha(m.CONTROL),raw={m.CONTROL:m.CONTROL.read_bytes(),m.CONTRACT:contract_raw})
    monkeypatch.setattr(m,'load',lambda:deepcopy(bound))
    checkpoint_plan_bytes = (s.slot/'checkpoint-plan.json').read_bytes()
    if admin_reapproved:
        monkeypatch.setattr(plans, '_code', lambda: '9'*64)
    prepared=plans.prepare_mounted(p['attempt_id'])
    assert t.state.read_bytes()==before and (s.slot/'prepare-completed.json').exists()
    assert prepared['result']['preparation']['backup']['sha256']==checkpoint['sha256']
    assert c.decode((s.slot/'prepare-plan.json').read_bytes())['type']==c.MOUNTED_PLAN
    assert (s.slot/'checkpoint-plan.json').read_bytes() == checkpoint_plan_bytes
    if admin_reapproved:
        assert c.decode((s.slot/'prepare-plan.json').read_bytes())['code_manifest_sha256'] == '9'*64
        assert c.decode(checkpoint_plan_bytes)['code_manifest_sha256'] != '9'*64


@pytest.mark.parametrize('uploaded_local', [False, True])
def test_full_mounted_audit_preserves_restore_credentials_and_rejects_live_changes(checkpoint_target,monkeypatch,uploaded_local):
    """Real original restore, source signatures, credentials and full shared audit.

    NFS observation and root approval are explicit adapters; no real NAS proof is
    claimed. The same cold audit rechecks every business byte and credential.
    """
    t=checkpoint_target;p=m.build_checkpoint(t.manager,PASSWORD,sha(t.x.root/'state.json'),lambda:None)
    cipher=Path(p['local_path']);kernel=dict(filesystem='nfs',source=m.SOURCE,mountpoint=str(m.MOUNT))
    bound=dict(control=dict(package_sha256=t.roles['transport'],context_sha256=t.kwargs['context_sha256'],
        publisher_sha256=t.kwargs['publisher_sha256'],receipt_sha256='e'*64,contract_sha256='f'*64),
        contract=dict(local_update_proof_path=str(t.x.root/'staging'/('local-code-update-'+'9'*32)/'verified.json'),
            local_update_receipt_sha256=p['local_update_receipt_sha256'],checkpoint_attempt_id='7'*32,
            local_path=str(cipher)),receipt=dict(sha256=p['sha256'],size=p['size'],
            verified_at=datetime.now(timezone.utc).isoformat(),mount=dict(kernel=kernel),
            local_identity=m.fingerprint(cipher.stat()),file_identity=m.fingerprint(cipher.stat())),
        control_sha='d'*64,raw={})
    if uploaded_local: bound['uploaded_local_audit']={'type':m.UPLOADED_TYPE}
    monkeypatch.setattr(m,'load',lambda:deepcopy(bound))
    monkeypatch.setattr(m,'checkpoint_result',lambda attempt:deepcopy(p))
    def fd_identity(fd):
        assert not uploaded_local, 'explicit confirmed-upload audit must not claim a live NAS descriptor'
        return deepcopy(kernel)
    monkeypatch.setattr(m.mounts,'fd_identity',fd_identity)
    transport=t.x.base/'published'/t.roles['transport']/'migration'
    monkeypatch.setattr(audit,'__file__',str(transport/audit.MODULE))
    import sys
    monkeypatch.setattr(sys,'executable',str(transport/'runtime/bin/python3.12'))
    before=(t.x.root/'state.json').read_bytes()
    with cipher.open('rb') as inherited:
        args=dict(mounted_fd=inherited.fileno(),audit_package_sha256=t.roles['transport'])
        with t.run(**args) as (prep,recheck):
            assert prep['backup']['release_sha256']==t.roles['execution']
            assert prep['backup']['sha256']==p['sha256']
            assert prep['restore']['restore_receipt_sha256']==t.kwargs['restore_receipt_sha256']
            assert 'backup' in prep['credential_purposes'] and recheck()==prep
        assert (t.x.root/'state.json').read_bytes()==before
        (t.x.root/'shared/data/current-only.txt').write_bytes(b'changed while mounted upload ran')
        with pytest.raises(ValueError):
            with t.run(**args):pytest.fail('changed live shared accepted')


def test_checkpoint_worker_uses_original_lock_and_keeps_state(chain,checkpoint_target,monkeypatch):
    from desktop_assistant import current_mac_worker as worker,mac_service_restore as restore
    from tests.desktop_assistant.test_current_mac_cutover_contract import valid_network
    t=checkpoint_target;root_path=t.x.root;base=t.x.base
    for module in (worker,):
        monkeypatch.setattr(module,'BASE',base);monkeypatch.setattr(module,'ROOT',root_path)
    monkeypatch.setattr(c,'BASE',str(base));monkeypatch.setattr(worker,'_entry',lambda:None)
    context=restore.runtime_identity()
    monkeypatch.setattr(worker,'authenticated',lambda *a,**k:(context,t.c.public,{}))
    p={k:v for k,v in chain[0]['update'].items() if k in c.CHECKPOINT_COMMON}
    network=c.canonical(valid_network());created=int(datetime.now(timezone.utc).timestamp())
    p.update(type=c.MOUNTED_PLAN,phase='checkpoint',created_at=created,expires_at=created+3600,
        state_before_sha256=sha(root_path/'state.json'),execution_package_sha256=t.roles['execution'],
        transport_package_sha256=t.roles['transport'],network_file_sha256=c.raw_digest(network),
        network_sha256=v3.digest(c.decode(network)))
    slot=base/('current-mac-cutover-'+p['attempt_id']);slot.mkdir(mode=0o755)
    raw=c.canonical(p);(slot/'checkpoint-plan.json').write_bytes(raw);(slot/'checkpoint-plan.json').chmod(0o644)
    (slot/'network.json').write_bytes(network);(slot/'network.json').chmod(0o644)
    request=dict(type=c.REQUEST,attempt_id=p['attempt_id'],phase='checkpoint',plan_sha256=c.raw_digest(raw),
        challenge='4'*32,lock_fd=None)
    before=(root_path/'state.json').read_bytes();original=m.build_checkpoint
    def under_lock(*args,**kwargs):
        with (root_path/'control/operation.lock').open('rb') as other:
            with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return original(*args,**kwargs)
    monkeypatch.setattr(m,'build_checkpoint',under_lock)
    reply=worker.run(request,script='synthetic identity adapter',executable='synthetic identity adapter')
    c.validate_reply(reply,request,p,c.raw_digest(raw))
    assert (root_path/'state.json').read_bytes()==before and reply['manual_stop'] is True
    assert reply['result']['checkpoint']['release_sha256']==t.roles['execution']


def install_uploaded_local_mode(s,monkeypatch):
    zspace_proxy(s)
    b=m.approve(s.nas,'9'*64)
    contract={**b['contract'],'checkpoint_attempt_id':'1'*32,'checkpoint_result_sha256':'2'*64}
    monkeypatch.setattr(m,'checkpoint_result_hash',lambda attempt:'2'*64)
    receipt={**b['receipt'],'contract_sha256':m.c.digest(contract)}
    control={**b['control'],'contract_sha256':m.c.digest(contract),'receipt_sha256':m.c.digest(receipt)}
    for path,v in [(m.CONTRACT,contract),(m.RECEIPT,receipt),(m.CONTROL,control)]:path.write_bytes(m.c.canonical(v))
    stamp=datetime.now(timezone.utc).isoformat()
    report=dict(backup_sha256=receipt['sha256'],backend_transfer=dict(name=s.nas.relative_to(m.MOUNT).as_posix(),
        dstFs='uzmount:',error='',bytes=receipt['size'],size=receipt['size'],completed_at=stamp),
        disk_cache=dict(uploadsQueued=0,uploadsInProgress=0,erroredFiles=0),checked_at=stamp,
        physical_backend_cold_hash_verified=False)
    backend=s.base/'mounted-backend-confirmation.json';backend.write_bytes(m.c.canonical(report))
    value=dict(type=m.UPLOADED_TYPE,context_sha256=control['context_sha256'],publisher_sha256=control['publisher_sha256'],
        audit_package_sha256=control['package_sha256'],contract_sha256=control['contract_sha256'],receipt_sha256=control['receipt_sha256'],
        backup_sha256=receipt['sha256'],size=receipt['size'],local_path=contract['local_path'],backend_confirmation_sha256=sha(backend))
    side=s.base/'mounted-upload-local-audit.json';side.write_bytes(m.c.canonical(value))
    monkeypatch.setattr(root.system,'_program_manifest',lambda meta:dict(runtime_capabilities={m.UPLOADED_CAPABILITY:1}))
    return side,value,backend,report


def test_confirmed_upload_uses_only_authenticated_local_cipher_with_no_nas_io(mounted_scene,monkeypatch):
    s=mounted_scene;install_uploaded_local_mode(s,monkeypatch);b=m.load()
    # The backend may be unavailable after a complete archival upload. This
    # explicit audit keeps the original mounted readback/upload evidence intact.
    s.nas.rename(s.nas.with_suffix('.retained'))
    monkeypatch.setattr(m,'observe',lambda:pytest.fail('new NAS observation forbidden in explicit local audit'))
    p=dict(transport_package_sha256='9'*64,transport_control_sha256=b['control_sha'],
        transport_contract_sha256=b['control']['contract_sha256'],backup_receipt_sha256=b['control']['receipt_sha256'])
    with m.broker(p) as fd:assert os.pread(fd,3,0)==b'enc'
    s.source.write_bytes(b'x'*s.source.stat().st_size)
    with pytest.raises(ValueError):
        with m.broker(p):pytest.fail('changed local ciphertext accepted')


@pytest.mark.parametrize('fault',['package','context','bytes','size-bool','queued','destination','name','error','cold-claim','future','proof-hash'])
def test_bad_confirmed_upload_never_falls_back_to_another_backend(mounted_scene,monkeypatch,fault):
    s=mounted_scene;side,value,backend,report=install_uploaded_local_mode(s,monkeypatch)
    if fault=='package':value['audit_package_sha256']='8'*64
    elif fault=='context':value['context_sha256']='a'*64
    elif fault=='bytes':report['backend_transfer']['bytes']-=1
    elif fault=='size-bool':report['backend_transfer']['size']=True
    elif fault=='queued':report['disk_cache']['uploadsQueued']=1
    elif fault=='destination':report['backend_transfer']['dstFs']=':local:'
    elif fault=='name':report['backend_transfer']['name']='other/full.tmbackup'
    elif fault=='error':report['backend_transfer']['error']='synthetic upload failed'
    elif fault=='cold-claim':report['physical_backend_cold_hash_verified']=True
    elif fault=='future':report['backend_transfer']['completed_at']='2099-10-13T00:00:00+00:00'
    else:value['backend_confirmation_sha256']='0'*64
    backend.write_bytes(m.c.canonical(report))
    if fault!='proof-hash':value['backend_confirmation_sha256']=sha(backend)
    side.write_bytes(m.c.canonical(value))
    with pytest.raises(ValueError):m.load()

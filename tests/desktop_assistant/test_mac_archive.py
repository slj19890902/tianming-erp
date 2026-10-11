"""Synthetic root/NFS adapters; real private IO, native ACL and fresh read process.

No real mounts, administrator writes, production backups or credentials.
"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from app.core import mac_service_keychain as keychain
from desktop_assistant import mac_archive_contract as c, mac_archive_install as i
from desktop_assistant import mac_archive_probe as p, mac_backup_storage as s, mac_mount as m
from desktop_assistant import mac_keychain_install as boot, mac_release_publish as pub


def contract(): return c.create('c'*64, '192.0.2.20', '/ERP归档', b'# original\n+auto_master\n')


def trigger():
    return dict(fsid=[20, 31], filesystem='autofs', mountpoint=str(c.MOUNT), source='map '+str(c.MAP),
                owner=0, flags=0x18, subtype=0)


def mounted(value):
    return {'classification': 'direct_network_unbound', 'autofs': trigger(),
        'kernel': dict(fsid=[21,31], filesystem='nfs', mountpoint=value['mountpoint'], source=value['source'],
                       owner=0, flags=0x4000018, subtype=0),
        'remote': dict(addresses=[value['server_address']], export=value['export'], server=value['server_address'],
                       version='4.0', port=2049, security='sys', tcp=True,
                       semantics={'hard':True,'nointr':True,'resvport':True})}


def test_derived_map_preserves_all_original_bytes_and_precedes_includes():
    original = b'# untouched\n/net -hosts\n+auto_master\n'
    result = c.append_master(original)
    assert result.replace(c.master_line(), b'') == original
    assert result.index(c.master_line()) < result.index(b'+auto_master')
    value = c.create('c'*64, '192.0.2.20', '/ERP归档', original)
    mapping = c.map_bytes(value)
    assert mapping.startswith(str(c.MOUNT).encode()+b'\t-')
    assert b'hard,nointr,tcp,vers=4,port=2049,sec=sys,resvport,nosuid,nodev' in mapping
    c.installed(value, {c.MASTER: result, c.MAP:mapping}.__getitem__)


@pytest.mark.parametrize('address', ['127.0.0.1','0.0.0.0','169.254.1.2','224.0.0.1','255.255.255.255',
                                      'nas.example.invalid','::1','192.0.2.020','192.0.2.20\n'])
def test_reject_ambiguous_or_proxy_endpoint(address):
    with pytest.raises(ValueError): c.endpoint(address, '/ERP')


@pytest.mark.parametrize('export', ['/', '/a//b', '/a/../b', '/a/./b','/a b','/a\nb','/a#b','/a&b',
                                    '/$HOST','/*','/a:b','/a,b','/a"b','/e\u0301','a/b'])
def test_reject_autofs_path_syntax(export):
    with pytest.raises(ValueError): c.endpoint('192.0.2.20', export)


@pytest.mark.parametrize('original', [b'',b'no-newline',b'x\0\n', b'auto_tianming_erp\n', b'TianmingERP\n',b'x'*65535+b'\n'])
def test_master_is_not_repaired_or_overwritten(original):
    with pytest.raises(ValueError): c.append_master(original)


@pytest.mark.parametrize('change', ['map','master','context','extra','path','portbool'])
def test_contract_is_exact(change):
    value=contract(); master=c.append_master(b'# original\n+auto_master\n'); mapping=c.map_bytes(value)
    if change=='map': mapping+=b'# changed\n'
    if change=='master': master+=b'# changed\n'
    if change=='context': value['context_sha256']='d'*64
    if change=='extra': value['extra']=True
    if change=='path': value['mountpoint']='/tmp/fake'
    if change=='portbool': value['port']=True
    with pytest.raises(ValueError):
        c.validate(value,'c'*64); c.installed(value,{c.MASTER:master,c.MAP:mapping}.__getitem__)


@pytest.mark.parametrize('fault', ['none','reverse','wrong-map','owner','duplicate','unknown','single-nfs','nested'])
def test_controlled_topology_is_order_independent_and_requires_trigger(monkeypatch,fault):
    value=contract(); obs=mounted(value); records=[obs['autofs'],obs['kernel']]
    if fault=='reverse': records.reverse()
    if fault=='wrong-map': obs['autofs']['source']='map /private/etc/other'
    if fault=='owner': obs['autofs']['owner']=501
    if fault=='duplicate': records.append(deepcopy(obs['kernel']))
    if fault=='unknown': records.append({**obs['kernel'],'filesystem':'smbfs'})
    if fault=='single-nfs': records=[obs['kernel']]
    if fault=='nested': records.append({**obs['kernel'],'mountpoint':str(c.DESTINATION)})
    monkeypatch.setattr(m,'mounts',lambda:records)
    expected={'mountpoint':str(c.MOUNT),'source':'map '+str(c.MAP)}
    if fault in {'none','reverse'}: assert m._autofs_select(c.DESTINATION,expected)==(obs['kernel'],obs['autofs'])
    else:
        with pytest.raises(ValueError): m._autofs_select(c.DESTINATION,expected)
    if fault in {'none','reverse'}:
        with pytest.raises(ValueError): m._select(c.DESTINATION)


@pytest.mark.parametrize('fault',['flags','subtype','extra','fsid'])
def test_historical_trigger_schema_is_complete(fault):
    value=contract(); obs=mounted(value)
    if fault in {'flags','subtype'}: del obs['autofs'][fault]
    if fault=='extra': obs['autofs']['extra']=True
    if fault=='fsid': obs['autofs']['fsid']=[True,31]
    assert not s._matches(value,obs)


@pytest.fixture
def local_archive(tmp_path,monkeypatch):
    base=tmp_path.resolve()/'system'; base.mkdir(mode=0o755)
    mount=base/'archive'; mount.mkdir(); dest=mount/'backups';dest.mkdir(mode=0o700)
    for module in (c,i,s): monkeypatch.setattr(module,'BASE',base)
    monkeypatch.setattr(c,'MOUNT',mount);monkeypatch.setattr(c,'DESTINATION',dest)
    monkeypatch.setattr(c,'MAP',tmp_path/'auto_tianming_erp')
    context={'uid':os.getuid(),'gid':os.getgid()}; value=contract(); obs=mounted(value)
    real_checked=keychain._checked_path
    @contextmanager
    def root_adapter(path,**kw):
        assert path==base and kw==dict(uid=0,directory=True,mode=0o755)
        with real_checked(path,uid=os.getuid(),directory=True,mode=0o755) as fd: yield fd
    monkeypatch.setattr(keychain,'_checked_path',root_adapter)
    monkeypatch.setattr(c,'installed',lambda *a:None)  # Root metadata simulated only.
    monkeypatch.setattr(m,'local_addresses',lambda:{'127.0.0.1'})
    monkeypatch.setattr(m,'observe',lambda *a,**k:deepcopy(obs))
    monkeypatch.setattr(m,'fd_identity',lambda fd:deepcopy(obs['kernel']))
    plan={'attempt_id':'a'*32,'contract':value,'context_sha256':value['context_sha256']}
    request={'phase':'probe','challenge':'b'*32,'plan':plan,'plan_sha256':'d'*64}
    monkeypatch.setattr(p,'authenticated',lambda *a:(context,plan))
    location=s.autofs_location(value,c.digest(boot.canonical(value)),context)
    return SimpleNamespace(base=base,dest=dest,context=context,value=value,obs=obs,plan=plan,request=request,location=location)


@pytest.mark.parametrize('fault',['none','changed-trigger','local','unknown','wrong-group','changed-config','changed-fd','soft'])
def test_explicit_trigger_binds_same_underlay_and_actual_fd(local_archive,monkeypatch,fault):
    a=local_archive; before={'classification':'automount_trigger_unbound','kernel':deepcopy(a.obs['autofs'])}
    if fault=='changed-trigger': a.obs['autofs']['fsid']=[999,31]
    if fault=='local': before={'classification':'local_or_unmounted'}
    if fault=='unknown': before={'classification':'unknown'}
    if fault=='wrong-group': a.context['gid']+=1
    if fault=='soft': a.obs['remote']['semantics']['hard']=False
    if fault=='changed-fd': monkeypatch.setattr(m,'fd_identity',lambda fd:{**a.obs['kernel'],'fsid':[99,31]})
    values=iter([before,a.obs,a.obs]); monkeypatch.setattr(m,'observe',lambda *a,**kw:next(values))
    if fault=='changed-config':
        calls=[]
        def installed(*args):
            calls.append(1)
            if len(calls)>1: raise ValueError('synthetic config change')
        monkeypatch.setattr(c,'installed',installed)
    if fault=='none': assert s.autofs_location(a.value,c.digest(boot.canonical(a.value)),a.context)['mount']==a.obs
    else:
        with pytest.raises(ValueError): s.autofs_location(a.value,c.digest(boot.canonical(a.value)),a.context)
    assert list(a.dest.iterdir())==[]


def fresh_reader(a,tmp_path):
    # A separate actual interpreter with explicit test-only OS/identity adapters.
    # Production entry/identity refusal is separately exercised; never add a
    # bypass to production source or use a real dedicated UID for this fixture.
    wrapper=tmp_path/'reader.py'
    wrapper.write_text('import sys, json\nfrom pathlib import Path\n'
        f'sys.path.insert(0,{str(Path(__file__).resolve().parents[2])!r})\n'
        'from desktop_assistant import mac_archive_probe as p, mac_archive_contract as c, mac_mount as m\n'
        f'c.DESTINATION=Path({str(a.dest)!r})\n'
        f'context={a.context!r}; plan={a.plan!r}; location={a.location!r}\n'
        'p.authenticated=lambda *a:(context,plan)\n'
        'p.anchor=lambda *a:location\n'
        "m.fd_identity=lambda fd:location['mount']['kernel']\n"
        'p.main(script=__file__,executable=sys.executable)\n')
    return wrapper


def test_synthetic_probe_uses_fresh_process_fsync_rename_and_retains_evidence(local_archive,tmp_path):
    a=local_archive; wrapper=fresh_reader(a,tmp_path)
    result=p.run(a.request,script=str(wrapper),executable=sys.executable)
    proof=result['proof']; path=Path(proof['path'])
    assert proof['independent_readback'] is True and path.stat().st_size==p.SIZE
    assert c.digest(path.read_bytes())==proof['sha256']
    assert path.stat().st_mode&0o777==0o600 and path.parent.stat().st_mode&0o777==0o700
    assert set(x.name for x in path.parent.iterdir())=={'complete.probe'}
    with pytest.raises(FileExistsError): p.run(a.request,script=str(wrapper),executable=sys.executable)
    assert path.exists()


@pytest.mark.parametrize('fault',['reader-error','challenge','digest','mutate-after-read','replace-after-read'])
def test_failed_probe_preserves_scene_without_a_success(local_archive,monkeypatch,fault):
    a=local_archive
    def child(request,*args):
        if fault=='reader-error': raise ValueError('synthetic read failure')
        observed={**p.reply(request,a.context),'proof':p.readback(a.plan,a.context,request['expected'])}
        path=Path(observed['proof']['path'])
        if fault=='challenge': observed['challenge']='0'*32
        if fault=='digest': observed['proof']['sha256']='0'*64
        if fault=='mutate-after-read': path.write_bytes(b'x'*p.SIZE)
        if fault=='replace-after-read':
            replacement=path.with_name('replacement'); replacement.write_bytes(path.read_bytes()); replacement.chmod(0o600)
            replacement.replace(path)
        return observed
    monkeypatch.setattr(p,'read_child',child)
    with pytest.raises(ValueError): p.run(a.request,script='synthetic',executable='synthetic')
    assert (a.dest/('.tianming-install-'+a.plan['attempt_id'])/'complete.probe').exists()
    assert not (a.base/'archive-contract.json').exists()


@pytest.fixture
def installer(tmp_path,monkeypatch):
    base=tmp_path/'system';base.mkdir();etc=tmp_path/'etc';etc.mkdir()
    master=etc/'auto_master'; master.write_bytes(b'# original\n+auto_master\n')
    monkeypatch.setattr(i,'BASE',base);monkeypatch.setattr(i,'CONTRACT',base/'archive-contract.json')
    monkeypatch.setattr(c,'MASTER',master);monkeypatch.setattr(c,'MAP',etc/'auto_tianming_erp')
    monkeypatch.setattr(c,'MOUNT',base/'archive');monkeypatch.setattr(c,'DESTINATION',base/'archive/backups')
    monkeypatch.setattr(c,'read_configuration',lambda path:path.read_bytes())
    monkeypatch.setattr(boot,'verify_code',lambda **kw:'e'*64)
    monkeypatch.setattr(boot,'protected',lambda path,**kw:path.read_bytes())
    monkeypatch.setattr(boot,'modules',lambda:(SimpleNamespace(_administrator=lambda:None),None))
    monkeypatch.setattr(pub,'lock',lambda:nullcontext())
    monkeypatch.setattr(pub,'read_record',lambda path:path.read_bytes())
    meta={'package':'f'*64,'context':{'uid':499,'gid':499},'context_sha256':'c'*64,
          'publisher_sha256':'d'*64,'registry_sha256':'b'*64,'tree':base/'published/migration'}
    monkeypatch.setattr(i,'metadata',lambda package:deepcopy(meta))
    monkeypatch.setattr(m,'mounts',lambda:[])
    calls=[]
    def worker(meta,raw,phase):
        calls.append(phase)
        if phase=='identity': assert not c.MAP.exists() and not i.CONTRACT.exists()
        else:
            assert c.MAP.exists() and not i.CONTRACT.exists()
            assert i.pending(json.loads(raw)['attempt_id']).read_bytes()==raw
        return {'synthetic_phase':phase}
    monkeypatch.setattr(i,'worker',worker)
    def communicate(process): calls.append('automount'); return (0,b'',b'')
    monkeypatch.setattr(i.service,'_communicate',communicate)
    monkeypatch.setattr(i.subprocess,'Popen',lambda command,**kw: (command,kw))
    plan=i.make_plan('f'*64,'192.0.2.20','/ERP');raw=boot.canonical(plan)
    return SimpleNamespace(base=base,master=master,meta=meta,plan=plan,raw=raw,calls=calls,worker=worker)


def test_install_publishes_only_after_probe_and_keeps_original_and_pending(installer):
    a=installer; original=a.master.read_bytes();result=i.apply(a.raw,c.digest(a.raw))
    assert a.calls==['identity','automount','probe']
    assert result['contract_published'] is True and not result['logout_reboot_verified']
    assert not result['remote_device_persistence_verified'] and not result['service_started']
    assert i.CONTRACT.read_bytes()==boot.canonical(a.plan['contract'])
    assert (a.base/('archive-install-'+a.plan['attempt_id']+'.original-master')).read_bytes()==original
    assert i.pending(a.plan['attempt_id']).read_bytes()==a.raw
    with pytest.raises(ValueError): i.apply(a.raw,c.digest(a.raw))


@pytest.mark.parametrize('fault',['sha','expired','code','context','master','map','mount','existing','capability'])
def test_invalid_plan_or_conflict_causes_no_install_writes(installer,monkeypatch,fault):
    a=installer;plan=deepcopy(a.plan); approved=None
    if fault=='sha': approved='0'*64
    if fault=='expired': plan['created_at']-=8*86400;plan['expires_at']-=8*86400
    if fault=='code': plan['code_manifest_sha256']='0'*64
    if fault=='context': plan['context_sha256']='0'*64
    if fault=='master': a.master.write_bytes(b'# changed\n')
    if fault=='map': c.MAP.write_bytes(b'preserve me')
    if fault=='mount': monkeypatch.setattr(m,'mounts',lambda:[{'mountpoint':str(c.MOUNT)}])
    if fault=='existing': (a.base/'archive').mkdir()
    if fault=='capability': monkeypatch.setattr(i,'metadata',lambda package:i.capability({'files':{}}))
    before={str(x):x.read_bytes() for x in a.base.parent.rglob('*') if x.is_file()}
    raw=boot.canonical(plan)
    with pytest.raises(ValueError): i.apply(raw,approved or c.digest(raw))
    assert {str(x):x.read_bytes() for x in a.base.parent.rglob('*') if x.is_file()}==before
    assert not a.calls


@pytest.mark.parametrize('phase',['identity','automount','probe','contract','receipt'])
def test_apply_failure_keeps_every_created_object_and_refuses_replay(installer,monkeypatch,phase):
    a=installer
    def worker(meta,raw,action):
        if action==phase: raise ValueError('synthetic failure')
        return a.worker(meta,raw,action)
    monkeypatch.setattr(i,'worker',worker)
    if phase=='automount': monkeypatch.setattr(i.service,'_communicate',lambda process:(1,b'',b''))
    original_write=boot._write
    def write(path,*args,**kwargs):
        if (phase=='contract' and path==i.CONTRACT) or (phase=='receipt' and path.name.endswith('.receipt.json')):
            raise ValueError('synthetic late failure')
        return original_write(path,*args,**kwargs)
    monkeypatch.setattr(boot,'_write',write)
    with pytest.raises(ValueError): i.apply(a.raw,c.digest(a.raw))
    if phase=='identity': assert list(a.base.iterdir())==[]
    else:
        assert i.pending(a.plan['attempt_id']).exists() and c.MAP.exists()
        assert (a.base/('archive-install-'+a.plan['attempt_id']+'.original-master')).exists()
        assert i.CONTRACT.exists() == (phase=='receipt')
        with pytest.raises(ValueError): i.apply(a.raw,c.digest(a.raw))


def test_root_pack_imports_with_stdlib_only_and_ordinary_user_entry_refuses():
    repo=Path(__file__).resolve().parents[2]
    code=f"import sys;sys.path.insert(0,{str(repo)!r});from desktop_assistant import mac_archive_install;assert not any(x in sys.modules for x in ('psutil','cryptography','sqlalchemy','desktop_assistant.manager','desktop_assistant.mac_archive_probe'))"
    result=subprocess.run(['/usr/bin/python3','-I','-B','-S','-c',code],capture_output=True)
    assert result.returncode==0,result.stderr
    result=subprocess.run([sys.executable,'-I','-B',str(repo/'desktop_assistant/archive_probe_entry.py')],capture_output=True)
    assert result.returncode!=0 and '归档安装验证未完成'.encode() in result.stderr
    assert b'Traceback' not in result.stderr


@pytest.mark.skipif(sys.platform!='darwin',reason='native Darwin root configuration reader')
def test_actual_master_read_is_local_bounded_and_readonly():
    raw=c.read_configuration(c.MASTER)
    assert 0<len(raw)<=65536 and raw==c.MASTER.read_bytes()


def test_probe_expiry_before_commit_keeps_scene_without_contract(installer,monkeypatch):
    a=installer
    def worker(meta,raw,phase):
        result=a.worker(meta,raw,phase)
        if phase=='probe': monkeypatch.setattr(i.time,'time',lambda:a.plan['expires_at'])
        return result
    monkeypatch.setattr(i,'worker',worker)
    with pytest.raises(ValueError,match='过期'): i.apply(a.raw,c.digest(a.raw))
    assert i.pending(a.plan['attempt_id']).exists() and c.MAP.exists()
    assert not i.CONTRACT.exists()


@pytest.mark.parametrize('fault',['none','empty-mount','address','underlay','soft','identity','challenge','exit','interrupt'])
def test_root_worker_checks_full_reply_and_drops_all_extra_groups(installer,monkeypatch,fault):
    a=installer
    # Recover the real root IPC function replaced by the apply fixture.
    import importlib.util
    spec=importlib.util.spec_from_file_location('archive_install_protocol',i.__file__)
    real=importlib.util.module_from_spec(spec);spec.loader.exec_module(real)
    obs=mounted(a.plan['contract']);calls=[]
    class Process:
        returncode=1 if fault=='exit' else 0
        def communicate(self,raw):
            calls.append(raw)
            if fault=='interrupt' and len(calls)==1: raise KeyboardInterrupt
            request=json.loads(raw) if raw is not None else json.loads(calls[0])
            proof=p.proof(a.plan,{'contract_sha256':c.digest(boot.canonical(a.plan['contract'])),
                                 'mount':deepcopy(obs)},'1'*64)
            reply=p.reply(request,a.meta['context']);reply['proof']=proof
            if fault=='empty-mount': proof['mount']={}
            if fault=='address': proof['mount']['remote']['addresses']=['192.0.2.21']
            if fault=='underlay': proof['mount']['autofs']['source']='map /wrong'
            if fault=='soft': proof['mount']['remote']['semantics']['hard']=False
            if fault=='identity': reply['identity']['kernel_groups']=[499,80]
            if fault=='challenge': reply['challenge']='0'*32
            return boot.canonical(reply),b''
    def popen(args,**kw):
        assert args==[str(a.meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(a.meta['tree']/i.ENTRY)]
        assert kw['user']==499 and kw['group']==499 and kw['extra_groups']==[]
        assert kw['umask']==0o077 and kw['cwd']=='/' and kw['pass_fds']==()
        assert kw['env']['ERP_HOME_REHEARSAL']=='1'
        return Process()
    monkeypatch.setattr(real.subprocess,'Popen',popen)
    if fault=='none': assert real.worker(a.meta,a.raw,'probe')['proof']['mount']==obs
    else:
        with pytest.raises(ValueError): real.worker(a.meta,a.raw,'probe')
    if fault=='interrupt': assert len(calls)==2 and calls[1] is None


@pytest.mark.parametrize('fault',['none','actual-uid','actual-groups','context','pending','script','python','local','capability'])
def test_worker_authenticates_actual_identity_root_plan_and_signed_tree(installer,monkeypatch,fault):
    a=installer;repo=Path(p.__file__).resolve().parent.parent
    raw_context=b'synthetic actual context'
    a.meta['public']=b'synthetic public'
    a.meta['context_sha256']=c.digest(raw_context)
    a.plan['context_sha256']=a.meta['context_sha256']
    a.plan['contract']['context_sha256']=a.meta['context_sha256']
    a.raw=boot.canonical(a.plan)
    monkeypatch.setattr(sys,'flags',SimpleNamespace(**{**{k:getattr(sys.flags,k) for k in dir(sys.flags)
                         if not k.startswith('_')},'isolated':1}))
    monkeypatch.setattr(sys,'dont_write_bytecode',True)
    monkeypatch.setattr(keychain,'selected',lambda:True)
    monkeypatch.setattr(keychain,'load_context',lambda:deepcopy(a.meta['context']))
    actual=boot._expected(499,499)
    if fault=='actual-uid': actual['euid']=501
    if fault=='actual-groups': actual['kernel_groups']=[499,80]
    monkeypatch.setattr(boot,'_actual',lambda:actual)
    manifest={'runtime_capabilities':{'archive_autofs_v1':1},'files':{i.ENTRY:'hash',i.MODULE:'hash'}}
    if fault=='capability': manifest['runtime_capabilities']={}
    monkeypatch.setattr(p.published,'resolve',lambda *a,**kw:(repo,manifest))
    def read(path):
        if path==keychain.CONTEXT: return b'changed' if fault=='context' else raw_context
        assert path==i.pending(a.plan['attempt_id'])
        return b'changed' if fault=='pending' else a.raw
    monkeypatch.setattr(pub,'read_record',read)
    monkeypatch.setattr(m,'local_addresses',lambda:{'192.0.2.20'} if fault=='local' else {'127.0.0.1'})
    request={'phase':'probe','challenge':'a'*32,'plan':a.plan,'plan_sha256':c.digest(a.raw)}
    script=repo/i.ENTRY if fault!='script' else repo/'other.py'
    executable=repo/'runtime/bin/python3.12' if fault!='python' else Path(sys.executable)
    if fault=='none': assert p.authenticated(request,script,executable)==(a.meta['context'],a.plan)
    else:
        with pytest.raises(ValueError): p.authenticated(request,script,executable)


@pytest.mark.parametrize('mutation',['fsid','flags','source','remove'])
def test_observe_rejects_trigger_change_during_nfs_query(monkeypatch,mutation):
    value=contract();obs=mounted(value); before=[obs['autofs'],obs['kernel']];after=deepcopy(before)
    if mutation=='remove': after=after[1:]
    elif mutation=='source': after[0]['source']='map /changed'
    else: after[0][mutation]=[99,31] if mutation=='fsid' else 0x10
    snapshots=iter([before,after]);monkeypatch.setattr(m,'mounts',lambda:next(snapshots))
    record={value['source']:{'Mount Point':value['mountpoint'],'Current mount parameters':{
        'General mount flags':{'Bitmask':hex(obs['kernel']['flags'])},
        'File system locations':[{'Export':value['export'],'Server':value['server_address'],'Locations':[value['server_address']]}],
        'NFS parameters':['vers=4.0','tcp','port=2049','sec=sys','hard','nointr','resvport']},'Status flags':{'Bitmask':'0x0'}}}
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=json.dumps(record).encode()))
    assert m.observe(c.DESTINATION,semantics=True,automount={'mountpoint':str(c.MOUNT),
        'source':'map '+str(c.MAP)})=={'classification':'unknown'}


@pytest.mark.parametrize('alias',['logical','physical'])
def test_vacant_rejects_both_aliases_without_stat_of_remote(installer,monkeypatch,alias):
    a=installer
    monkeypatch.setattr(c,'MOUNT',Path('/System/Volumes/Data/Library/TianmingERP/archive'))
    path=i.BASE/'archive' if alias=='logical' else c.MOUNT
    monkeypatch.setattr(m,'mounts',lambda:[{'mountpoint':str(path)}])
    original_stat=Path.stat
    def fail_remote(self,*args,**kwargs):
        if self in (i.BASE/'archive',c.MOUNT): pytest.fail('root stat of prospective remote')
        return original_stat(self,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',fail_remote)
    with pytest.raises(ValueError,match='已有归档挂载'): i.vacant()


def test_vacant_checks_names_without_following_archive_symlink(installer,monkeypatch):
    (i.BASE/'archive').symlink_to('/nonexistent-remote')
    original_stat=Path.stat
    def fail_remote(self,*args,**kwargs):
        if self==i.BASE/'archive': pytest.fail('root stat of prospective remote')
        return original_stat(self,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',fail_remote)
    with pytest.raises(ValueError,match='已有归档对象'): i.vacant()


def test_concurrent_master_edit_is_preserved_and_prevents_replace(installer,monkeypatch):
    a=installer;original_write=boot._write
    def write(path,*args,**kwargs):
        result=original_write(path,*args,**kwargs)
        if path.name.startswith('.auto_master.tianming-'): a.master.write_bytes(b'# external admin change\n')
        return result
    monkeypatch.setattr(boot,'_write',write)
    with pytest.raises(ValueError,match='并发变化'): i.apply(a.raw,c.digest(a.raw))
    assert a.master.read_bytes()==b'# external admin change\n'
    assert c.MAP.exists() and list(a.master.parent.glob('.auto_master.tianming-*'))
    assert not i.CONTRACT.exists()


@pytest.mark.parametrize('mutation',['hard','nointr','resvport','readonly','async','nodev','nosuid','owner','port'])
def test_mount_semantics_cannot_silently_weaken_contract(mutation):
    value=contract();obs=mounted(value)
    if mutation in ('hard','nointr','resvport'): obs['remote']['semantics'][mutation]=False
    if mutation=='readonly':obs['kernel']['flags']|=1
    if mutation=='async':obs['kernel']['flags']|=0x40
    if mutation=='nodev':obs['kernel']['flags']&=~0x10
    if mutation=='nosuid':obs['kernel']['flags']&=~0x8
    if mutation=='owner':obs['kernel']['owner']=501
    if mutation=='port':obs['remote']['port']=52100
    assert not c.matches(value,obs)


def test_nfs_current_flags_prove_hard_nointr_reserved_port():
    value=contract();obs=mounted(value)
    record={value['source']:{'Mount Point':value['mountpoint'],'Current mount parameters':{
        'General mount flags':{'Bitmask':hex(obs['kernel']['flags'])},
        'File system locations':[{'Export':value['export'],'Server':value['server_address'],'Locations':[value['server_address']]}],
        'NFS parameters':['vers=4.0','tcp','port=2049','sec=sys','hard','nointr','resvport']},'Status flags':{'Bitmask':'0x0'}}}
    assert m.parse_nfs(json.dumps(record).encode(),obs['kernel'],semantics=True)['semantics']==obs['remote']['semantics']
    record[value['source']]['Current mount parameters']['NFS parameters'].append('soft')
    assert m.parse_nfs(json.dumps(record).encode(),obs['kernel'],semantics=True)['semantics']['hard'] is False

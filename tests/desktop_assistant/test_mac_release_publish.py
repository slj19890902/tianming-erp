"""Synthetic packages and simulated root identity; no privileged installation."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import mac_release_publish as m
from desktop_assistant import mac_keychain_install as b
REAL_PREFLIGHT = m.preflight

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac publication contract')


def make_package(path, key, *, metadata=None, additions=None, signature=None):
    files = {name: ('synthetic '+name).encode() for name in m.REQUIRED}
    files.update(additions or {})
    value = {'type':'tianming.release.v1','runtime_platform':'macos-arm64','version':path.stem,'revision':'synthetic-r1',
             'runtime_capabilities':{'immutable_execution_v1':1},
             'executable_files':['runtime/bin/python3.12'],
             'files':{name:b.digest(raw) for name,raw in files.items()}}
    value.update(metadata or {})
    raw = b.canonical(value)
    with zipfile.ZipFile(path,'x') as archive:
        for name, content in {**files,'manifest.json':raw,'manifest.sig':signature or key.sign(raw)}.items():
            archive.writestr(name,content)
    return raw,value


@pytest.fixture
def case(tmp_path,monkeypatch):
    # This fixture models the legacy-only publication contract. It must never
    # read an installed Mac-authority sidecar after a synthetic bad signature.
    from desktop_assistant import mac_publisher_trust
    monkeypatch.setattr(mac_publisher_trust,'_managed',lambda:False)
    root = tmp_path.resolve()
    base = root/'system';base.mkdir()
    for folder in ('instance','instance/shared',*(f'instance/shared/{n}' for n in m.MANAGED.values())):
        (base/folder).mkdir(mode=0o700)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    package = root/'native.zip'
    manifest_raw,manifest = make_package(package,key)
    context = {'type':'tianming.service-keychain.v1','installation_id':'a'*32,'host_sha256':'b'*64,'uid':499,'gid':499}
    context_raw = b.canonical(context)
    (base/'keychain-context.json').write_bytes(context_raw)
    code = b.canonical({'type':'tianming.keychain-code.v1','source':'c'*40,'files':{n:'d'*64 for n in b.FILES}})
    plan = m.prepare(package,public,context_raw,code,publisher_sha256=b.digest(public))
    monkeypatch.setattr(m,'BASE',base)
    monkeypatch.setattr(b,'verify_code',lambda **kw:b.digest(code))
    monkeypatch.setattr(b,'protected',lambda p,**kw: b'' if kw.get('directory') else p.read_bytes())
    calls=[]
    def guard(path,**kw):
        info=path.lstat()
        if kw.get('link'):
            if not stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o755:
                raise ValueError('synthetic managed link mode refusal')
            calls.append((path,kw));return
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode)!=kw.get('mode',0o644):
            raise ValueError('synthetic ownership/mode refusal')
        calls.append((path,kw))
    monkeypatch.setattr(m,'guard',guard)
    monkeypatch.setattr(m,'preflight',lambda value:deepcopy(context))
    monkeypatch.setattr(b,'modules',lambda:(SimpleNamespace(_administrator=lambda:None),None))
    monkeypatch.setattr(m.os,'getuid',lambda:0)
    monkeypatch.setattr(m.os,'geteuid',lambda:0)
    child = Mock()
    monkeypatch.setattr(m,'verify_child',child)
    return SimpleNamespace(root=root,base=base,key=key,public=public,package=package,context=context,
        context_raw=context_raw,code=code,plan=plan,manifest=manifest,manifest_raw=manifest_raw,child=child,calls=calls)


def publish(case,plan=None,package=None):
    raw=b.canonical(plan or case.plan)
    return m.publish(raw,b.digest(raw),package or case.package)


def test_publication_constructs_two_complete_trees_and_only_then_approves(case):
    def child(plan,context,slot):
        assert not (case.base/'published.json').exists()
        assert context==case.context
        assert (slot/'migration/manifest.json').read_bytes()==case.manifest_raw
        assert not (slot/'migration/data').exists()
    case.child.side_effect=child
    result=publish(case)
    identity=case.plan['package_sha256'];slot=case.base/'published'/identity
    assert set(p.name for p in slot.iterdir())=={'package.zip','server','migration'}
    assert (slot/'package.zip').read_bytes()==case.package.read_bytes()
    for kind in ('server','migration'):
        for name,digest in case.manifest['files'].items():
            assert b.digest((slot/kind/name).read_bytes())==digest
    for relative,folder in m.MANAGED.items():
        assert (slot/'server'/relative).is_symlink()
        assert (slot/'server'/relative).resolve()==case.base/'instance/shared'/folder
    record=json.loads((case.base/'published.json').read_bytes())
    assert record['packages']==[identity]
    assert (case.base/'publisher-public.pem').read_bytes()==case.public
    assert result['signature_verified_by_service_uid']
    assert not result['factory_activated'] and not result['system_daemon_installed']


def test_restrictive_umask_does_not_make_managed_links_root_only(case):
    targets={folder:(case.base/'instance/shared'/folder).stat()
             for folder in m.MANAGED.values()}
    previous=os.umask(0o077)
    try:publish(case)
    finally:os.umask(previous)
    slot=case.base/'published'/case.plan['package_sha256']
    for relative,folder in m.MANAGED.items():
        link=slot/'server'/relative
        assert stat.S_IMODE(link.lstat().st_mode)==0o755
        assert os.readlink(link)==str(case.base/'instance/shared'/folder)
        target=(case.base/'instance/shared'/folder).stat()
        before=targets[folder]
        assert (target.st_dev,target.st_ino,target.st_uid,target.st_gid,target.st_mode)==(
            before.st_dev,before.st_ino,before.st_uid,before.st_gid,before.st_mode)


def test_second_approval_preserves_previous_program_and_record(case):
    publish(case)
    old=(case.base/'published.json').read_bytes()
    package=case.root/'second.zip';make_package(package,case.key)
    plan=m.prepare(package,case.public,case.context_raw,case.code,publisher_sha256=b.digest(case.public))
    publish(case,plan,package)
    record=json.loads((case.base/'published.json').read_bytes())
    assert record['packages']==sorted([case.plan['package_sha256'],plan['package_sha256']])
    assert (case.base/'published'/case.plan['package_sha256']/'package.zip').read_bytes()==case.package.read_bytes()
    assert json.loads(old)['packages']==[case.plan['package_sha256']]


def test_published_output_satisfies_actual_execution_resolver(case,monkeypatch):
    publish(case)
    from desktop_assistant import mac_published_runtime as resolver
    from desktop_assistant import release_verification
    monkeypatch.setattr(resolver,'BASE',case.base)
    monkeypatch.setattr(resolver,'ROOT',case.base/'instance')
    monkeypatch.setattr(resolver,'PUBLISHED',case.base/'published')
    monkeypatch.setattr(resolver,'PUBLIC',case.base/'publisher-public.pem')
    monkeypatch.setattr(resolver,'REGISTRY',case.base/'published.json')
    monkeypatch.setattr(resolver.keychain,'CONTEXT',case.base/'keychain-context.json')
    monkeypatch.setattr(resolver.keychain,'load_context',lambda:case.context)
    @contextmanager
    def checked(path,**kw):
        m.guard(path,**kw)
        fd=os.open(path,os.O_RDONLY)
        try:yield fd
        finally:os.close(fd)
    monkeypatch.setattr(resolver.keychain,'_checked_path',checked)
    # Ownership is simulated; actual tree inventory, signature and hashes are not.
    def immutable(path,*,directory=False,link=False):
        m.guard(path,directory=directory,link=link,
                mode=0o755 if directory or path.stat().st_mode & 0o111 else 0o644)
    monkeypatch.setattr(resolver,'immutable',immutable)
    monkeypatch.setattr(release_verification,'immutable',immutable)
    for kind in ('server','migration'):
        tree,manifest=resolver.resolve(case.base/'instance',case.plan['package_sha256'],case.public,kind=kind)
        assert tree==case.base/'published'/case.plan['package_sha256']/kind
        assert manifest==case.manifest


@pytest.mark.parametrize('fault',['expired','wrong-sha','wrong-code','wrong-context-sha','boolean-size','extra-field'])
def test_bad_plan_rejected_before_publication(case,fault,monkeypatch):
    plan=deepcopy(case.plan)
    if fault=='expired':plan['expires_at']=plan['created_at']
    elif fault=='wrong-code':plan['code_manifest_sha256']='f'*64
    elif fault=='wrong-context-sha':plan['context_sha256']='invalid'
    elif fault=='boolean-size':plan['package_bytes']=True
    elif fault=='extra-field':plan['factory_activated']=True
    raw=b.canonical(plan)
    with pytest.raises(ValueError):m.publish(raw,'f'*64 if fault=='wrong-sha' else b.digest(raw),case.package)
    assert not (case.base/'published').exists() and not case.child.called


@pytest.mark.parametrize('fault',['source-changed','child-failed','readback-failed','receipt-failed'])
def test_failure_preserves_partial_evidence_without_approval(case,fault,monkeypatch):
    if fault=='source-changed':case.package.write_bytes(b'changed')
    elif fault=='child-failed':case.child.side_effect=ValueError('synthetic child failure')
    elif fault=='readback-failed':
        original=m._extract
        def extract(*args):
            original(*args)
            raise ValueError('synthetic readback failure')
        monkeypatch.setattr(m,'_extract',extract)
    else:
        original=b._write
        def write(path,*a,**kw):
            if path.name.endswith('.verified.json'):raise OSError('synthetic disk failure')
            return original(path,*a,**kw)
        monkeypatch.setattr(b,'_write',write)
    with pytest.raises((ValueError,OSError)):publish(case)
    assert not (case.base/'published.json').exists()
    if fault!='source-changed':
        assert (case.base/'published'/case.plan['package_sha256']).is_dir()
        assert list(case.base.glob('*.pending.json'))


def test_existing_slot_or_same_approved_package_is_never_replaced(case):
    publish(case)
    before=(case.base/'published.json').read_bytes()
    with pytest.raises(ValueError,match='已经批准'):publish(case)
    assert (case.base/'published.json').read_bytes()==before
    (case.base/'published.json').unlink()  # Remove only the synthetic fixture approval.
    with pytest.raises(ValueError,match='既有发布现场'):publish(case)
    assert (case.base/'published'/case.plan['package_sha256']/'package.zip').read_bytes()==case.package.read_bytes()


@pytest.mark.parametrize('fault',['traversal','data','case','prefix','capability','executable','windows','extra-member'])
def test_archive_structure_rejects_unsafe_or_unsupported_packages(case,fault):
    additions={};metadata={}
    if fault=='traversal':additions={'../outside.py':b'x'}
    elif fault=='data':additions={'data/business.sqlite3':b'x'}
    elif fault=='case':additions={'Main.py':b'x'}
    elif fault=='prefix':additions={'app':b'x'}
    elif fault=='capability':metadata={'runtime_capabilities':{'immutable_execution_v1':True}}
    elif fault=='executable':metadata={'executable_files':[]}
    elif fault=='windows':metadata={'runtime_platform':'windows'}
    package=case.root/'bad.zip';make_package(package,case.key,metadata=metadata,additions=additions)
    if fault=='extra-member':
        with zipfile.ZipFile(package,'a') as z:z.writestr('unlisted.py',b'x')
    with package.open('rb') as stream, pytest.raises(ValueError):m.inspect(stream)


def test_prepare_rejects_bad_factory_signature(case):
    package=case.root/'bad-signature.zip';make_package(package,case.key,signature=b'x'*64)
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        m.prepare(package,case.public,case.context_raw,case.code,publisher_sha256=b.digest(case.public))


def test_prepare_requires_independently_pinned_factory_publisher(case):
    with pytest.raises(ValueError,match='既有工厂发布身份'):
        m.prepare(case.package,case.public,case.context_raw,case.code,publisher_sha256='0'*64)


def test_atomic_replace_failure_keeps_previous_approval_bytes(case,monkeypatch):
    publish(case)
    previous=(case.base/'published.json').read_bytes()
    package=case.root/'replacement.zip';make_package(package,case.key)
    plan=m.prepare(package,case.public,case.context_raw,case.code,publisher_sha256=b.digest(case.public))
    def failed_replace(*a,**kw):raise OSError('synthetic rename failure')
    monkeypatch.setattr(m.os,'replace',failed_replace)
    with pytest.raises(OSError,match='rename failure'):publish(case,plan,package)
    assert (case.base/'published.json').read_bytes()==previous
    assert (case.base/('published-'+plan['attempt_id']+'.json')).is_file()
    assert (case.base/'published'/plan['package_sha256']).is_dir()


@pytest.mark.parametrize('fault',['none','context','mode','link','missing','public-key','acl'])
def test_real_preflight_logic_checks_context_data_and_publisher(case,monkeypatch,fault):
    # The identity/owner/ACL provider is simulated; the real preflight control
    # flow and filesystem paths run, separately from the publication fixture.
    context_path=case.base/'keychain-context.json';context_path.chmod(0o644)
    identity=SimpleNamespace(_administrator=lambda:None)
    keychain=SimpleNamespace(CONTEXT=context_path,_identity=lambda *a,**k:None)
    monkeypatch.setattr(b,'modules',lambda:(identity,keychain))
    plan=deepcopy(case.plan)
    path=case.base/'instance/shared/data'
    if fault=='context':plan['context_sha256']='f'*64
    elif fault=='mode':path.chmod(0o755)
    elif fault=='link':
        path.rmdir();path.symlink_to(case.base/'instance/shared/logs')
    elif fault=='missing':path.rmdir()
    elif fault=='public-key':
        public=case.base/'publisher-public.pem';public.write_bytes(b'wrong');public.chmod(0o644)
    elif fault=='acl':
        original=m.guard
        def guard(value,**kw):
            if value==path:raise ValueError('synthetic extra ACL')
            return original(value,**kw)
        monkeypatch.setattr(m,'guard',guard)
    if fault=='none':assert REAL_PREFLIGHT(plan)==case.context
    else:
        monkeypatch.setattr(m,'preflight',REAL_PREFLIGHT)
        with pytest.raises((ValueError,OSError)):publish(case,plan)
    assert not (case.base/'published').exists() and not case.child.called


def test_native_guard_rejects_non_root_owned_file(tmp_path):
    path=tmp_path/'synthetic-code';path.write_bytes(b'synthetic');path.chmod(0o644)
    with pytest.raises(ValueError):m.guard(path)


def test_worker_rechecks_real_signature_and_challenge_without_erp(case,monkeypatch):
    publish(case)
    monkeypatch.setattr(m.os,'getuid',lambda:499)
    monkeypatch.setattr(b,'_actual',lambda:b._expected(499,499))
    request={'plan':case.plan,'context':case.context,'challenge':'a'*32}
    result=m.worker(request)
    assert result['signature_verified'] and result['managed_links_readable'] and result['challenge']=='a'*32
    wrong={**case.plan,'public_key':Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()}
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):m.worker({**request,'plan':wrong})


def test_worker_can_read_pristine_links_before_private_targets_exist(case,monkeypatch):
    publish(case)
    for folder in m.MANAGED.values():
        (case.base/'instance/shared'/folder).rmdir()  # Empty synthetic directories.
    monkeypatch.setattr(m.os,'getuid',lambda:499)
    monkeypatch.setattr(b,'_actual',lambda:b._expected(499,499))
    result=m.worker({'plan':case.plan,'context':case.context,'challenge':'c'*32})
    assert result['managed_links_readable'] is True


@pytest.mark.parametrize('fault',['root-only','writable','wrong-target','regular-file','read-denied'])
def test_worker_rejects_unreadable_or_substituted_managed_links(case,monkeypatch,fault):
    publish(case)
    monkeypatch.setattr(m.os,'getuid',lambda:499)
    monkeypatch.setattr(b,'_actual',lambda:b._expected(499,499))
    link=case.base/'published'/case.plan['package_sha256']/'server/data'
    if fault in {'root-only','writable'}:
        os.chmod(link,0o700 if fault=='root-only' else 0o777,follow_symlinks=False)
    elif fault in {'wrong-target','regular-file'}:
        link.unlink()  # Synthetic test link only.
        if fault=='wrong-target':
            link.symlink_to(case.base/'instance/shared/logs')
            os.chmod(link,0o755,follow_symlinks=False)
        else:link.write_bytes(b'synthetic substitute')
    else:
        original=os.readlink
        def denied(path,*args,**kwargs):
            if Path(path)==link:raise PermissionError('synthetic service read refusal')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(m.os,'readlink',denied)
    with pytest.raises((ValueError,PermissionError)):
        m.worker({'plan':case.plan,'context':case.context,'challenge':'b'*32})


@pytest.mark.parametrize('interrupt',[False,True])
def test_child_is_fresh_low_privilege_exec_and_interrupt_is_reaped(case,monkeypatch,interrupt):
    # Capture the real orchestration while substituting the process only.
    from importlib import reload
    actual=reload(m).verify_child
    monkeypatch.setattr(m,'BASE',case.base)
    monkeypatch.setattr(m,'guard',lambda *a,**kw:None)
    process=Mock(returncode=0)
    calls=[]
    def spawn(argv,**kw):
        calls.append((argv,kw))
        def communicate(data=None):
            if data is not None:
                request=json.loads(data)
                process.reply=b.canonical({'challenge':request['challenge'],'identity':b._expected(499,499),
                    'package_sha256':case.plan['package_sha256'],'signature_verified':True,
                    'managed_links_readable':True})
                if interrupt:raise KeyboardInterrupt()
            return process.reply,b''
        process.communicate.side_effect=communicate
        return process
    monkeypatch.setattr(m.subprocess,'Popen',spawn)
    if interrupt:
        with pytest.raises(ValueError,match='观察进程退出'):actual(case.plan,case.context,case.root/'slot')
    else:actual(case.plan,case.context,case.root/'slot')
    argv,kw=calls[0]
    assert argv[1:3]==['-I','-B'] and argv[-1]=='_publish_worker'
    assert kw['user']==kw['group']==499 and kw['extra_groups']==[]
    assert kw['close_fds'] and kw['pass_fds']==() and kw['cwd']=='/'
    assert set(kw['env'])=={'HOME','PATH','LANG'}
    assert process.communicate.call_count==(2 if interrupt else 1)
    process.kill.assert_not_called();process.terminate.assert_not_called()


@pytest.mark.parametrize('proof',[None,False])
def test_child_reply_requires_positive_managed_link_proof(case,monkeypatch,proof):
    from importlib import reload
    actual=reload(m).verify_child
    monkeypatch.setattr(m,'BASE',case.base)
    monkeypatch.setattr(m,'guard',lambda *a,**kw:None)
    process=Mock(returncode=0)
    def communicate(data):
        request=json.loads(data)
        result={'challenge':request['challenge'],'identity':b._expected(499,499),
                'package_sha256':case.plan['package_sha256'],'signature_verified':True}
        if proof is not None:result['managed_links_readable']=proof
        return b.canonical(result),b''
    process.communicate.side_effect=communicate
    monkeypatch.setattr(m.subprocess,'Popen',lambda *a,**kw:process)
    with pytest.raises(ValueError,match='回执不匹配'):
        actual(case.plan,case.context,case.root/'slot')


def test_stdlib_publisher_imports_under_real_system_python():
    repo=Path(__file__).resolve().parents[2]
    script="import sys; sys.path.insert(0,sys.argv[1]); import desktop_assistant.mac_release_publish; assert 'cryptography' not in sys.modules; assert 'app' not in sys.modules; print('stdlib_only')"
    result=subprocess.run(['/usr/bin/python3','-I','-B','-S','-c',script,str(repo)],capture_output=True,text=True,check=True)
    assert result.stdout.strip()=='stdlib_only'

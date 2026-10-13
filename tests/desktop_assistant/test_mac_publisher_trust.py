"""Synthetic root/context adapters, real Ed25519 and complete archive checks.

Never reads installed authority, generates a real publishing key or changes a
service. Only temporary test Ed25519 identities are used.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import mac_publisher_trust as trust, storage, release_request
from desktop_assistant.signing import public_bytes


@pytest.fixture
def scene(tmp_path,monkeypatch):
    base=tmp_path/'base';base.mkdir(mode=0o755);instance=base/'instance';instance.mkdir(mode=0o700)
    monkeypatch.setattr(trust,'BASE',base);monkeypatch.setattr(trust,'ROOT',instance)
    for key,name in (('CONTEXT','keychain-context.json'),('LEGACY_PUBLIC','publisher-public.pem'),
                     ('MAC_PUBLIC','mac-publisher-public.pem'),('RECORD','mac-publisher-trust.json')):
        monkeypatch.setattr(trust,key,base/name)
    legacy=Ed25519PrivateKey.generate();mac=Ed25519PrivateKey.generate();other=Ed25519PrivateKey.generate()
    context=dict(type='tianming.service-keychain.v1',installation_id='a'*32,uid=499,gid=499,host_sha256='b'*64)
    for path,raw in ((trust.CONTEXT,trust.canonical(context)),(trust.LEGACY_PUBLIC,public_bytes(legacy)),
                     (trust.MAC_PUBLIC,public_bytes(mac))):
        path.write_bytes(raw);path.chmod(0o644)
    # Ownership and ACL are adapters; file/link/type/mode/FD checks are real.
    original=trust._protected
    def ownership(info,*,directory=False,mode=0o644,uid=0,gid=None):
        if directory and uid==0:
            assert (info.st_mode & 0o170000)==0o040000
            return
        info=SimpleNamespace(**{k:getattr(info,k) for k in
            ('st_mode','st_uid','st_gid','st_nlink')})
        if info.st_uid==os.getuid():info.st_uid=uid
        if gid==499 and info.st_gid==os.getgid():info.st_gid=499
        return original(info,directory=directory,mode=mode,uid=uid,gid=gid)
    monkeypatch.setattr(trust,'_protected',ownership);monkeypatch.setattr(trust,'_no_acl',lambda fd:None)
    monkeypatch.setattr(trust,'sys',SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(trust,'_managed',lambda:True)
    tree=tmp_path/'payload';tree.mkdir();(tree/'main.py').write_bytes(b'print("synthetic")\n')
    metadata=dict(type='tianming.release.v1',version='synthetic',revision='r1',git_sha='c'*40,
        runtime_platform='macos-arm64',executable_files=['main.py'])
    packages={}
    for name,key in (('legacy',legacy),('mac',mac),('other',other)):
        path=tmp_path/(name+'.zip');storage.pack_tree(tree,path,metadata,key);packages[name]=path
    empty=dict(type=trust.TYPE,context_sha256=storage.sha(trust.CONTEXT),
        legacy_anchor_sha256=storage.sha(trust.LEGACY_PUBLIC),mac_publisher_sha256=storage.sha(trust.MAC_PUBLIC),packages={})
    def write(record):trust.RECORD.write_bytes(trust.canonical(record));trust.RECORD.chmod(0o644)
    def authorized(name='mac'):
        with zipfile.ZipFile(packages[name]) as archive:manifest=archive.read('manifest.json')
        return trust.authorize_package(empty,storage.sha(packages[name]),trust.digest(manifest))
    write(authorized())
    return SimpleNamespace(base=base,instance=instance,tmp=tmp_path,legacy=legacy,mac=mac,other=other,
        context=context,empty=empty,packages=packages,write=write,authorized=authorized,original_guard=original)


def consume(s,kind='inspect',name='mac'):
    if kind=='manifest':return storage.signed_release_manifest(s.packages[name],public_bytes(s.legacy))
    if kind=='extract':return storage.extract_verified(s.packages[name],s.tmp/'extracted',public_bytes(s.legacy))
    return release_request.inspect_package(s.packages[name],public_bytes(s.legacy))[1]


@pytest.mark.parametrize('kind',['manifest','inspect','extract'])
def test_exact_root_authorized_mac_signature_works_in_all_consumers(scene,kind):
    s=scene;before=trust.LEGACY_PUBLIC.read_bytes();manifest=consume(s,kind)
    assert manifest['git_sha']=='c'*40 and trust.LEGACY_PUBLIC.read_bytes()==before
    with zipfile.ZipFile(s.packages['mac']) as archive:
        proof=trust.verify_manifest(archive,archive.read('manifest.json'),archive.read('manifest.sig'),before)
        assert proof['authority']=='mac-root-authorized' and proof['signer_sha256']==s.empty['mac_publisher_sha256']
        proof['recheck']()


@pytest.mark.parametrize('kind',['manifest','inspect','extract'])
def test_historical_signature_never_depends_on_sidecar(scene,monkeypatch,kind):
    s=scene;trust.RECORD.write_bytes(b'corrupt root sidecar')
    monkeypatch.setattr(trust,'read_authority',lambda **kw:pytest.fail('legacy signature must remain independent'))
    assert consume(s,kind,'legacy')['revision']=='r1'


@pytest.mark.parametrize('fault',['empty','archive','manifest','signer','context','legacy-anchor','mac-fingerprint',
    'extra-field','duplicate-field','mac-public','corrupt','wrong-record-mode','record-link','record-hardlink','mac-key-link'])
def test_unapproved_or_changed_root_evidence_cannot_fallback(scene,fault):
    s=scene;record=s.authorized()
    if fault=='empty':record=s.empty
    elif fault=='archive':record['packages']={'d'*64:next(iter(record['packages'].values()))}
    elif fault=='manifest':next(iter(record['packages'].values()))['manifest_sha256']='d'*64
    elif fault=='signer':next(iter(record['packages'].values()))['signer_sha256']='d'*64
    elif fault=='context':record['context_sha256']='d'*64
    elif fault=='legacy-anchor':record['legacy_anchor_sha256']='d'*64
    elif fault=='mac-fingerprint':record['mac_publisher_sha256']='d'*64
    elif fault=='extra-field':record['unexpected']=True
    s.write(record)
    if fault=='duplicate-field':trust.RECORD.write_bytes(trust.RECORD.read_bytes()[:-1]+b',"type":"other"}')
    elif fault=='corrupt':trust.RECORD.write_bytes(b'not JSON')
    elif fault=='mac-public':trust.MAC_PUBLIC.write_bytes(public_bytes(s.other))
    elif fault=='wrong-record-mode':trust.RECORD.chmod(0o666)
    elif fault=='record-link':
        other=s.tmp/'sidecar';trust.RECORD.rename(other);trust.RECORD.symlink_to(other)
    elif fault=='record-hardlink':os.link(trust.RECORD,s.tmp/'other-record')
    elif fault=='mac-key-link':
        other=s.tmp/'public';trust.MAC_PUBLIC.rename(other);trust.MAC_PUBLIC.symlink_to(other)
    with pytest.raises((ValueError,OSError)):consume(s)


def test_unauthorized_signature_and_embedded_key_never_become_authority(scene):
    s=scene;s.write(s.authorized('other'))
    with pytest.raises(InvalidSignature):consume(s,name='other')
    path=s.packages['other'];changed=s.tmp/'embedded-key.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(changed,'x') as output:
        manifest=json.loads(source.read('manifest.json'));manifest['publisher_public_key']=public_bytes(s.other).decode()
        raw=json.dumps(manifest).encode()
        for info in source.infolist():
            data=raw if info.filename=='manifest.json' else s.other.sign(raw) if info.filename=='manifest.sig' else source.read(info.filename)
            output.writestr(info,data)
    s.packages['other']=changed;s.write(s.authorized('other'))
    with pytest.raises(InvalidSignature):consume(s,name='other')


@pytest.mark.parametrize('kind',['inspect','extract'])
def test_authority_does_not_skip_payload_file_hashes(scene,kind):
    s=scene;original=s.packages['mac'];changed=s.tmp/'corrupt-payload.zip'
    with zipfile.ZipFile(original) as source,zipfile.ZipFile(changed,'x') as output:
        for info in source.infolist():
            output.writestr(info,b'altered source' if info.filename=='main.py' else source.read(info.filename))
    s.packages['mac']=changed;s.write(s.authorized())
    with pytest.raises(ValueError,match='校验|哈希'):consume(s,kind)


def test_sidecar_cannot_be_used_outside_darwin_managed_or_root(scene,monkeypatch):
    s=scene;monkeypatch.setattr(trust,'_managed',lambda:False)
    monkeypatch.setattr(trust,'read_authority',lambda **kw:pytest.fail('unmanaged caller read sidecar'))
    with pytest.raises(InvalidSignature):consume(s)


def test_non_signature_error_never_triggers_fallback(scene,monkeypatch):
    s=scene;monkeypatch.setattr(trust,'read_authority',lambda **kw:pytest.fail('bad key cannot fallback'))
    with zipfile.ZipFile(s.packages['mac']) as archive,pytest.raises(ValueError):
        trust.verify_manifest(archive,archive.read('manifest.json'),archive.read('manifest.sig'),b'bad public key')


@pytest.mark.parametrize('fault',['archive-replaced','record-changed'])
def test_verification_recheck_rejects_mutation_before_use(scene,fault):
    s=scene;path=s.packages['mac']
    with zipfile.ZipFile(path) as archive:
        proof=trust.verify_manifest(archive,archive.read('manifest.json'),archive.read('manifest.sig'),public_bytes(s.legacy))
        if fault=='archive-replaced':
            raw=path.read_bytes();path.rename(s.tmp/'old-archive');path.write_bytes(raw)
        else:s.write(s.empty)
        with pytest.raises(ValueError):proof['recheck']()


def test_pure_builder_allows_empty_and_large_history_but_never_overwrites(scene):
    s=scene;r=deepcopy(s.empty)
    trust.validate_record(r,context_sha256=r['context_sha256'],legacy_anchor_sha256=r['legacy_anchor_sha256'],
        mac_publisher_sha256=r['mac_publisher_sha256'])
    for number in range(256):r=trust.authorize_package(r,f'{number:064x}',f'{number+256:064x}')
    assert len(r['packages'])==256 and s.empty['packages']=={}
    for package,manifest in (('0'*64,'1'*64),(f'{300:064x}',f'{301:064x}')):
        with pytest.raises(ValueError):trust.authorize_package(r,package,manifest)
    with pytest.raises(ValueError):trust.authorize_package(s.authorized(),storage.sha(s.packages['mac']),'d'*64)


def test_root_owner_and_mode_checks_reject_nonroot_records(scene):
    s=scene
    for owner,mode in ((501,0o100644),(0,0o100666)):
        info=SimpleNamespace(st_mode=mode,st_uid=owner,st_gid=0,st_nlink=1)
        with pytest.raises(ValueError):s.original_guard(info)


def test_root_authority_expected_context_and_identity_are_binding(scene):
    s=scene;result=trust.read_authority(expected_context_sha256=s.empty['context_sha256'],
        expected_legacy_anchor_sha256=s.empty['legacy_anchor_sha256'],expected_mac_publisher_sha256=s.empty['mac_publisher_sha256'])
    assert result['mac_public']==public_bytes(s.mac) and result['legacy_public']==public_bytes(s.legacy)
    for key in ('expected_context_sha256','expected_legacy_anchor_sha256','expected_mac_publisher_sha256'):
        with pytest.raises(ValueError):trust.read_authority(**{key:'d'*64})


def test_root_helpers_import_without_crypto_under_apple_isolated_python():
    import subprocess
    import sys
    if sys.platform!='darwin':pytest.skip('Apple Python stdlib helper check; no root action')
    source=Path(trust.__file__).resolve().parent.parent
    script='''import sys,base64,hashlib
sys.path.insert(0,sys.argv[1])
from desktop_assistant import mac_publisher_trust as m
raw=b'-----BEGIN PUBLIC KEY-----\\n'+base64.b64encode(bytes.fromhex('302a300506032b6570032100')+bytes(32))+b'\\n-----END PUBLIC KEY-----\\n'
assert m.public_fingerprint(raw)==hashlib.sha256(raw).hexdigest()
r=dict(type=m.TYPE,context_sha256='a'*64,legacy_anchor_sha256='b'*64,mac_publisher_sha256=m.public_fingerprint(raw),packages={})
m.validate_record(r,context_sha256=r['context_sha256'],legacy_anchor_sha256=r['legacy_anchor_sha256'],mac_publisher_sha256=r['mac_publisher_sha256'])
m.authorize_package(r,'c'*64,'d'*64)
assert not any(name.startswith('cryptography') for name in sys.modules)
'''
    subprocess.run(['/usr/bin/python3','-I','-S','-B','-c',script,str(source)],check=True,
        capture_output=True,env={'PATH':'/usr/bin:/bin'},cwd='/')


def test_apple_root_and_service_selection_are_explicit(monkeypatch):
    from app.core import mac_service_keychain
    monkeypatch.setattr(trust,'sys',SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(trust.os,'getuid',lambda:0);monkeypatch.setattr(trust.os,'geteuid',lambda:0)
    assert trust._managed()
    monkeypatch.setattr(trust.os,'getuid',lambda:501);monkeypatch.setattr(trust.os,'geteuid',lambda:501)
    monkeypatch.setattr(mac_service_keychain,'selected',lambda:False)
    assert not trust._managed()
    monkeypatch.setattr(mac_service_keychain,'selected',lambda:True)
    assert trust._managed()
    monkeypatch.setattr(trust,'sys',SimpleNamespace(platform='linux'))
    assert not trust._managed()

"""Offline build gates use synthetic wheels; real pinned wheels are built separately."""
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest
from desktop_assistant import mac_smb_stream_build as m
from desktop_assistant import build

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac native build file modes and sandbox contract')


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    paths=[];specs={}
    for name,module,version in [('smbprotocol','smbprotocol','1.17.0'),('pyspnego','spnego','0.12.4')]:
        wheel=f'{name}-{version}-py3-none-any.whl';path=tmp_path/wheel
        dist=f'{name}-{version}.dist-info'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr(module+'/__init__.py',b'# synthetic module\n')
            z.writestr(dist+'/METADATA',f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
        paths.append(path)
        specs[wheel]=dict(name=name,version=version,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),roots={module,dist})
    monkeypatch.setattr(m,'WHEELS',specs)
    sources={name:b'# synthetic source' for name in m.cfg.FILES}
    sources[m.REQUIREMENTS]=b'# pinned\nsmbprotocol==1.17.0\npyspnego==0.12.4\n'
    runtime=tmp_path/'new-payload/runtime';site=runtime/'lib/python3.12/site-packages';site.mkdir(parents=True)
    return SimpleNamespace(paths=paths,sources=sources,runtime=runtime,site=site,specs=specs)


def test_validated_bytes_install_offline_into_only_new_runtime(fixture):
    a=fixture;before={p:p.read_bytes() for p in a.paths}
    wheels=m.inputs(a.sources,a.paths);m.install(a.runtime,wheels)
    assert {p:p.read_bytes() for p in a.paths}==before
    versions=dict(json.loads((a.runtime/'dependency-versions.json').read_text()))
    assert versions==m.VERSIONS
    for members in wheels.values():
        for name,raw in members.items():
            assert (a.site/name).read_bytes()==raw and stat.S_IMODE((a.site/name).stat().st_mode)==0o644


@pytest.mark.parametrize('fault',['source','requirements','missing','duplicate','hash','symlink','name'])
def test_bad_inputs_reject_before_any_payload_write(fixture,fault):
    a=fixture
    if fault=='source':a.sources.pop(m.cfg.ENTRY)
    if fault=='requirements':a.sources[m.REQUIREMENTS]=b'smbprotocol>=1\npyspnego==0.12.4'
    if fault=='missing':a.paths.pop()
    if fault=='duplicate':a.paths[1]=a.paths[0]
    if fault=='hash':a.paths[0].write_bytes(a.paths[0].read_bytes()+b'changed')
    if fault=='symlink':
        target=a.paths[0].with_suffix('.saved');a.paths[0].rename(target);a.paths[0].symlink_to(target)
    if fault=='name':a.paths[0]=a.paths[0].with_name('other.whl')
    with pytest.raises(ValueError):m.inputs(a.sources,a.paths)
    assert list(a.site.iterdir())==[]


@pytest.mark.parametrize('name,mode',[('../escape',0o100644),('/absolute',0o100644),
    ('smbprotocol/../../escape',0o100644),('smbprotocol/./bad',0o100644),
    ('smbprotocol//bad',0o100644),('smbprotocol\\bad',0o100644),
    ('outside/module.py',0o100644),('smbprotocol/link',0o120777),
    ('smbprotocol/fifo',0o010600),('smbprotocol/',0o040755)])
def test_unapproved_members_rejected_even_with_test_hash(fixture,name,mode):
    a=fixture;path=a.paths[0]
    with zipfile.ZipFile(path,'a') as z:
        info=zipfile.ZipInfo(name);info.external_attr=mode<<16;z.writestr(info,b'synthetic')
    a.specs[path.name]['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):m.inputs(a.sources,a.paths)


@pytest.mark.parametrize('kind',['module','link','old-dist'])
def test_existing_runtime_dependency_is_never_overwritten(fixture,kind):
    a=fixture;wheels=m.inputs(a.sources,a.paths)
    if kind=='module':(a.site/'spnego').mkdir()
    if kind=='link':(a.site/'spnego').symlink_to(a.runtime/'missing')
    if kind=='old-dist':
        dist=a.site/'pyspnego-0.1.dist-info';dist.mkdir();(dist/'METADATA').write_text('Name: pyspnego\nVersion: 0.1\n')
    before=sorted(str(p.relative_to(a.site)) for p in a.site.rglob('*'))
    with pytest.raises(ValueError):m.install(a.runtime,wheels)
    assert sorted(str(p.relative_to(a.site)) for p in a.site.rglob('*'))==before


def test_isolated_runtime_probe_is_network_denied_and_reply_checked(tmp_path,monkeypatch):
    tree=tmp_path/'payload';calls=[]
    proof=dict(versions=m.VERSIONS,isolated=True,packaged_imports=True,new_connection_denied=True)
    def run(args,**kwargs):
        calls.append(args)
        assert args[:3]==['/usr/bin/sandbox-exec','-p',m.cfg.PROFILE]
        assert args[3:7]==[str(tree/'runtime/bin/python3.12'),'-I','-B','-c']
        assert kwargs['env']=={'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'}
        assert kwargs['check'] and kwargs['timeout']==60 and kwargs['cwd']==tree
        return SimpleNamespace(stdout=json.dumps(proof))
    monkeypatch.setattr(m.subprocess,'run',run)
    assert m.verify(tree)['new_connection_denied']
    proof['versions']={'smbprotocol':'other'}
    with pytest.raises(ValueError):m.verify(tree)
    assert len(calls)==2


def test_windows_cannot_request_smb_stream_before_source_or_output_io(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'argv',['build','--repo',str(tmp_path),'--runtime-base',str(tmp_path),
        '--site-packages',str(tmp_path),'--ocr-models',str(tmp_path),'--output',str(tmp_path/'new'),
        '--revision','r1','--version','v1','--signing-request-only','--trusted-public-key',str(tmp_path/'public'),
        '--smb-stream-wheels','one.whl','two.whl'])
    with pytest.raises(SystemExit) as error:build.main()
    assert error.value.code==2 and not (tmp_path/'new').exists()


@pytest.mark.parametrize('enabled,failed,platform,omitted',[
    (False,False,'macos-arm64',None),(True,False,'macos-arm64',None),
    (True,True,'macos-arm64',None),(False,False,'windows',None),
    (False,False,'macos-arm64','mac_system_upgrade_worker.py'),
    (True,False,'macos-arm64','mac_system_upgrade_contract.py')])
def test_builder_declares_capability_only_after_explicit_verified_runtime(tmp_path,monkeypatch,enabled,failed,platform,omitted):
    # Exercise the real main() wiring; packaging and heavy native copies are
    # explicit fakes, not an actual native build or signature.
    from desktop_assistant import native_runtime,ocr_models
    from desktop_assistant.mac_release_publish import SERVICE_RESTORE_FILES
    from desktop_assistant.mac_factory_runtime import EXECUTION_FILES
    repo=Path(build.__file__).resolve().parents[1]
    sources={str(p.relative_to(repo)):p.read_bytes() for p in (repo/'desktop_assistant').glob('*.py')}
    sources.update({name:b'# fixture' for name in SERVICE_RESTORE_FILES|EXECUTION_FILES})
    # The builder's existing inventory reader gates are intentionally exercised.
    import ast
    for node in ast.walk(ast.parse(Path(build.__file__).read_text())):
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='reader_sources' for t in node.targets):
            sources.update({name:b'# fixture' for name in ast.literal_eval(node.value)})
    if omitted:sources.pop('desktop_assistant/'+omitted)
    calls=[];output=tmp_path/'output'
    argv=['build','--repo',str(repo),'--runtime-base',str(tmp_path/'base'),
        '--site-packages',str(tmp_path/'site'),'--ocr-models',str(tmp_path/'ocr'),'--output',str(output),
        '--runtime-platform',platform,'--revision','r1','--version','v1',
        '--signing-request-only','--trusted-public-key',str(tmp_path/'public')]
    if enabled:argv+=['--smb-stream-wheels','one.whl','two.whl']
    monkeypatch.setattr(sys,'argv',argv)
    monkeypatch.setattr(build,'source_snapshot',lambda *a:('a'*40,sources))
    monkeypatch.setattr(build,'schema_contract_from_sources',lambda *a:{})
    (tmp_path/'public').write_bytes(b'synthetic public')
    from desktop_assistant import release_request
    monkeypatch.setattr(release_request,'public_identity',lambda *a:None)
    monkeypatch.setattr(release_request,'prepare_request',lambda *a:{})
    monkeypatch.setattr(native_runtime,'assemble',lambda *a:a[2].mkdir())
    monkeypatch.setattr(build.shutil,'copytree',lambda src,dst,**kw:dst.mkdir(parents=True,exist_ok=True))
    monkeypatch.setattr(native_runtime,'verify_relocation',lambda *a:{})
    monkeypatch.setattr(native_runtime,'executable_files',lambda *a:[])
    monkeypatch.setattr(ocr_models,'copy_models',lambda *a:[])
    monkeypatch.setattr(ocr_models,'prepare_readonly_layout',lambda *a:None)
    monkeypatch.setattr(build,'remove_transient_build_trees',lambda *a:None)
    monkeypatch.setattr(m,'inputs',lambda *a:calls.append('inputs') or {'validated':True})
    monkeypatch.setattr(m,'install',lambda *a:calls.append('install'))
    def verify(*args):
        calls.append('verify')
        if failed:raise ValueError('synthetic failed relocation')
        return {'synthetic-proof':True}
    monkeypatch.setattr(m,'verify',verify)
    manifests=[];monkeypatch.setattr(build,'pack_tree',lambda tree,path,manifest,key:manifests.append(manifest))
    if omitted:
        with pytest.raises(ValueError,match='监护升级'):build.main()
        assert not manifests and not output.exists()
        assert calls==(['inputs'] if enabled else [])
        return
    if failed:
        with pytest.raises(ValueError):build.main()
        assert not manifests
    else:
        build.main();assert len(manifests)==1
        capabilities=manifests[0].get('runtime_capabilities',{})
        assert (capabilities.get(m.cfg.CAPABILITY)==1)==enabled
        assert (capabilities.get('system_upgrade_worker_v1')==1)==(platform=='macos-arm64')
        for name in ('current_mac_worker_v1','current_mac_audit_v3','factory_current_mac_execution_v3'):
            assert (capabilities.get(name)==1)==(platform=='macos-arm64')
        assert (capabilities.get('current_mac_worker_broker_v1')==1)==enabled
    assert calls==(['inputs','install','verify'] if enabled else [])

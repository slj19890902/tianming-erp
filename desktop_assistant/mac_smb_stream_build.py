"""Offline, opt-in SMB stream dependencies for a new Mac release payload."""
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import zipfile

from . import mac_smb_stream_runtime_contract as cfg
from .mac_smb_stream_transport import VERSIONS

WHEELS = {
    'smbprotocol-1.17.0-py3-none-any.whl': {
        'name':'smbprotocol', 'version':'1.17.0',
        'sha256':'bd1abff5417f5af83ca516a64ab8e5acece3dbdcf58d4e5e23e47f5165a77349',
        'roots':{'smbclient','smbprotocol','smbprotocol-1.17.0.dist-info'}},
    'pyspnego-0.12.4-py3-none-any.whl': {
        'name':'pyspnego', 'version':'0.12.4',
        'sha256':'a29a34de1abe9e9b30d9aa1d363f8e49fc159eb7355b1f2ee86ccb3a2ce3bba9',
        'roots':{'spnego','pyspnego-0.12.4.dist-info'}},
}
REQUIREMENTS = 'desktop_assistant/requirements-macos-smb-stream.txt'
ERROR = 'SMB流构建依赖未通过固定来源核验，保留构建现场'


def require(ok):
    if not ok: raise ValueError(ERROR)


def inputs(sources, paths):
    require(cfg.FILES <= sources.keys() and REQUIREMENTS in sources)
    lines={line.strip() for line in sources[REQUIREMENTS].decode('utf-8').splitlines()
           if line.strip() and not line.lstrip().startswith('#')}
    require(lines=={name+'=='+version for name,version in VERSIONS.items()})
    require(len(paths)==len(WHEELS) and {Path(p).name for p in paths}==set(WHEELS))
    result={}
    for path in map(Path,paths):
        spec=WHEELS[path.name]
        require(path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=4*1024**2)
        raw=path.read_bytes();require(hashlib.sha256(raw).hexdigest()==spec['sha256'])
        entries={};total=0
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for item in archive.infolist():
                name=PurePosixPath(item.filename);mode=item.external_attr>>16
                require(not item.is_dir() and not name.is_absolute() and '\\' not in item.filename
                    and all(part not in ('','..','.') for part in item.filename.split('/'))
                    and name.parts[0] in spec['roots'] and item.filename not in entries
                    and stat.S_IFMT(mode) in (0,stat.S_IFREG) and not item.flag_bits&1)
                total+=item.file_size;require(total<=16*1024**2)
                entries[item.filename]=archive.read(item)
        require({PurePosixPath(name).parts[0] for name in entries}==spec['roots'])
        result[path.name]=entries
    return result


def install(runtime, wheels):
    """Only the caller's newly assembled runtime; no pip or source-env mutation."""
    require(set(wheels)==set(WHEELS))
    site=runtime/'lib/python3.12/site-packages'
    require(site.is_dir() and not site.is_symlink())
    existing={dist.metadata['Name'].lower().replace('_','-')
              for dist in importlib.metadata.distributions(path=[str(site)])}
    require(not existing.intersection(VERSIONS))
    roots=set().union(*(spec['roots'] for spec in WHEELS.values()))
    require(all(not (site/name).exists() and not (site/name).is_symlink() for name in roots))
    for entries in wheels.values():
        for name,raw in entries.items():
            target=site/name;target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as stream:
                os.fchmod(stream.fileno(),0o644);stream.write(raw)
    versions=sorted((dist.metadata['Name'],dist.version)
                    for dist in importlib.metadata.distributions(path=[str(site)]))
    (runtime/'dependency-versions.json').write_text(json.dumps(versions,indent=2),encoding='utf-8')


def verify(tree):
    script='''
import importlib, importlib.metadata, json, socket, sys
from pathlib import Path
tree=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(tree))
assert sys.flags.isolated and sys.dont_write_bytecode
assert Path(sys.prefix).resolve()==tree/'runtime'
expected=json.loads(sys.argv[2])
versions={name:importlib.metadata.version(name) for name in expected}
assert versions==expected
site=tree/'runtime/lib/python3.12/site-packages'
for name in ('smbprotocol','smbprotocol.connection','spnego','cryptography'):
    module=importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(site)
for name in ('mac_smb_stream_runtime','mac_smb_stream_setup','mac_smb_stream_target','mac_smb_stream_work',
             'mac_smb_stream_files_v2','mac_smb_stream_probe_v2',
             'current_mac_worker','current_mac_worker_contract','current_mac_smb_broker',
             'current_mac_target_audit','current_mac_cutover','current_mac_cutover_contract',
             'mac_factory_runtime','current_mac_publication'):
    module=importlib.import_module('desktop_assistant.'+name)
    assert Path(module.__file__).resolve().is_relative_to(tree/'desktop_assistant')
with socket.socket() as probe:
    try: probe.connect(('127.0.0.1',9))
    except PermissionError: pass
    else: raise AssertionError('outbound network not denied')
print(json.dumps({'versions':versions,'isolated':True,'packaged_imports':True,'new_connection_denied':True}))
'''
    environment={'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'}
    result=subprocess.run(['/usr/bin/sandbox-exec','-p',cfg.PROFILE,
        str(tree/'runtime/bin/python3.12'),'-I','-B','-c',script,str(tree),json.dumps(VERSIONS)],
        cwd=tree,env=environment,capture_output=True,text=True,timeout=60,check=True)
    proof=json.loads(result.stdout)
    require(proof==dict(versions=VERSIONS,isolated=True,packaged_imports=True,new_connection_denied=True))
    return {**proof,'wheel_sha256':{name:spec['sha256'] for name,spec in WHEELS.items()}}

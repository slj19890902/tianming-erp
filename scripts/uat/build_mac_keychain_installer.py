"""Build only the reviewed stdlib installer payload from exact committed bytes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

FILES = ('app/__init__.py', 'app/core/__init__.py', 'app/core/mac_keychain.py',
         'app/core/mac_service_keychain.py', 'desktop_assistant/__init__.py',
         'desktop_assistant/mac_service_identity.py', 'desktop_assistant/mac_keychain_install.py',
         'desktop_assistant/mac_release_publish.py', 'desktop_assistant/mac_system_service.py',
         'desktop_assistant/mac_archive_contract.py', 'desktop_assistant/mac_archive_install.py',
         'desktop_assistant/mac_mount.py', 'desktop_assistant/factory_target_contract.py',
         'desktop_assistant/factory_network.py', 'desktop_assistant/mac_factory_request_install.py',
         'desktop_assistant/factory_activation_contract.py',
         'desktop_assistant/mac_factory_activation_install.py', 'desktop_assistant/home_process.py',
         'desktop_assistant/mac_smb_stream_contract.py',
         'desktop_assistant/mac_smb_stream_runtime_contract.py',
         'desktop_assistant/mac_smb_stream_setup.py')


def build(repo, commit, output):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Require a full committed Git SHA')
    contents = {}
    for name in FILES:
        entry = subprocess.check_output(['git', 'ls-tree', commit, '--', name], cwd=repo).decode()
        if not entry.startswith(('100644 blob ', '100755 blob ')) or not entry.endswith('\t' + name + '\n'):
            raise ValueError('Missing regular committed installer source')
        raw = subprocess.check_output(['git', 'show', commit + ':' + name], cwd=repo)
        if not 0 < len(raw) <= 65536:
            raise ValueError('Installer source length is invalid')
        contents[name] = raw
    manifest = {'type': 'tianming.keychain-code.v1', 'source': commit,
                'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}}
    contents['install-manifest.json'] = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    with Path(output).open('xb') as target:
        os.fchmod(target.fileno(), 0o600)
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in sorted(contents.items()):
                entry = zipfile.ZipInfo(name, date_time=(2026, 10, 11, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100444 << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, raw)
        target.flush(); os.fsync(target.fileno())
    with zipfile.ZipFile(output) as archive:
        if set(archive.namelist()) != set(contents) or len(archive.namelist()) != len(contents):
            raise ValueError('Installer pack inventory readback failed')
        for name, raw in contents.items():
            if archive.read(name) != raw:
                raise ValueError('Installer pack byte readback failed')
    raw = Path(output).read_bytes()
    return {'source': commit, 'files': len(contents), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'manifest_sha256': hashlib.sha256(contents['install-manifest.json']).hexdigest(),
            'contains_credentials': False, 'runtime_release': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.repo, args.commit, args.output)))

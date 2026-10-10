"""Assemble a relocatable, explicit macOS arm64 Python 3.12 runtime."""
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def safe_copy(source, destination, ignore):
    source = Path(source).resolve()
    for path in source.rglob('*'):
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(source) or resolved.is_dir():
                raise ValueError('运行环境包含外部或目录链接，拒绝打包')
    shutil.copytree(source, destination, ignore=ignore)


def assemble(base, site_packages, destination, ignore):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('Mac原生包必须在Apple Silicon Mac构建并验证')
    executable = Path(base) / 'bin/python3.12'
    with executable.open('rb') as stream:
        header = stream.read(8)
    # Official standalone build is a thin little-endian arm64 Mach-O executable.
    if header != b'\xcf\xfa\xed\xfe\x0c\x00\x00\x01':
        raise ValueError('运行时不是原生arm64 Mach-O Python')
    safe_copy(base, destination, ignore)
    safe_copy(site_packages, destination / 'lib/python3.12/site-packages',
              shutil.ignore_patterns('__pycache__', '*.pyc'))
    (destination / 'bin/python3.12').chmod(0o755)
    versions = sorted((dist.metadata['Name'], dist.version)
                      for dist in importlib.metadata.distributions(path=[str(site_packages)]))
    (destination / 'dependency-versions.json').write_text(json.dumps(versions, indent=2), encoding='utf-8')


def executable_files(tree):
    return sorted(path.relative_to(tree).as_posix() for path in tree.rglob('*')
                  if path.is_file() and path.stat().st_mode & 0o111)


def verify_relocation(tree):
    script = '''
import sys, json, platform
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core.home_rehearsal import install_network_guard
install_network_guard()
assert sys.version_info[:2] == (3, 12)
assert platform.machine() == 'arm64'
assert Path(sys.prefix).resolve() == (Path(sys.argv[1]) / 'runtime').resolve()
import fastapi, sqlalchemy, alembic, reportlab, pymupdf, torch, easyocr, bcrypt, jwt
print(json.dumps({'python': sys.version.split()[0], 'machine': platform.machine(),
                  'prefix': sys.prefix, 'core_dependencies_imported': True}))
'''
    env = {key: value for key, value in os.environ.items() if key in ('PATH', 'TMPDIR', 'LANG')}
    env['ERP_HOME_REHEARSAL'] = '1'
    result = subprocess.run([str(tree / 'runtime/bin/python3.12'), '-I', '-c', script, str(tree)],
                            cwd=tree, env=env, capture_output=True, text=True, timeout=180, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])

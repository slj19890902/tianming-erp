"""Build on the development PC. Runtime includes installed dependencies; no business data."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant.storage import pack_tree, sha, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--runtime-base', type=Path, required=True)
    parser.add_argument('--site-packages', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--signing-key', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--package-only', action='store_true', help='仅构建签名更新包，不重复生成安装器')
    args = parser.parse_args()
    root, output = args.repo.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError('构建输出必须是新目录')
    if root == output or root in output.parents:
        raise ValueError('构建输出必须在仓库之外')
    output.mkdir(parents=True)
    if not args.signing_key.is_file():
        raise ValueError('发布私钥必须预先生成并安全保存，构建不得自动更换发布身份')
    key = serialization.load_pem_private_key(args.signing_key.read_bytes(), password=None)
    public = output / 'release-public.pem'
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    tree = output / 'payload'
    tree.mkdir()
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode('utf-8').split('\0')
    allowed = {'app', 'alembic', 'static', 'templates', 'desktop_assistant'}
    singles = {'main.py', 'alembic.ini', 'requirements.txt'}
    for relative in tracked:
        if not relative:
            continue
        path = Path(relative)
        if path.parts[0] not in allowed and relative not in singles:
            continue
        if relative.startswith('static/uploads/') or relative.endswith('.pyc') or '__pycache__' in path.parts:
            continue
        source = root / path
        if source.is_symlink() or source.is_junction():
            raise ValueError('发布源码不得含链接')
        destination = tree / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    runtime = tree / 'runtime'
    shutil.copytree(args.runtime_base, runtime, ignore=shutil.ignore_patterns('site-packages', '__pycache__', 'Scripts'))
    shutil.copytree(args.site_packages, runtime / 'Lib/site-packages',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Force a relocatable Python search path, independent of machine registry and PYTHONHOME.
    (runtime / 'python312._pth').write_text('python312.zip\n.\nLib\nDLLs\nLib/site-packages\n..\nimport site\n', encoding='ascii')
    code_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    package = output / 'release.zip'
    pack_tree(tree, package, {'type': 'tianming.release.v1', 'version': args.version,
                            'revision': args.revision, 'git_sha': code_sha}, key)
    if args.package_only:
        write_json(output / 'build-result.json', {'git_sha': code_sha, 'version': args.version,
                   'release_sha256': sha(package), 'installer_built': False})
        return
    common = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
              '--paths', str(root), '--specpath', str(output), '--workpath', str(output / 'pyi-work'),
              '--distpath', str(output)]
    subprocess.run([*common, '--name', 'TianmingERP-Assistant', '--add-data', str(public) + ';.',
                    str(root / 'desktop_assistant/gui.py')], check=True, cwd=root)
    subprocess.run([*common, '--name', 'TianmingERP-Setup',
                    '--add-data', str(output / 'TianmingERP-Assistant.exe') + ';.',
                    '--add-data', str(package) + ';.', str(root / 'desktop_assistant/installer.py')], check=True, cwd=root)
    write_json(output / 'build-result.json', {'git_sha': code_sha, 'version': args.version,
               'release_sha256': sha(package), 'installer_sha256': sha(output / 'TianmingERP-Setup.exe')})


if __name__ == '__main__':
    main()

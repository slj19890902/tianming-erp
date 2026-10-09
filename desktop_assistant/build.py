"""Build on the development PC. Runtime includes installed dependencies; no business data."""
import argparse
import ast
import io
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant.storage import archive_path, pack_tree, sha, write_json
from desktop_assistant.schema_contract import schema_contract_from_sources


def remove_transient_build_trees(output: Path) -> None:
    """Remove reproducible build trees after durable artifacts are complete."""
    output = archive_path(output)
    for name in ("payload", "pyi-work"):
        path = output / name
        if path.is_symlink() or path.is_junction() or archive_path(path).parent != output:
            raise ValueError(f"构建临时目录不得为链接: {path}")
        if path.exists():
            shutil.rmtree(path)


def runtime_copy_ignore(_directory, names):
    """Drop content that is rebuilt explicitly for every signed package."""
    rebuilt = {'site-packages', '__pycache__', 'Scripts', 'ocr'}
    return {name for name in names if name in rebuilt}


def source_snapshot(root, version, revision):
    """Bind metadata and every source byte to one committed Git tree."""
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=root):
        raise ValueError('构建前请提交源码改动，工作区必须干净')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    raw = subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                                   'archive', '--format=zip', commit], cwd=root)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        contents = {}
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('发布源码不得含链接')
            contents[entry.filename] = archive.read(entry)

    def literal(source, name):
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
        raise ValueError('发布源码缺少版本字段: ' + name)

    if literal(contents['app/version.py'], 'APP_VERSION') != version:
        raise ValueError('安装包版本必须与程序 APP_VERSION 一致')
    chain = {}
    for name, source in contents.items():
        if name.startswith('alembic/versions/') and name.endswith('.py') and not name.endswith('/__init__.py'):
            current = literal(source, 'revision')
            if current in chain:
                raise ValueError('迁移 revision 重复')
            parent = literal(source, 'down_revision')
            chain[current] = () if parent is None else ((parent,) if isinstance(parent, str) else tuple(parent))
    parents = {parent for values in chain.values() for parent in values}
    if parents - chain.keys() or set(chain) - parents != {revision}:
        raise ValueError('安装包 revision 必须与源码唯一迁移 head 一致')
    return commit, contents


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--runtime-base', type=Path, required=True)
    parser.add_argument('--site-packages', type=Path, required=True)
    parser.add_argument('--ocr-models', type=Path, required=True, help='已下载的 EasyOCR model 目录')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--signing-key', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--package-only', action='store_true', help='仅构建签名更新包，不重复生成安装器')
    parser.add_argument('--upgrade-from-revision')
    parser.add_argument('--rollback-package-sha256')
    args = parser.parse_args()
    migration = None
    if args.upgrade_from_revision or args.rollback_package_sha256:
        if (not args.upgrade_from_revision or not args.rollback_package_sha256
                or not re.fullmatch(r'[0-9a-f]{64}', args.rollback_package_sha256)):
            raise ValueError('跨版本包必须同时指定起始revision及已验证可回退程序包SHA256')
        migration = {'policy': 'preserve_existing_facts_v1', 'from_revision': args.upgrade_from_revision,
                     'rollback_package_sha256': args.rollback_package_sha256}
    root, output = args.repo.resolve(), archive_path(args.output)
    code_sha, sources = source_snapshot(root, args.version, args.revision)
    reader_sources = {'app/services/processed_component_stock.py',
                      'app/services/stock_preparation_processing.py',
                      'app/services/stock_preparation_assembly.py',
                      'app/services/quotation_mutations.py', 'app/api/quotations.py',
                      'app/services/shared_finished_stock.py', 'app/models/shared_finished_stock.py',
                      'app/services/shared_finished_management.py', 'app/services/shared_finished_receipts.py'}
    if not reader_sources <= sources.keys():
        raise ValueError('库存业务读取契约的实现不完整，禁止签名发布')
    schema_contract = schema_contract_from_sources(sources, args.revision)
    if output.exists():
        raise ValueError('构建输出必须是新目录')
    if root == output or root in output.parents:
        raise ValueError('构建输出必须在仓库之外')
    output.mkdir(parents=True)
    if not args.signing_key.is_file():
        raise ValueError('发布私钥必须预先生成并安全保存，构建不得自动更换发布身份')
    from desktop_assistant.signing import load_key
    key = load_key(args.signing_key)
    public = output / 'release-public.pem'
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    tree = output / 'payload'
    tree.mkdir()
    allowed = {'app', 'alembic', 'static', 'templates', 'desktop_assistant'}
    singles = {'main.py', 'alembic.ini', 'requirements.txt', 'scripts/admin/release_erp.ps1',
               'scripts/admin/confirm_shared_finished_pilot.py',
               'scripts/admin/confirm_shared_customer_codes.py'}
    for relative, content in sources.items():
        if not relative:
            continue
        path = Path(relative)
        frontend_asset = relative.startswith('frontend-v2/dist/')
        if path.parts[0] not in allowed and relative not in singles and not frontend_asset:
            continue
        if relative.startswith('static/uploads/') or relative.endswith('.pyc') or '__pycache__' in path.parts:
            continue
        destination = tree / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    runtime = tree / 'runtime'
    shutil.copytree(args.runtime_base, runtime, ignore=runtime_copy_ignore)
    shutil.copytree(args.site_packages, runtime / 'Lib/site-packages',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    from desktop_assistant.ocr_models import copy_models
    models = copy_models(args.ocr_models, args.site_packages, runtime / 'ocr/model')
    # Force a relocatable Python search path, independent of machine registry and PYTHONHOME.
    (runtime / 'python312._pth').write_text('python312.zip\n.\nLib\nDLLs\nLib/site-packages\n..\nimport site\n', encoding='ascii')
    package = output / 'release.zip'
    pack_tree(tree, package, {'type': 'tianming.release.v1', 'version': args.version,
                            'revision': args.revision, 'git_sha': code_sha, 'migration': migration,
                            'schema_contract': schema_contract,
                            'reader_capabilities': {'order_inventory_v1': 1, 'quotation_write_v1': 1,
                                                    'shared_finished_v1': 1,
                                                    'shared_finished_management_v1': 1,
                                                    'contract_seal_v1': 1,
                                                    'company_profiles_v1': 1, 'shared_bom_v1': 1},
                            'offline_ocr_models': models}, key)
    if args.package_only:
        write_json(output / 'build-result.json', {'git_sha': code_sha, 'version': args.version,
                   'release_sha256': sha(package), 'installer_built': False})
        remove_transient_build_trees(output)
        return
    common = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
              '--paths', str(tree), '--specpath', str(output), '--workpath', str(output / 'pyi-work'),
              '--distpath', str(output)]
    subprocess.run([*common, '--name', 'TianmingERP-Assistant', '--add-data', str(public) + ';.',
                    str(tree / 'desktop_assistant/gui.py')], check=True, cwd=tree)
    subprocess.run([*common, '--name', 'TianmingERP-Setup',
                    '--add-data', str(output / 'TianmingERP-Assistant.exe') + ';.',
                    '--add-data', str(package) + ';.', str(tree / 'desktop_assistant/installer.py')], check=True, cwd=tree)
    write_json(output / 'build-result.json', {'git_sha': code_sha, 'version': args.version,
               'release_sha256': sha(package), 'installer_sha256': sha(output / 'TianmingERP-Setup.exe')})
    remove_transient_build_trees(output)


if __name__ == '__main__':
    main()

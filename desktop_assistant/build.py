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


def current_mac_capabilities(sources, *, stream_enabled):
    """Declare complete code protocols; no authority or completed install."""
    from desktop_assistant import current_mac_worker as worker
    from desktop_assistant import current_mac_target_audit as audit
    from desktop_assistant import current_mac_smb_broker as broker, mounted_nas_proof as mounted
    from desktop_assistant.mac_factory_runtime import CURRENT_MAC_FILES, CURRENT_MAC_CAPABILITY
    required = CURRENT_MAC_FILES | {
        worker.MODULE, worker.CONTRACT_MODULE, audit.MODULE,
        'desktop_assistant/factory_target_audit.py',
        'desktop_assistant/mac_service_manage.py',
        'desktop_assistant/mac_system_service.py',
        'desktop_assistant/mac_system_upgrade_worker.py',
        'desktop_assistant/mac_smb_stream_runtime.py',
        'desktop_assistant/mac_smb_stream_runtime_contract.py', broker.MODULE, mounted.MODULE, 'desktop_assistant/mac_mount.py'}
    if type(stream_enabled) is not bool or not required <= sources.keys():
        raise ValueError('当前Mac接管协议源码不完整，不能声明能力')
    result = {worker.CAPABILITY: 1, audit.CAPABILITY: 1, CURRENT_MAC_CAPABILITY: 1,
              mounted.CAPABILITY: 1, mounted.UPLOADED_CAPABILITY: 1}
    from desktop_assistant import owner_activation_contract as owner
    if owner.FILES <= sources.keys():
        result[owner.CAPABILITY] = 1
    if stream_enabled:
        from desktop_assistant.mac_smb_stream_runtime_contract import FILES
        if not FILES <= sources.keys():
            raise ValueError('当前Mac固定连接代理的传输源码不完整')
        result[broker.CAPABILITY] = 1
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--runtime-base', type=Path, required=True)
    parser.add_argument('--site-packages', type=Path, required=True)
    parser.add_argument('--ocr-models', type=Path, required=True, help='已下载的 EasyOCR model 目录')
    parser.add_argument('--output', type=Path, required=True)
    signing = parser.add_mutually_exclusive_group(required=True)
    signing.add_argument('--signing-key', type=Path)
    signing.add_argument('--signing-request-only', action='store_true')
    parser.add_argument('--trusted-public-key', type=Path)
    parser.add_argument('--runtime-platform', choices=('windows', 'macos-arm64'), default='windows')
    parser.add_argument('--smb-stream-wheels', type=Path, nargs=2,
                        help='显式启用Mac SMB流能力；提供两个固定版本及摘要的本地wheel')
    parser.add_argument('--revision', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--package-only', action='store_true', help='仅构建签名更新包，不重复生成安装器')
    parser.add_argument('--upgrade-from-revision')
    parser.add_argument('--rollback-package-sha256')
    args = parser.parse_args()
    if args.smb_stream_wheels and args.runtime_platform != 'macos-arm64':
        parser.error('SMB流构建只适用于Mac原生包')
    if args.runtime_platform == 'macos-arm64' and not (args.package_only or args.signing_request_only):
        parser.error('Mac原生包使用--package-only，Windows安装器不能跨平台使用')
    if args.signing_request_only and not args.trusted_public_key:
        parser.error('签名请求必须指定已有可信公钥')
    migration = None
    if args.upgrade_from_revision or args.rollback_package_sha256:
        if (not args.upgrade_from_revision or not args.rollback_package_sha256
                or not re.fullmatch(r'[0-9a-f]{64}', args.rollback_package_sha256)):
            raise ValueError('跨版本包必须同时指定起始revision及已验证可回退程序包SHA256')
        migration = {'policy': 'preserve_existing_facts_v1', 'from_revision': args.upgrade_from_revision,
                     'rollback_package_sha256': args.rollback_package_sha256}
    root, output = args.repo.resolve(), archive_path(args.output)
    code_sha, sources = source_snapshot(root, args.version, args.revision)
    stream_wheels = None
    if args.smb_stream_wheels:
        from desktop_assistant import mac_smb_stream_build as stream_build
        stream_wheels = stream_build.inputs(sources, args.smb_stream_wheels)
    reader_sources = {'app/services/processed_component_stock.py',
                      'app/services/stock_preparation_processing.py',
                      'app/services/stock_preparation_assembly.py',
                      'app/services/quotation_mutations.py', 'app/api/quotations.py',
                      'app/services/shared_finished_stock.py', 'app/models/shared_finished_stock.py',
                      'app/services/shared_finished_management.py', 'app/services/shared_finished_receipts.py',
                      'app/services/delivery_dispatch_commands.py',
                      'app/services/sheet_cutting_contract.py', 'app/services/sheet_cutting_settings.py',
                      'desktop_assistant/delivery_dispatch_contract.py',
                      'desktop_assistant/restore_provenance.py',
                      'desktop_assistant/physical_kit_reader_contract.py',
                      'app/services/emergency_kit_correction_20261014.py',
                      'app/services/entry_source_valuation.py',
                      'app/core/inventory_entry_guard.py',
                      'desktop_assistant/reader_contract.py'}
    if not reader_sources <= sources.keys():
        raise ValueError('库存业务读取契约的实现不完整，禁止签名发布')
    if args.runtime_platform == 'macos-arm64':
        from desktop_assistant.mac_release_publish import SERVICE_RESTORE_FILES
        if not SERVICE_RESTORE_FILES <= sources.keys():
            raise ValueError('专用恢复入口源码不完整，禁止声明原生恢复能力')
        from desktop_assistant.mac_restore_completion import ENTRY as completion_entry, MODULE as completion_module
        if not {completion_entry, completion_module} <= sources.keys():
            raise ValueError('有限恢复完成入口源码不完整')
        from desktop_assistant.mac_system_service import MANAGE_ENTRY, MANAGE_MODULE
        if not {MANAGE_ENTRY, MANAGE_MODULE, 'desktop_assistant/nightly.py',
                'desktop_assistant/service_supervisor.py'} <= sources.keys():
            raise ValueError('专用管理或系统定时备份源码不完整')
        if not {'desktop_assistant/mac_system_upgrade_worker.py',
                'desktop_assistant/mac_system_upgrade_contract.py',
                'desktop_assistant/mac_system_upgrade_recovery.py',
                'desktop_assistant/mac_system_upgrade_recovery_contract.py',
                'desktop_assistant/mac_system_service.py',
                'desktop_assistant/mac_service_manage.py',
                'desktop_assistant/manager.py',
                'desktop_assistant/service_supervisor.py'} <= sources.keys():
            raise ValueError('系统监护升级运行协议源码不完整')
        if not {'desktop_assistant/factory_request_entry.py', 'desktop_assistant/mac_factory_request.py',
                'desktop_assistant/factory_network.py', 'desktop_assistant/factory_target_contract.py',
                'desktop_assistant/mac_factory_request_install.py'} <= sources.keys():
            raise ValueError('目标切换申请源码不完整')
        from desktop_assistant.mac_factory_runtime import EXECUTION_FILES
        if not EXECUTION_FILES <= sources.keys():
            raise ValueError('正式运行准入源码不完整')
        if 'desktop_assistant/factory_target_audit.py' not in sources:
            raise ValueError('最终恢复核验源码不完整')
        if not {'desktop_assistant/factory_activation_entry.py','desktop_assistant/mac_factory_activation.py',
                'desktop_assistant/factory_activation_contract.py','desktop_assistant/mac_factory_activation_install.py',
                'desktop_assistant/home_process.py'} <= sources.keys():
            raise ValueError('受保护激活发布源码不完整')
        current_protocols = current_mac_capabilities(sources, stream_enabled=stream_wheels is not None)
    schema_contract = schema_contract_from_sources(sources, args.revision)
    if output.exists():
        raise ValueError('构建输出必须是新目录')
    if root == output or root in output.parents:
        raise ValueError('构建输出必须在仓库之外')
    output.mkdir(parents=True)
    public = output / 'release-public.pem'
    key = None
    if args.signing_request_only:
        from desktop_assistant.release_request import public_identity
        raw_public = args.trusted_public_key.read_bytes()
        public_identity(raw_public)
        public.write_bytes(raw_public)
    else:
        if not args.signing_key.is_file():
            raise ValueError('发布私钥必须预先生成并安全保存，构建不得自动更换发布身份')
        from desktop_assistant.signing import load_key
        key = load_key(args.signing_key)
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
    runtime_metadata = {}
    if args.runtime_platform == 'macos-arm64':
        from desktop_assistant.native_runtime import assemble, executable_files, verify_relocation
        assemble(args.runtime_base, args.site_packages, runtime, runtime_copy_ignore)
        if stream_wheels is not None:
            stream_build.install(runtime, stream_wheels)
            proof = stream_build.verify(tree)
            write_json(output / 'smb-stream-runtime-verification.json', proof)
            runtime_metadata['smb_stream_dependencies'] = proof
        runtime_metadata['runtime_platform'] = 'macos-arm64'
        write_json(output / 'native-runtime-verification.json', verify_relocation(tree))
    else:
        shutil.copytree(args.runtime_base, runtime, ignore=runtime_copy_ignore)
        shutil.copytree(args.site_packages, runtime / 'Lib/site-packages',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    from desktop_assistant.ocr_models import copy_models
    models = copy_models(args.ocr_models, args.site_packages, runtime / 'ocr/model')
    # Force a relocatable Python search path, independent of machine registry and PYTHONHOME.
    if args.runtime_platform == 'windows':
        (runtime / 'python312._pth').write_text('python312.zip\n.\nLib\nDLLs\nLib/site-packages\n..\nimport site\n', encoding='ascii')
    if args.runtime_platform == 'macos-arm64':
        from desktop_assistant.ocr_models import prepare_readonly_layout
        prepare_readonly_layout(runtime / 'ocr/model')
        runtime_metadata['executable_files'] = executable_files(tree)
        from desktop_assistant.mac_archive_install import ENTRY as archive_entry, MODULE as archive_module
        if any(not (tree/name).is_file() for name in (archive_entry, archive_module)):
            raise ValueError('Mac归档探针源码不完整，不能声明协议能力')
        # These describe execution/service protocols, not completed system
        # installation or permission to activate a factory writer.
        runtime_metadata['runtime_capabilities'] = {'immutable_execution_v1': 1, 'system_service_v1': 1,
                                                    'service_restore_v1': 1, 'service_restore_completion_v1': 1,
                                                    'service_restore_resume_v1': 1, 'service_restore_reauthentication_v1': 1,
                                                    'archive_autofs_v1': 1,
                                                    'service_manage_v1': 1, 'system_nightly_v1': 1,
                                                    'system_upgrade_worker_v1': 1,
                                                    'system_upgrade_preserve_activation_v1': 1,
                                                    'system_upgrade_split_recovery_v1': 1,
                                                    'factory_request_v1': 1, 'factory_audit_v1': 1,
                                                    'factory_activation_v1': 1, 'factory_execution_v1': 1}
        runtime_metadata['runtime_capabilities'].update(current_protocols)
        local_update_files = {'desktop_assistant/local_code_update.py',
                              'desktop_assistant/service_local_code_update_entry.py'}
        if local_update_files <= sources.keys():
            runtime_metadata['runtime_capabilities']['local_code_update_v1'] = 1
        elif local_update_files & sources.keys():
            raise ValueError('本地程序切换协议源码不完整')
        if 'desktop_assistant/local_backup_only.py' in sources:
            local_backup_files = local_update_files | {
                'desktop_assistant/manager.py', 'desktop_assistant/nightly.py',
                'desktop_assistant/mac_service_manage.py'}
            if not local_backup_files <= sources.keys():
                raise ValueError('本地单份备份协议源码不完整')
            runtime_metadata['runtime_capabilities']['local_backup_only_v1'] = 1
        data_recovery_files = {
            'desktop_assistant/data_recovery.py',
            'desktop_assistant/storage.py',
            'desktop_assistant/manager.py',
            'desktop_assistant/restore_cli.py',
        }
        if data_recovery_files <= sources.keys():
            runtime_metadata['runtime_capabilities']['data_recovery_v2'] = 1
        elif data_recovery_files & sources.keys():
            raise ValueError('数据与程序拆分恢复协议源码不完整')
        if stream_wheels is not None:
            rotation_files = {'desktop_assistant/nas_backup_rotation_worker.py',
                              'desktop_assistant/nas_backup_rotation.py',
                              'desktop_assistant/nas_backup_rotation_stream.py',
                              'desktop_assistant/nas_full_backup_retention.py'}
            if not rotation_files <= sources.keys():
                raise ValueError('NAS备份轮换实现不完整，不能声明轮换能力')
            runtime_metadata['runtime_capabilities'][stream_build.cfg.CAPABILITY] = 1
            runtime_metadata['runtime_capabilities']['archive_smb_upgrade_v1'] = 1
            runtime_metadata['runtime_capabilities']['nas_backup_rotation_v1'] = 1
    package = output / ('release.unsigned.zip' if args.signing_request_only else 'release.zip')
    pack_tree(tree, package, {'type': 'tianming.release.v1', 'version': args.version,
                            'revision': args.revision, 'git_sha': code_sha, 'migration': migration,
                            'schema_contract': schema_contract,
                            'reader_capabilities': {'order_inventory_v1': 1, 'quotation_write_v1': 1,
                                                    'shared_finished_v1': 1,
                                                    'shared_finished_management_v1': 1,
                                                    'contract_seal_v1': 1,
                                                    'company_profiles_v1': 1, 'shared_bom_v1': 1,
                                                    'stock_purchase_identity_v1': 1,
                                                    'delivery_dispatch_v1': 1,
                                                    'unused_mold_deletion_v1': 1,
                                                    'supplier_sheet_trim_v1': 1,
                                                    'restore_provenance_v1': 1,
                                                    'physical_kit_correction_20261014_v1': 1,
                                                    **({'factory_cutover_v1': 1} if args.runtime_platform == 'macos-arm64' else {})},
                            'offline_ocr_models': models, **runtime_metadata}, key)
    if args.signing_request_only:
        from desktop_assistant.release_request import prepare_request
        record = prepare_request(package, public.read_bytes(), output / 'signing-request.json')
        write_json(output / 'build-result.json', {**record, 'installer_built': False})
        remove_transient_build_trees(output)
        return
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

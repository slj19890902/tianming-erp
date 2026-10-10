"""Single-host maintenance engine. Data rollback is never an update fallback."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import uuid

import psutil

from desktop_assistant.storage import (archive_path, database_info, decrypt_file, encrypt_file,
                                      extract_verified, pack_tree, pack_recovery, read_json, sha, write_json, signed_release_manifest)
from desktop_assistant.attachments import rebind_pdf_sources
from desktop_assistant.operation_lock import operation_lock
from desktop_assistant import delivery_dispatch_contract as dispatch_contract
from desktop_assistant.schema_contract import (
    schema_contract_from_signed_release,
    validate_database_schema,
)

CN = timezone(timedelta(hours=8))
ORDER_INVENTORY_READER = 'order_inventory_v1'


def _has_supplier_trim_facts(db):
    for table, column in (
        ('products', 'sheet_cutting_settings'),
        ('sales_order_items', 'sheet_cutting_settings_snapshot'),
        ('sales_order_item_bom_components', 'sheet_cutting_settings_snapshot'),
        ('material_requisition_items', 'sheet_cutting_snapshot'),
        ('supplier_requisition_order_items', 'sheet_cutting_snapshot'),
        ('stock_replenishment_order_items', 'sheet_cutting_snapshot')):
        if any(row[1] == column for row in db.execute(f'PRAGMA table_info("{table}")')):
            if db.execute(f'SELECT 1 FROM "{table}" WHERE json_valid("{column}") AND json_extract("{column}", \'$.schema_version\') = 3 LIMIT 1').fetchone():
                return True
    return False


class Manager:
    def __init__(self, root: Path, public_key: bytes):
        self.root = root.resolve()
        self.public_key = public_key
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('releases', 'shared', 'control', 'backups', 'packages', 'staging'):
            (self.root / name).mkdir(exist_ok=True)

    @property
    def state(self):
        path = self.root / 'state.json'
        return read_json(path) if path.exists() else {'current': None, 'previous': None}

    @contextmanager
    def lock(self):
        with operation_lock(self.root / 'control' / 'operation.lock'):
            yield

    def manifest(self, release=None):
        return read_json(self.root / 'releases' / (release or self.state['current']) / 'manifest.json')

    def compatible(self, release, revision, authority=None):
        try:
            self._check_dispatch_reader(release)
        except (ValueError, OSError):
            return False
        # Preserve rollback metadata, but never run a writer which ignores the
        # active quotation version/idempotency contract. This check is read-only.
        import sqlite3
        database = self.root / 'shared/data/carton_erp.sqlite3'
        if database.is_file():
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                quotation_contract = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quotation_mutations'"
                ).fetchone()
                shared_stock_contract = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_finished_groups'"
                ).fetchone()
                shared_management_contract = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_finished_policies'"
                ).fetchone()
                contract_seal_contract = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contract_seals'"
                ).fetchone()
                company_profile_contract = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_profiles'"
                ).fetchone()
                shared_bom_contract = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_bom_members'"
                ).fetchone()
                purchase_identity_contract = any(row[1] == 'production_snapshot_json' for row in
                    db.execute('PRAGMA table_info(stock_replenishment_order_items)'))
                mold_deletion_contract = any(row[1] == 'deleted_at' for row in
                    db.execute('PRAGMA table_info(mold_tools)'))
                supplier_trim_contract = _has_supplier_trim_facts(db)
            if supplier_trim_contract and not self._shared_finished_reader(release, "supplier_sheet_trim_v1"):
                return False
            if mold_deletion_contract and not self._shared_finished_reader(release, "unused_mold_deletion_v1"):
                return False
            if quotation_contract and not self._quotation_writer(release):
                return False
            if shared_stock_contract and not self._shared_finished_reader(release):
                return False
            if shared_management_contract and not self._shared_finished_reader(release, "shared_finished_management_v1"):
                return False
            if contract_seal_contract and not self._shared_finished_reader(release, "contract_seal_v1"):
                return False
            if company_profile_contract and not self._shared_finished_reader(release, "company_profiles_v1"):
                return False
            if shared_bom_contract and not self._shared_finished_reader(release, "shared_bom_v1"):
                return False
            if purchase_identity_contract and not self._shared_finished_reader(release, "stock_purchase_identity_v1"):
                return False
        # Additive JSON business facts can change semantics without an Alembic
        # revision change. Check the signed reader contract before that shortcut.
        activation = self.state.get('order_inventory_activation')
        if activation:
            if (not isinstance(activation, dict)
                    or activation.get('reader_capability') != ORDER_INVENTORY_READER
                    or type(activation.get('version')) is not int
                    or activation.get('version') != 1
                    or not self._order_inventory_reader(release)):
                return False
        if self.manifest(release)['revision'] == revision:
            return True
        if revision == 'eg1008sc' and self.manifest(release)['revision'] == 'ef1007cp':
            # Old code can read additive NULL columns, but cannot create new
            # business using the independent mold/supplier-cutting semantics.
            import sqlite3
            database = self.root / 'shared/data/carton_erp.sqlite3'
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                for table, column in (
                    ('products', 'sheet_cutting_settings'),
                    ('sales_order_items', 'sheet_cutting_settings_snapshot'),
                    ('sales_order_item_bom_components', 'sheet_cutting_settings_snapshot'),
                    ('material_requisition_items', 'sheet_cutting_snapshot'),
                    ('supplier_requisition_order_items', 'sheet_cutting_snapshot'),
                    ('stock_replenishment_order_items', 'sheet_cutting_snapshot')):
                    if db.execute(f'SELECT 1 FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT 1').fetchone():
                        return False
        authority = authority or self.state.get('schema_authority')
        if not authority:
            return False
        package = self.root / 'packages' / (authority + '.zip')
        if not package.is_file() or sha(package) != authority:
            return False
        manifest = signed_release_manifest(package, self.public_key)
        contract = (manifest.get('migration') or {})
        return (manifest['revision'] == revision
                and contract.get('policy') == 'preserve_existing_facts_v1'
                and contract.get('rollback_package_sha256') == release)

    def _order_inventory_reader(self, release):
        package = self.root / 'packages' / (release + '.zip')
        try:
            if not package.is_file() or sha(package) != release:
                return False
            manifest = signed_release_manifest(package, self.public_key)
            capabilities = manifest.get('reader_capabilities') or {}
            return (capabilities.get(ORDER_INVENTORY_READER) == 1
                    and type(capabilities.get(ORDER_INVENTORY_READER)) is int
                    and manifest == self.manifest(release))
        except Exception:
            return False

    def _quotation_writer(self, release):
        package = self.root / 'packages' / (release + '.zip')
        try:
            if not package.is_file() or sha(package) != release:
                return False
            manifest = signed_release_manifest(package, self.public_key)
            capability = (manifest.get('reader_capabilities') or {}).get('quotation_write_v1')
            return type(capability) is int and capability == 1 and manifest == self.manifest(release)
        except Exception:
            return False

    def _shared_finished_reader(self, release, name="shared_finished_v1"):
        package = self.root / 'packages' / (release + '.zip')
        try:
            if not package.is_file() or sha(package) != release:
                return False
            manifest = signed_release_manifest(package, self.public_key)
            capability = (manifest.get('reader_capabilities') or {}).get(name)
            return type(capability) is int and capability == 1 and manifest == self.manifest(release)
        except Exception:
            return False

    def _dispatch_reader(self, release):
        return self._shared_finished_reader(release, dispatch_contract.CAPABILITY)

    def _check_dispatch_reader(self, release, *, require_active=False):
        value = dispatch_contract.inspect_activation(
            self.root / 'shared', self.state, require_active=require_active)
        if value is not None and not self._dispatch_reader(release):
            raise ValueError('此程序不支持已启用的发货保护规则，未切换程序；请使用兼容的新版本')
        return value

    def _activate_dispatch_reader(self, candidate, old, new, backup):
        if not self._dispatch_reader(candidate):
            return
        value = dispatch_contract.inspect_activation(self.root / 'shared', self.state)
        if value is None:
            marker = self.root / 'shared' / dispatch_contract.MARKER
            contract = {
                'reader_capability': dispatch_contract.CAPABILITY, 'version': 1,
                'activated_package': candidate, 'activated_at': datetime.now(CN).isoformat(),
                'pre_activation_package': old['current'], 'pre_activation_backup': str(backup),
            }
            dispatch_contract.validate_contract(contract)
            write_json(marker, contract)
            value = dispatch_contract.activation(self.root / 'shared')
        new[dispatch_contract.STATE_KEY] = dispatch_contract.state_index(value)
        if not self._dispatch_reader(old['current']):
            new['previous'] = None

    def stage_release(self, package: Path, *, verify_existing: bool = False) -> dict:
        # Hash names avoid arbitrary version strings becoming paths.
        identity = sha(package)
        destination = self.root / 'releases' / identity
        if not destination.exists() or verify_existing:
            stage = self.root / 'staging' / uuid.uuid4().hex
            manifest = extract_verified(package, stage, self.public_key)
            if manifest.get('type') != 'tianming.release.v1':
                raise ValueError('发布包类型不匹配')
            required = ('runtime/python.exe', 'main.py', 'app/main.py', 'desktop_assistant/server_entry.py')
            if not all((stage / p).is_file() for p in required):
                raise ValueError('发布包缺少程序或运行环境')
            if any((stage / p).exists() for p in ('data', '.env', 'static/uploads', 'factory_twin/data')):
                raise ValueError('程序发布包不得携带业务数据或配置')
            if destination.exists():
                # Restore must authenticate the code that will actually run,
                # not merely the archive or its mutable manifest cache.
                self._verify_cached_release(destination, manifest)
                if stage.resolve().parent != (self.root / 'staging').resolve():
                    raise ValueError('恢复校验暂存路径异常')
                shutil.rmtree(stage)
            else:
                stage.rename(destination)
        # Preserve the signed source for recovery even after app data junctions exist.
        cache = self.root / 'packages' / (identity + '.zip')
        if not cache.exists():
            shutil.copyfile(package, cache)
        if sha(cache) != identity:
            raise ValueError('缓存发布包校验失败')
        return {'id': identity, **read_json(destination / 'manifest.json')}

    def _verify_cached_release(self, destination: Path, manifest: dict) -> None:
        if destination.is_symlink() or destination.is_junction():
            raise ValueError('恢复程序缓存不得使用链接')
        entries = list(destination.rglob('*'))
        if any(path.is_symlink() or path.is_junction() for path in entries):
            raise ValueError('恢复程序缓存包含链接，不能确认安全')
        actual = {path.relative_to(destination).as_posix() for path in entries if path.is_file()}
        if actual != set(manifest['files']) | {'manifest.json'}:
            raise ValueError('恢复程序缓存文件清单不一致')
        if read_json(destination / 'manifest.json') != manifest:
            raise ValueError('恢复程序缓存版本信息不一致')
        for name, expected in manifest['files'].items():
            if sha(destination / name) != expected:
                raise ValueError('恢复程序缓存文件校验失败：' + name)

    def _process(self):
        path = self.root / 'control' / 'process.json'
        if not path.exists():
            return None
        record = read_json(path)
        try:
            proc = psutil.Process(record['pid'])
            if abs(proc.create_time() - record['created']) > .01:
                return None
            if Path(proc.exe()).resolve() != Path(record['exe']).resolve():
                raise ValueError('服务进程身份不匹配，停止维护')
            return proc, record
        except psutil.NoSuchProcess:
            return None

    def stop(self):
        item = self._process()
        if item:
            proc, record = item
            (self.root / 'control' / (record['nonce'] + '.stop')).touch()
            try:
                proc.wait(timeout=60)
            except psutil.TimeoutExpired:
                raise ValueError('服务未能正常停止，未强杀，请检查日志') from None

    def pause_after_backup(self, password: str, nas: Path):
        with self.lock():
            from desktop_assistant.nas_probe import check_before_stop
            check_before_stop(nas)
            running = bool(self._process())
            self.stop()
            try:
                backup = self._backup_stopped(password, nas)
                state = self.state
                state['manual_stop'] = True
                write_json(self.root / 'state.json', state)
                return f'完整备份已保存：{backup}。ERP已停止；下次点“启动并打开ERP”恢复使用。'
            except Exception:
                if running:
                    self.start()
                raise

    def resume(self):
        with self.lock():
            self.start()
            state = self.state
            state['manual_stop'] = False
            write_json(self.root / 'state.json', state)

    def _environment(self, release: Path):
        from desktop_assistant.ai_config import load_openai_api_key, load_deepseek_api_key

        shared = self.root / 'shared'
        saved = read_json(shared / 'environment.json')
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(('ERP_', 'TM_ERP_', 'PYTHON'))
            and key not in {'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'}
        }
        for key, value in saved.items():
            env[key] = value.replace('${SHARED}', str(shared))
        env['PYTHONUTF8'] = '1'
        env['PYTHONPATH'] = str(release)
        env['ERP_DATABASE_PATH'] = str(shared / 'data/carton_erp.sqlite3')
        env['ERP_WORKERS'] = '1'
        ai_key = load_openai_api_key(self.root)
        if ai_key:
            env['OPENAI_API_KEY'] = ai_key
            env['ERP_AI_INVENTORY_PROVIDER'] = 'openai'
            env.setdefault('ERP_AI_INVENTORY_MODEL', 'gpt-5-mini')
        deepseek_key = load_deepseek_api_key(self.root)
        if deepseek_key:
            env.pop('OPENAI_API_KEY', None)
            env['DEEPSEEK_API_KEY'] = deepseek_key
            env['ERP_AI_INVENTORY_PROVIDER'] = 'deepseek'
            env['ERP_AI_INVENTORY_MODEL'] = 'deepseek-flash'
        models = release / 'runtime/ocr/model'
        if models.is_dir():
            env['EASYOCR_MODULE_PATH'] = str(models.parent)
        return env

    def _link_data(self, release: Path):
        for relative, folder in (('data', 'data'), ('static/uploads', 'legacy_uploads'),
                                 ('factory_twin/data', 'factory_twin_data'), ('logs', 'logs')):
            link, target = release / relative, self.root / 'shared' / folder
            target.mkdir(parents=True, exist_ok=True)
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists():
                if link.resolve() != target.resolve():
                    raise ValueError('程序目录存在非托管数据，拒绝覆盖')
                continue
            # Native junction creation; never pass data paths through cmd strings.
            quoted_link = str(link).replace("'", "''")
            quoted_target = str(target).replace("'", "''")
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                            f"New-Item -ItemType Junction -Path '{quoted_link}' -Target '{quoted_target}' | Out-Null"],
                           check=True, creationflags=subprocess.CREATE_NO_WINDOW)

    def start(self):
        state = self.state
        if not state.get('current'):
            raise ValueError('尚未接入或恢复ERP数据')
        self._check_dispatch_reader(state['current'], require_active=self._dispatch_reader(state['current']))
        running = self._process()
        if running:
            expected = self.root / 'releases' / state['current'] / 'runtime/python.exe'
            if Path(running[1]['exe']).resolve() != expected.resolve():
                raise ValueError('运行程序与已核对版本不一致，未接管或停止其他进程')
            return
        if state.get('onboarding_pending'):
            raise ValueError('首次接入尚未完成完整备份，请点击“完成首次接入”后再启动')
        if state.get('operation') in ('migration_running', 'migration_failed'):
            raise ValueError('上次数据库升级未完成，先核对现场或从已验证备份恢复到空安装，不能直接启动')
        release = self.root / 'releases' / state['current']
        info = database_info(self.root / 'shared/data/carton_erp.sqlite3')
        if not self.compatible(state['current'], info['revision']):
            raise ValueError('程序与数据库版本不同，须先完成专项迁移演练')
        self._link_data(release)
        env = self._environment(release)
        port = int(env['ERP_PORT'])
        with socket.socket() as probe:
            if probe.connect_ex((env.get('ERP_BIND_HOST', '127.0.0.1'), port)) == 0:
                raise ValueError('端口已有服务，拒绝接管或停止其他ERP')
        nonce = uuid.uuid4().hex
        env.update(TM_ERP_CONTROL=str(self.root / 'control'), TM_ERP_NONCE=nonce)
        python = release / 'runtime/python.exe'
        with (self.root / 'control/server.log').open('ab') as log:
            proc = subprocess.Popen([str(python), '-m', 'desktop_assistant.server_entry'],
                                    cwd=release, env=env, stdout=log, stderr=log,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        owner = psutil.Process(proc.pid)
        write_json(self.root / 'control/process.json', {'pid': proc.pid, 'created': owner.create_time(),
                   'exe': str(python), 'nonce': nonce})
        for _ in range(120):
            if proc.poll() is not None:
                raise ValueError('ERP启动失败，请查看control/server.log')
            if (self.root / 'control' / (nonce + '.ready')).exists():
                return
            time.sleep(.5)
        self.stop()
        raise ValueError('ERP启动超时，已停止本次进程')

    def _backup_stopped(self, password: str, nas: Path) -> Path:
        # NAS must already exist; a disconnected mapping must not become a local folder.
        if not nas.is_dir():
            raise ValueError('NAS目录不可达，保留原备份并停止更新')
        state = self.state
        current = state['current']
        if not current:
            raise ValueError('尚未导入或恢复ERP数据')
        dispatch_activation = self._check_dispatch_reader(current, require_active=self._dispatch_reader(current))
        before = database_info(self.root / 'shared/data/carton_erp.sqlite3')
        from desktop_assistant.preflight import inspect
        checks = inspect(self.root / 'shared/data/carton_erp.sqlite3', self.root / 'shared', managed=True)
        if any(checks['counts'][key] for key in ('missing', 'external', 'hash_mismatch')):
            raise ValueError('附件缺失或校验失败，不能标记为完整备份')
        # Backup payload, encryption and verification all live on the selected NAS.
        # Never fall back to C/D when a mapped NAS is unavailable.
        nas = archive_path(nas)
        if nas == self.root or self.root in nas.parents:
            raise ValueError('NAS备份目录不能位于ERP安装目录内')
        package_ids = {current, state.get('schema_authority')} - {None}
        required = 4 * (sum(p.stat().st_size for p in (self.root / 'shared').rglob('*') if p.is_file())
                        + sum((self.root / 'packages' / (p + '.zip')).stat().st_size for p in package_ids)) + 2 * 1024**3
        if shutil.disk_usage(nas).free < required:
            raise ValueError('NAS空间不足，未创建备份；请保留旧备份')
        # ZSpace virtual volumes may discard dot-prefixed directories on sync.
        work_root = nas / 'tianming-backup-work'
        work_root.mkdir(exist_ok=True)
        if work_root.is_symlink() or work_root.is_junction():
            raise ValueError('NAS备份临时目录不能是链接')
        job = work_root / uuid.uuid4().hex
        job.mkdir()
        # Stream local stopped data directly into the NAS archive. Virtual NAS
        # drives need not support loose SQLite files or hidden build/test files.
        package = self.root / 'packages' / (current + '.zip')
        if sha(package) != current:
            raise ValueError('对应程序发布包已损坏')
        packages = {'release.zip': package}
        authority = state.get('schema_authority')
        if authority and self.manifest()['revision'] != before['revision']:
            if not self.compatible(current, before['revision'], authority):
                raise ValueError('旧程序缺少当前数据库的签名兼容授权')
            packages['compatibility.zip'] = self.root / 'packages' / (authority + '.zip')
        else:
            authority = None
        stamp = datetime.now(CN).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        raw = job / 'recovery.zip'
        pack_recovery(self.root / 'shared', packages, raw, {'type': 'tianming.recovery.v1', 'created': datetime.now(CN).isoformat(),
                            'release': current, 'version': self.manifest()['version'], 'database': before,
                            'source_shared': str(self.root / 'shared'), 'schema_authority': authority,
                            **({dispatch_contract.STATE_KEY: dispatch_activation} if dispatch_activation else {})})
        if database_info(self.root / 'shared/data/carton_erp.sqlite3') != before:
            raise ValueError('备份期间数据复核不一致')
        encrypted = job / (stamp + '.tmbackup')
        encrypt_file(raw, encrypted, password)
        # Authenticate before publication and hash the independently copied NAS file.
        check = job / 'check.zip'
        decrypt_file(encrypted, check, password)
        if sha(check) != sha(raw):
            raise ValueError('备份解密校验失败')
        pending = nas / (encrypted.name + '.pending')
        final = nas / encrypted.name
        with encrypted.open('rb') as src, pending.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if sha(pending) != sha(encrypted):
            raise ValueError('NAS副本校验失败，未标记成功')
        pending.rename(final)
        (self.root / 'control' / 'backup-receipts').mkdir(exist_ok=True)
        write_json(self.root / 'control' / 'backup-receipts' / (stamp + '.json'),
                   {'path': str(final), 'sha256': sha(final), 'size': final.stat().st_size,
                    'verified': True, 'storage': 'nas', 'database': before})
        state.update(last_backup=str(final), last_backup_at=datetime.now(CN).isoformat(), backup_error=None)
        write_json(self.root / 'state.json', state)
        # Staging only, never remove managed shared data or a user-selected directory.
        if archive_path(job).parent != archive_path(work_root):
            raise ValueError('临时目录清理边界不匹配')
        shutil.rmtree(job)
        return final

    def backup(self, password: str, nas: Path):
        with self.lock():
            from desktop_assistant.nas_probe import check_before_stop
            check_before_stop(nas)
            running = bool(self._process())
            self.stop()
            try:
                return self._backup_stopped(password, nas)
            except Exception as error:
                state = self.state
                state['backup_error'] = str(error)
                write_json(self.root / 'state.json', state)
                raise
            finally:
                if running:
                    self.start()

    def preview_update(self, package: Path):
        from desktop_assistant.migration import rehearse
        with self.lock():
            return rehearse(self, package)

    def _plan_update_chain(self, package: Path, releases: Path):
        """Cache a fully verified chain before stopping any running service."""
        import re
        current = self.state['current']
        revision = database_info(self.root / 'shared/data/carton_erp.sqlite3')['revision']
        chain, seen = [], set()
        expected = None
        for _ in range(64):
            if not package.is_file():
                raise ValueError('NAS缺少必要的中间版本包，未停止服务；请补齐已签名发布包')
            identity = sha(package)
            if expected is not None and identity != expected:
                raise ValueError('中间版本包哈希不匹配，未停止服务')
            if identity in seen:
                raise ValueError('版本升级链存在循环，未停止服务')
            seen.add(identity)
            signed = signed_release_manifest(package, self.public_key)
            candidate = self.stage_release(package)
            if any(candidate.get(key) != value for key,value in signed.items()):
                raise ValueError('已暂存程序与签名版本信息不一致，未停止服务')
            if chain and chain[-1]['from_revision'] != candidate['revision']:
                raise ValueError('中间版本与签名迁移起点不一致，未停止服务')
            if identity == current:
                return list(reversed(chain))
            contract = candidate.get('migration') or {}
            chain.append({'id':identity, 'version':candidate['version'],
                          'from_revision':contract.get('from_revision')})
            if self.compatible(identity, revision):
                return list(reversed(chain))
            previous = contract.get('rollback_package_sha256')
            if (contract.get('policy') != 'preserve_existing_facts_v1'
                    or not isinstance(previous,str) or not re.fullmatch(r'[0-9a-f]{64}',previous)):
                raise ValueError('升级缺少已签名的上一版本兼容契约，未停止服务')
            if previous == current:
                if contract.get('from_revision') != revision:
                    raise ValueError('当前数据库与迁移起点不一致，未停止服务')
                return list(reversed(chain))
            expected = previous
            cached = self.root / 'packages' / (previous + '.zip')
            package = cached if cached.is_file() else releases / (previous + '.zip')
        raise ValueError('版本升级链过长，未停止服务；请联系管理员核对')

    def update_from_feed(self, package: Path, password: str, nas: Path):
        with self.lock():
            if self.state.get('operation') in ('migration_running','migration_failed'):
                raise ValueError('上次迁移尚未完成，请先处理恢复')
            chain = self._plan_update_chain(package, nas / 'releases')
            if not chain:
                from desktop_assistant.retention import after_update
                after_update(self, package.parent)
                return '已经是该版本'
            for candidate in chain:
                self._update_locked(self.root / 'packages' / (candidate['id'] + '.zip'), password, nas)
            from desktop_assistant.retention import after_update
            after_update(self, package.parent)
            return chain[-1]['version']

    def update(self, package: Path, password: str, nas: Path, *, rollback=False):
        with self.lock():
            result = self._update_locked(package, password, nas, rollback=rollback)
            from desktop_assistant.retention import after_update
            archive_dir = nas.parent / 'releases' if nas.name == 'backups' else nas / 'releases'
            for settings_path in (self.root / 'control/setup-defaults.json', self.root / 'preferences.json'):
                try:
                    configured = read_json(settings_path).get('release_feed') if settings_path.is_file() else None
                    if isinstance(configured, str) and configured:
                        archive_dir = Path(configured)
                except (OSError, ValueError, AttributeError):
                    pass  # Cleanup configuration cannot fail an activated update.
            after_update(self, archive_dir)
            return result

    def _update_locked(self, package: Path, password: str, nas: Path, *, rollback=False):
        candidate = self.stage_release(package)
        old = self.state
        if old.get('onboarding_pending'):
            raise ValueError('请先完成首次接入的完整备份，不能叠加更新')
        if old.get('operation') in ('migration_running', 'migration_failed'):
            raise ValueError('上次迁移尚未完成，禁止叠加更新；请先处理恢复')
        # A semantic writer incompatibility is never a request to migrate.
        self._check_dispatch_reader(candidate['id'], require_active=(
            candidate['id'] == old['current'] and self._dispatch_reader(candidate['id'])))
        if candidate['id'] == old['current']:
            return '已经是该版本'
        revision = database_info(self.root / 'shared/data/carton_erp.sqlite3')['revision']
        migrate = not self.compatible(candidate['id'], revision)
        if migrate:
            contract = (candidate.get('migration') or {})
            if (rollback or contract.get('policy') != 'preserve_existing_facts_v1'
                    or contract.get('from_revision') != revision
                    or contract.get('rollback_package_sha256') != old['current']):
                raise ValueError('此版本需要专项迁移或缺少上版程序兼容契约，未停止服务')
        from desktop_assistant.nas_probe import check_before_stop
        check_before_stop(nas)
        self.stop()
        try:
            backup = self._backup_stopped(password, nas)
        except Exception:
            self.start()
            raise
        new = self.state
        if migrate:
            from desktop_assistant.migration import rehearse, run_migration, facts, files, schema
            # The checked NAS backup exists before both rehearsal and real migration.
            try:
                report = rehearse(self, package)
            except Exception:
                self.start()
                raise
            shared = self.root / 'shared'
            dbpath = shared / 'data/carton_erp.sqlite3'
            before_files = files(shared)
            release = self.root / 'releases' / candidate['id']
            new.update(operation='migration_running', update_backup=str(backup),
                       migration_report=report['report_path'], migration_target=candidate['id'],
                       migration_files=before_files, migration_report_sha256=sha(Path(report['report_path'])),
                       migration_backup_sha256=sha(backup))
            write_json(self.root / 'state.json', new)
            try:
                run_migration(release, shared, candidate['revision'],
                    self.root / 'control' / ('migration-' + uuid.uuid4().hex + '.log'),
                    environment=self._environment(release))
                actual = database_info(dbpath)
                if (actual['revision'] != candidate['revision']
                        or schema(dbpath) != report['result_schema']
                        or facts(dbpath, report['source_facts']['columns']) != report['source_facts']
                        or files(shared) != before_files
                        or actual['counts'] != report['result']['counts']):
                    raise ValueError('实际迁移与演练不一致')
                new['schema_authority'] = candidate['id']
            except Exception:
                # Never replace the database with an old backup as an update fallback.
                new.update(operation='migration_failed', update_backup=str(backup),
                           migration_report=report['report_path'])
                write_json(self.root / 'state.json', new)
                raise ValueError('数据库升级未完整通过，服务保持停止；已保留现场及NAS备份，请专项恢复，未回写旧数据') from None
        new.update(current=candidate['id'], previous=old['current'], update_backup=str(backup),
                   operation='rollback' if rollback else 'update', manual_stop=False, updated_at=datetime.now(CN).isoformat())
        if self._order_inventory_reader(candidate['id']):
            new['order_inventory_activation'] = old.get('order_inventory_activation') or {
                'reader_capability': ORDER_INVENTORY_READER, 'version': 1,
                'activated_package': candidate['id'], 'activated_at': datetime.now(CN).isoformat(),
                'pre_activation_package': old['current'], 'pre_activation_backup': str(backup),
                'next_release_must_preserve_reader_contract': True,
            }
            if not self._order_inventory_reader(old['current']):
                new['previous'] = None
        self._activate_dispatch_reader(candidate['id'], old, new, backup)
        # Commit the reader gate and removal of unsafe one-click rollback before
        # starting the new process; failure here never opens new write requests.
        write_json(self.root / 'state.json', new)
        try:
            self.start()
        except Exception:
            self.stop()
            if not self.compatible(old['current'], database_info(self.root / 'shared/data/carton_erp.sqlite3')['revision']):
                new.update(operation='update_failed_reader_contract', previous=None)
                write_json(self.root / 'state.json', new)
                raise ValueError('新程序未启动且旧程序不支持新业务保护规则，服务保持停止；保留现场和备份，请向前修复，数据未回退') from None
            new.update(current=old['current'], previous=old.get('previous'), operation='update_failed')
            write_json(self.root / 'state.json', new)
            self.start()
            raise ValueError('新版本启动失败，已切回原程序，业务数据库未回退') from None
        return candidate['version']

    def rollback(self, password: str, nas: Path):
        previous = self.state.get('previous')
        if not previous:
            raise ValueError('没有可回退的上一个版本')
        return self.update(self.root / 'packages' / (previous + '.zip'), password, nas, rollback=True)

    def recover_interrupted_update(self):
        """Finish activation only when the complete migration result is proven intact."""
        from desktop_assistant.migration import facts, files, schema, recovery_schema
        with self.lock():
            state = self.state
            if state.get('operation') not in ('migration_running', 'migration_failed'):
                raise ValueError('没有待恢复的中断升级')
            if self._process():
                raise ValueError('ERP仍在运行，不能处理中断升级')
            target = state.get('migration_target', '')
            package = self.root / 'packages' / (target + '.zip')
            if not package.is_file() or sha(package) != target:
                raise ValueError('原升级包缺失或校验失败，保留现场')
            manifest = signed_release_manifest(package, self.public_key)
            contract = manifest.get('migration') or {}
            if (contract.get('policy') != 'preserve_existing_facts_v1'
                    or contract.get('rollback_package_sha256') != state['current']):
                raise ValueError('升级兼容证明不匹配，保留现场')
            python = (self.root / 'releases' / target / 'runtime/python.exe').resolve()
            for proc in psutil.process_iter(['exe']):
                try:
                    executable = proc.info['exe']
                    if executable and Path(executable).name.casefold() == python.name.casefold() and Path(executable).resolve() == python:
                        raise ValueError('升级运行环境仍有进程，请等待结束后重试')
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            report_path = Path(state.get('migration_report', ''))
            backup = Path(state.get('update_backup', ''))
            if (not report_path.is_file() or sha(report_path) != state.get('migration_report_sha256')
                    or not backup.is_file() or sha(backup) != state.get('migration_backup_sha256')):
                raise ValueError('缺少完整演练或备份校验证据，保留现场，请专项恢复')
            report = read_json(report_path)
            expected_schema = recovery_schema(report, report_path)
            shared = self.root / 'shared'
            database = shared / 'data/carton_erp.sqlite3'
            actual = database_info(database)
            if (report.get('status') != 'passed' or report.get('target_revision') != manifest['revision']
                    or report.get('package_sha256') != target
                    or actual != report['result']
                    or schema(database) != expected_schema
                    or facts(database, report['source_facts']['columns']) != report['source_facts']
                    or files(shared) != state.get('migration_files')):
                raise ValueError('现场未达到完整升级结果，保持停服；未覆盖数据，请专项恢复')
            previous = state['current']
            self._check_dispatch_reader(target)
            old_state = dict(state)
            state.update(current=target, previous=previous, schema_authority=target,
                         operation='migration_recovered', updated_at=datetime.now(CN).isoformat())
            self._activate_dispatch_reader(target, old_state, state, backup)
            write_json(self.root / 'state.json', state)
            try:
                self.start()
            except Exception:
                self.stop()
                if not self.compatible(previous, actual['revision']):
                    state.update(operation='update_failed_reader_contract', previous=None)
                    write_json(self.root / 'state.json', state)
                    raise ValueError('新程序未启动且旧程序不支持新业务保护规则，服务保持停止；请向前修复，数据未回退') from None
                state.update(current=previous, previous=target, operation='recovery_start_failed')
                write_json(self.root / 'state.json', state)
                self.start()
                raise ValueError('升级数据完整，但新程序启动失败；已恢复兼容旧程序，数据未回退') from None
            return '已核对完整升级结果并恢复运行，业务数据未回退'

    def restore(self, backup: Path, password: str):
        """Restore into an EMPTY managed installation. Never replaces running data."""
        with self.lock():
            if self.state['current'] or any((self.root / 'shared').iterdir()):
                raise ValueError('恢复仅允许全新安装目录，现有数据不被覆盖')
            job = self.root / 'staging' / uuid.uuid4().hex
            job.mkdir()
            raw = job / 'recovery.zip'
            decrypt_file(backup, raw, password)
            manifest = extract_verified(raw, job / 'payload')
            if manifest.get('type') != 'tianming.recovery.v1':
                raise ValueError('不是完整ERP恢复包')
            payload = job / 'payload'
            if sha(payload / 'release.zip') != manifest['release']:
                raise ValueError('恢复程序身份不一致')
            info = database_info(payload / 'shared/data/carton_erp.sqlite3')
            if info != manifest['database']:
                raise ValueError('恢复库表计数或版本不一致')
            release_manifest, release_contract = schema_contract_from_signed_release(
                payload / 'release.zip', self.public_key
            )
            release = {'id': manifest['release'], **release_manifest}
            authority = manifest.get('schema_authority')
            authority_manifest = None
            authority_contract = None
            if authority:
                compatible_package = payload / 'compatibility.zip'
                if sha(compatible_package) != authority:
                    raise ValueError('恢复包兼容授权身份不一致')
                authority_manifest, authority_contract = schema_contract_from_signed_release(
                    compatible_package, self.public_key
                )
            directly_compatible = release_manifest['revision'] == info['revision']
            if not directly_compatible and authority_manifest:
                migration = authority_manifest.get('migration') or {}
                directly_compatible = (
                    authority_manifest.get('revision') == info['revision']
                    and migration.get('policy') == 'preserve_existing_facts_v1'
                    and migration.get('rollback_package_sha256') == release['id']
                )
            # Same compatibility rule as compatible(), applied only to the
            # authenticated archive facts; no mutable extracted manifest.
            if not directly_compatible:
                raise ValueError('备份程序与数据不兼容')
            contract = release_contract if release['revision'] == info['revision'] else authority_contract
            if not contract or contract['revision'] != info['revision']:
                raise ValueError('缺少与恢复数据库版本匹配的签名必要结构契约')
            database = payload / 'shared/data/carton_erp.sqlite3'
            import sqlite3
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                trim_reader = (release_manifest.get('reader_capabilities') or {}).get('supplier_sheet_trim_v1')
                if _has_supplier_trim_facts(db) and not (type(trim_reader) is int and trim_reader == 1):
                    raise ValueError('恢复程序不支持已保存的供应商修边尺寸，尚未启用恢复数据')
            dispatch_activation = dispatch_contract.validate_recovery(payload / 'shared', manifest, release_manifest)
            validate_database_schema(database, contract)
            rebind_pdf_sources(database, Path(manifest['source_shared']),
                               payload / 'shared', self.root / 'shared')
            from desktop_assistant.preflight import inspect
            checks = inspect(database, payload / 'shared',
                             managed=True, recorded_root=self.root / 'shared')
            if any(checks['counts'][key] for key in ('missing', 'external', 'hash_mismatch')):
                raise ValueError('恢复附件缺失或校验失败，尚未启用恢复数据')
            if database_info(database) != info:
                raise ValueError('恢复库最终完整性、表计数或版本复核失败')
            validate_database_schema(database, contract)
            # Promote program caches only after all payload checks pass.
            if authority:
                self.stage_release(compatible_package, verify_existing=True)
            self.stage_release(payload / 'release.zip', verify_existing=True)
            (self.root / 'shared').rmdir()  # proven empty above
            (payload / 'shared').rename(self.root / 'shared')
            write_json(self.root / 'state.json', {'current': release['id'], 'previous': None,
                       'restored_at': datetime.now(CN).isoformat(), 'source_time': manifest['created'],
                       'schema_authority': authority,
                       **({dispatch_contract.STATE_KEY: dispatch_contract.state_index(dispatch_activation)}
                          if dispatch_activation else {})})
            # A new PC's LAN URLs, firewall and printer need explicit local configuration.
            return {'version': release['version'], 'data_time': manifest['created'], 'started': False}

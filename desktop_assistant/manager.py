"""Single-host maintenance engine. Data rollback is never an update fallback."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import uuid

import psutil

from desktop_assistant.storage import (database_info, decrypt_file, encrypt_file,
                                      extract_verified, pack_tree, read_json, sha, write_json)
from desktop_assistant.attachments import rebind_pdf_sources

CN = timezone(timedelta(hours=8))


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
        import msvcrt
        with (self.root / 'control' / 'operation.lock').open('a+b') as stream:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError('另一个更新、恢复或备份正在进行') from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def manifest(self, release=None):
        return read_json(self.root / 'releases' / (release or self.state['current']) / 'manifest.json')

    def stage_release(self, package: Path) -> dict:
        # Hash names avoid arbitrary version strings becoming paths.
        identity = sha(package)
        destination = self.root / 'releases' / identity
        if not destination.exists():
            stage = self.root / 'staging' / uuid.uuid4().hex
            manifest = extract_verified(package, stage, self.public_key)
            if manifest.get('type') != 'tianming.release.v1':
                raise ValueError('发布包类型不匹配')
            required = ('runtime/python.exe', 'main.py', 'app/main.py', 'desktop_assistant/server_entry.py')
            if not all((stage / p).is_file() for p in required):
                raise ValueError('发布包缺少程序或运行环境')
            if any((stage / p).exists() for p in ('data', '.env', 'static/uploads', 'factory_twin/data')):
                raise ValueError('程序发布包不得携带业务数据或配置')
            stage.rename(destination)
        # Preserve the signed source for recovery even after app data junctions exist.
        cache = self.root / 'packages' / (identity + '.zip')
        if not cache.exists():
            shutil.copyfile(package, cache)
        if sha(cache) != identity:
            raise ValueError('缓存发布包校验失败')
        return {'id': identity, **read_json(destination / 'manifest.json')}

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

    def _environment(self, release: Path):
        shared = self.root / 'shared'
        saved = read_json(shared / 'environment.json')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ERP_', 'TM_ERP_', 'PYTHON'))}
        for key, value in saved.items():
            env[key] = value.replace('${SHARED}', str(shared))
        env['PYTHONUTF8'] = '1'
        env['PYTHONPATH'] = str(release)
        env['ERP_DATABASE_PATH'] = str(shared / 'data/carton_erp.sqlite3')
        env['ERP_WORKERS'] = '1'
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
        if self._process():
            return
        state = self.state
        release = self.root / 'releases' / state['current']
        info = database_info(self.root / 'shared/data/carton_erp.sqlite3')
        if info['revision'] != self.manifest()['revision']:
            raise ValueError('程序与数据库版本不同，须先完成专项迁移演练')
        self._link_data(release)
        env = self._environment(release)
        port = int(env['ERP_PORT'])
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
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
        before = database_info(self.root / 'shared/data/carton_erp.sqlite3')
        job = self.root / 'staging' / uuid.uuid4().hex
        job.mkdir()
        tree = job / 'payload'
        tree.mkdir()
        # Full managed data includes attachments, layouts, accounts and environment.
        shutil.copytree(self.root / 'shared', tree / 'shared')
        package = self.root / 'packages' / (current + '.zip')
        if sha(package) != current:
            raise ValueError('对应程序发布包已损坏')
        shutil.copyfile(package, tree / 'release.zip')
        if database_info(tree / 'shared/data/carton_erp.sqlite3') != before:
            raise ValueError('备份数据复核不一致')
        # Verify every registered PDF is included; the private copy keeps the original paths.
        rebind_pdf_sources(tree / 'shared/data/carton_erp.sqlite3', self.root / 'shared',
                           tree / 'shared', self.root / 'shared')
        stamp = datetime.now(CN).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        raw = job / 'recovery.zip'
        pack_tree(tree, raw, {'type': 'tianming.recovery.v1', 'created': datetime.now(CN).isoformat(),
                            'release': current, 'version': self.manifest()['version'], 'database': before,
                            'source_shared': str(self.root / 'shared')})
        encrypted = self.root / 'backups' / (stamp + '.tmbackup')
        encrypt_file(raw, encrypted, password)
        # Authenticate and re-read the completed local package before copying to NAS.
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
        state.update(last_backup=str(final), last_backup_at=datetime.now(CN).isoformat(), backup_error=None)
        write_json(self.root / 'state.json', state)
        # Staging only, never remove managed shared data or a user-selected directory.
        if job.resolve().parent != (self.root / 'staging').resolve():
            raise ValueError('临时目录清理边界不匹配')
        shutil.rmtree(job)
        return final

    def backup(self, password: str, nas: Path):
        with self.lock():
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

    def update(self, package: Path, password: str, nas: Path, *, rollback=False):
        with self.lock():
            candidate = self.stage_release(package)
            old = self.state
            if candidate['id'] == old['current']:
                return '已经是该版本'
            revision = database_info(self.root / 'shared/data/carton_erp.sqlite3')['revision']
            if candidate['revision'] != revision:
                raise ValueError('此版本需要数据库迁移：自动更新暂不执行，须使用已验证迁移发布流程')
            self.stop()
            try:
                backup = self._backup_stopped(password, nas)
            except Exception:
                self.start()
                raise
            new = self.state
            new.update(current=candidate['id'], previous=old['current'], update_backup=str(backup),
                       operation='rollback' if rollback else 'update', updated_at=datetime.now(CN).isoformat())
            write_json(self.root / 'state.json', new)
            try:
                self.start()
            except Exception:
                self.stop()
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
            release = self.stage_release(payload / 'release.zip')
            if release['revision'] != info['revision']:
                raise ValueError('备份程序与数据不兼容')
            rebind_pdf_sources(payload / 'shared/data/carton_erp.sqlite3', Path(manifest['source_shared']),
                               payload / 'shared', self.root / 'shared')
            (self.root / 'shared').rmdir()  # proven empty above
            (payload / 'shared').rename(self.root / 'shared')
            write_json(self.root / 'state.json', {'current': release['id'], 'previous': None,
                       'restored_at': datetime.now(CN).isoformat(), 'source_time': manifest['created']})
            # A new PC's LAN URLs, firewall and printer need explicit local configuration.
            return {'version': release['version'], 'data_time': manifest['created'], 'started': False}

"""Explicit one-time COPY into an empty managed installation; never modifies source."""
from pathlib import Path
import shutil
import socket

import psutil

from desktop_assistant.storage import database_info, read_json, sha, write_json
from desktop_assistant.attachments import rebind_pdf_sources


def import_existing(manager, source: Path, package: Path):
    source = source.resolve()
    if source == manager.root or source in manager.root.parents or manager.root in source.parents:
        raise ValueError('导入源和助手目录必须相互独立')
    with manager.lock():
        if manager.state['current'] or any((manager.root / 'shared').iterdir()):
            raise ValueError('仅允许导入到空安装目录')
        # Read .env locally; do not print secrets or accept unrelated machine paths.
        env = {}
        envfile = source / '.env'
        if not envfile.is_file():
            raise ValueError('原ERP缺少.env，请由工厂先核对运行配置')
        for line in envfile.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
        if any(k.startswith('ERP_UAT_') for k in env):
            raise ValueError('不能把家庭UAT配置导入为正式安装')
        for proc in psutil.process_iter(['cmdline']):
            try:
                cmd = ' '.join(proc.info['cmdline'] or []).casefold()
                if str(source).casefold() in cmd and ('uvicorn' in cmd or 'server_entry' in cmd):
                    raise ValueError('原ERP仍在运行，请正常退出后导入，助手不强制接管')
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        port = int(env.get('ERP_PORT', '8000'))
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError('原ERP端口仍占用，请先正常停止服务')
        db = source / 'data/carton_erp.sqlite3'
        actual = Path(env.get('ERP_DATABASE_PATH', str(db)))
        if not actual.is_absolute():
            actual = source / actual
        if actual.resolve() != db.resolve():
            raise ValueError('原数据库不在标准data目录，需先制定路径迁移清单')
        before = sha(db)
        info = database_info(db)
        release = manager.stage_release(package)
        if info['revision'] != release['revision']:
            raise ValueError('原数据与安装包迁移版本不一致')
        shared = manager.root / 'shared'
        # Validate custom paths before copying. Absolute paths outside the source cannot travel.
        for key, value in list(env.items()):
            if key.startswith('ERP_') and key.endswith(('_PATH', '_DIR', '_FILE')):
                if key == 'ERP_BACKUP_DIR':
                    env[key] = '${SHARED}/database_backups'
                    continue
                path = Path(value)
                if not path.is_absolute():
                    path = source / path
                try:
                    rel = path.resolve().relative_to(source).as_posix()
                except ValueError:
                    raise ValueError('配置含外部数据路径，需先归档：' + key) from None
                if rel.startswith('data/'):
                    env[key] = '${SHARED}/' + rel
                elif rel.startswith('static/uploads/'):
                    env[key] = '${SHARED}/legacy_uploads/' + rel[len('static/uploads/'):]
                elif rel.startswith('factory_twin/data/'):
                    env[key] = '${SHARED}/factory_twin_data/' + rel[len('factory_twin/data/'):]
                else:
                    raise ValueError('尚未纳入恢复范围的路径：' + key)
        for old, new in [('data', 'data'), ('static/uploads', 'legacy_uploads'), ('factory_twin/data', 'factory_twin_data')]:
            src = source / old
            if src.exists():
                if any(p.is_symlink() or p.is_junction() for p in [src, *src.rglob('*')]):
                    raise ValueError('原数据含目录链接，需先核对真实位置')
                shutil.copytree(src, shared / new, ignore=shutil.ignore_patterns('backups', '__pycache__'))
        if sha(db) != before or sha(shared / 'data/carton_erp.sqlite3') != before:
            raise ValueError('导入期间原数据库发生变化，未启用新系统')
        rebind_pdf_sources(shared / 'data/carton_erp.sqlite3', source, shared, shared)
        env['ERP_SECRET_KEY_FILE'] = '${SHARED}/data/session_secret.key'
        env['ERP_BACKUP_DIR'] = '${SHARED}/database_backups'
        env.setdefault('ERP_PORT', '8000')
        env.setdefault('ERP_BIND_HOST', '127.0.0.1')
        write_json(shared / 'environment.json', env)
        write_json(manager.root / 'state.json', {'current': release['id'], 'previous': None,
                   'imported_source': str(source), 'source_sha256': before})
        return '原系统已只读复制；请先核对本机网络设置并执行完整备份，再启用'

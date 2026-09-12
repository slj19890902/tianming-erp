"""Explicit one-time COPY into an empty managed installation; never modifies source."""
from pathlib import Path
import shutil
import socket
import uuid

import psutil

from desktop_assistant.storage import database_info, read_json, sha, write_json
from desktop_assistant.attachments import rebind_pdf_sources


COPY_TREES = (('data', 'data'), ('static/uploads', 'legacy_uploads'),
              ('factory_twin/data', 'factory_twin_data'))


def _copy_ignore(source, directory, names):
    """One policy for preflight and copy; do not follow ignored tool dependencies."""
    relative = Path(directory).relative_to(source)
    ignored = {name for name in names if name in {'backups', '__pycache__'}}
    if relative.is_relative_to('data/work'):
        ignored.update(name for name in names if name.casefold() == 'node_modules')
    for name in set(names) - ignored:
        _reject_link(source, Path(directory) / name)
    return ignored


def _reject_link(source, path):
    if path.is_symlink() or path.is_junction():
        raise ValueError('原数据含目录或文件链接：' + path.relative_to(source).as_posix()
                         + '；需核对真实位置后接入')


def check_source_data(source):
    """Inspect only the copied trees, before cutover; never traverse a link."""
    for old, _ in COPY_TREES:
        root = source / old
        path = root
        while path != source:
            _reject_link(source, path)  # Includes dangling roots and linked ancestors.
            path = path.parent
        if not root.exists():
            continue
        pending = [root]
        while pending:
            directory = pending.pop()
            entries = list(directory.iterdir())
            ignored = _copy_ignore(source, directory, [p.name for p in entries])
            pending.extend(p for p in entries if p.name not in ignored and p.is_dir())


def import_existing(manager, source: Path, package: Path):
    source = source.resolve()
    if source == manager.root or source in manager.root.parents or manager.root in source.parents:
        raise ValueError('导入源和助手目录必须相互独立')
    with manager.lock():
        return _import_locked(manager, source, package)


def _import_locked(manager, source, package):
    if manager.state['current'] or any((manager.root / 'shared').iterdir()):
        raise ValueError('仅允许导入到空安装目录')
    check_source_data(source)
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
        host = env.get('ERP_BIND_HOST', '127.0.0.1')
        if host in ('0.0.0.0', '::'):
            host = '127.0.0.1'
        if probe.connect_ex((host, port)) == 0:
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
    final_shared = manager.root / 'shared'
    shared = manager.root / 'staging' / ('import-' + uuid.uuid4().hex)
    shared.mkdir()
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
    for old, new in COPY_TREES:
        src = source / old
        _reject_link(source, src)
        if src.exists():
            shutil.copytree(src, shared / new,
                            ignore=lambda directory, names: _copy_ignore(source, directory, names))
    if sha(db) != before or sha(shared / 'data/carton_erp.sqlite3') != before:
        raise ValueError('导入期间原数据库发生变化，未启用新系统')
    rebind_pdf_sources(shared / 'data/carton_erp.sqlite3', source, shared, final_shared, importing=True)
    env['ERP_SECRET_KEY_FILE'] = '${SHARED}/data/session_secret.key'
    env['ERP_BACKUP_DIR'] = '${SHARED}/database_backups'
    env.setdefault('ERP_PORT', '8000')
    env.setdefault('ERP_BIND_HOST', '127.0.0.1')
    write_json(shared / 'environment.json', env)
    database_info(shared / 'data/carton_erp.sqlite3')
    final_shared.rmdir()  # Empty destination verified while holding the operation lock.
    shared.rename(final_shared)
    write_json(manager.root / 'state.json', {'current': release['id'], 'previous': None,
               'imported_source': str(source), 'source_sha256': before})
    return '原系统已只读复制；请先核对本机网络设置并执行完整备份，再启用'

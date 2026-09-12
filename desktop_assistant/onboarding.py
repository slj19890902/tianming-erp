"""Explicit first factory cutover, using the existing production process guard."""
from pathlib import Path
from contextlib import closing
import ast
import re
import sqlite3
import subprocess
import uuid

from desktop_assistant.storage import database_info, sha, write_json
from desktop_assistant.import_existing import _import_locked
from desktop_assistant.preflight import inspect
from desktop_assistant.windows import run_maintenance_powershell


def _same_script(actual, reference):
    if not actual.is_file() or actual.is_symlink() or actual.is_junction():
        raise ValueError('原ERP缺少可核验的维护程序，请先更新原ERP')
    if actual.read_text(encoding='utf-8-sig') != reference.read_text(encoding='utf-8-sig'):
        raise ValueError('原ERP维护程序与签名安装包不同，请先由维护人员核对版本；尚未停服')


def check_original_runtime(source, release, *, stop=False):
    relative = 'scripts/admin/release_erp.ps1'
    _same_script(source / relative, release / relative)
    quoted = str(source / relative).replace("'", "''")
    # Reuse all original executable, app-dir, single-worker, port and DB checks.
    # No arbitrary port-killing or new process-name-only stop implementation.
    command = (f"$ErrorActionPreference='Stop'; . '{quoted}' -LibraryOnly; "
               "Initialize-ReleaseRuntime; "
               "$listeners=Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue; "
               "foreach($item in $listeners){Get-ValidatedErpProcess -ProcessId $item.OwningProcess | Out-Null}; ")
    if stop:
        command += "Set-ErpMaintenanceLock 'ERP assistant first setup'; Stop-ErpService; Assert-ErpStopped"
    result = run_maintenance_powershell(command, source)
    if result.returncode:
        detail = (result.stderr or b'')
        if b'UnauthorizedAccess' in detail or b'PSSecurityException' in detail:
            raise ValueError('Windows阻止了原ERP维护脚本，尚未继续接入。请管理员检查电脑的脚本运行策略；原数据保留。')
        raise ValueError('原ERP未通过停服检查，未继续接入。请查看原ERP的logs/erp_release.log；不要强制结束进程')


def stop_original(source, release):
    check_original_runtime(source, release, stop=True)


def self_test_powershell():
    """Exercise the packaged child process using only an isolated local fixture."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix='tm-powershell-check-') as temp:
        root = Path(temp)
        source, release = root / 'original', root / 'signed'
        trace = str(root / 'checked.txt').replace("'", "''")
        script = (
            'param([switch]$LibraryOnly)\n'
            'function Initialize-ReleaseRuntime { $script:ErpPort=1 }\n'
            'function Get-NetTCPConnection { }\n'
            'function Get-ValidatedErpProcess { throw "unexpected listener" }\n'
            'function Set-ErpMaintenanceLock { }\n'
            'function Stop-ErpService { }\n'
            f"function Assert-ErpStopped {{ Set-Content -LiteralPath '{trace}' -Value 'passed' }}\n"
        )
        for directory in (source, release):
            path = directory / 'scripts/admin/release_erp.ps1'
            path.parent.mkdir(parents=True)
            path.write_text(script, encoding='utf-8-sig')
        stop_original(source, release)
        assert (root / 'checked.txt').read_text().strip() == 'passed'


def _check_nas(password, nas):
    if len(password) < 12:
        raise ValueError('请先设置至少12个字符的恢复密码')
    if not nas.is_dir():
        raise ValueError('备份共享文件夹不可用，尚未停服')
    probe = nas / ('assistant-write-check-' + uuid.uuid4().hex + '.tmp')
    try:
        with probe.open('xb') as f:
            f.write(b'ERP backup write check')
    finally:
        probe.unlink(missing_ok=True)


def check_source_environment(source):
    env = {}
    for line in (source / '.env').read_text(encoding='utf-8-sig').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    if any(k.startswith('ERP_UAT_') for k in env):
        raise ValueError('不能将测试配置接入正式助手；尚未停服')
    database = Path(env.get('ERP_DATABASE_PATH', 'data/carton_erp.sqlite3'))
    if not database.is_absolute():
        database = source / database
    if database.resolve() != (source / 'data/carton_erp.sqlite3').resolve():
        raise ValueError('原数据库不在标准data目录，尚未停服')
    for key, value in env.items():
        if key.startswith('ERP_') and key.endswith(('_PATH', '_DIR', '_FILE')) and key != 'ERP_BACKUP_DIR':
            path = Path(value)
            path = path if path.is_absolute() else source / path
            try:
                rel = path.resolve().relative_to(source).as_posix()
            except ValueError:
                raise ValueError('原配置有未归档的外部文件：' + key + '；尚未停服') from None
            if not rel.startswith(('data/', 'static/uploads/', 'factory_twin/data/')):
                raise ValueError('原配置路径尚不支持完整恢复：' + key + '；尚未停服')


def check_source_version(source, release):
    try:
        tree = ast.parse((source / 'app/version.py').read_text(encoding='utf-8-sig'))
        value = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'APP_VERSION' for t in node.targets))
        incoming = release['version']
        if value == incoming:
            return
        original = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', value)
        candidate = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', incoming)
        if not original or not candidate:
            raise ValueError('invalid version')
    except (OSError, ValueError, TypeError, KeyError, StopIteration, SyntaxError):
        raise ValueError('无法核对原ERP与安装包的程序版本，请检查完整安装包；尚未停服') from None
    if tuple(map(int, original.groups())) > tuple(map(int, candidate.groups())):
        raise ValueError(f'安装包ERP {incoming} 比原ERP {value} 旧，请先运行最新版助手安装器更新此安装；尚未停服')


def onboard(manager, source, package, password, nas):
    source = source.resolve()
    if source == manager.root or source in manager.root.parents or manager.root in source.parents:
        raise ValueError('助手不能安装在原ERP文件夹内。请将助手安装到独立目录，例如D:\\TianmingERP，再选择原ERP整个文件夹')
    with manager.lock():
        if manager.state.get('current') or any((manager.root / 'shared').iterdir()):
            raise ValueError('首次接入只能用于空助手；已有数据请直接启动或更新')
        _check_nas(password, nas)
        if not (source / '.env').is_file():
            raise ValueError('请选择包含.env和data的原ERP整个文件夹，不要只选择data')
        if (source / 'data/runtime/erp_managed_installation.json').exists():
            raise ValueError('原ERP已经接入另一助手，请打开已接入的助手，不要重复复制')
        check_source_environment(source)
        release = manager.stage_release(package)
        check_source_version(source, release)
        release_root = manager.root / 'releases' / release['id']
        database = source / 'data/carton_erp.sqlite3'
        before = database_info(database)
        if before['revision'] != release['revision']:
            raise ValueError('原ERP数据版本与安装包不同，请更新安装包后再接入；尚未停服')
        checks = inspect(database, source)
        if any(checks['counts'][key] for key in ('missing', 'external', 'hash_mismatch')):
            raise ValueError('原ERP附件尚不满足完整备份条件，尚未停服')
        # A verified, consistent local point-in-time copy exists BEFORE stopping.
        backup = manager.root / 'backups' / ('before-first-setup-' + uuid.uuid4().hex + '.sqlite3')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(backup)) as dst:
                src.backup(dst)
        database_info(backup)
        write_json(manager.root / 'control/first-setup.json',
                   {'source': str(source), 'backup': str(backup), 'sha256': sha(backup), 'stage': 'before_stop'})
        stop_original(source, release_root)
        _import_locked(manager, source, package)
        state = manager.state
        state['onboarding_pending'] = True
        write_json(manager.root / 'state.json', state)
        return _finish_locked(manager, password, nas)


def _finish_locked(manager, password, nas):
    _check_nas(password, nas)
    source = Path(manager.state['imported_source'])
    # Never resume a stale copied ledger after the original has been used again.
    if sha(source / 'data/carton_erp.sqlite3') != manager.state['source_sha256']:
        raise ValueError('原ERP数据在复制后发生变化，不能启用旧副本；请保留两边数据并联系维护人员核对')
    backup = manager._backup_stopped(password, nas)
    marker = source / 'data/runtime/erp_managed_installation.json'
    marker.parent.mkdir(parents=True, exist_ok=True)
    write_json(marker, {'managed_root': str(manager.root), 'backup': str(backup),
                        'source_sha256': manager.state['source_sha256']})
    state = manager.state
    state.update(onboarding_pending=False, manual_stop=False)
    write_json(manager.root / 'state.json', state)
    manager.start()
    return '首次接入完成，完整备份已保存，ERP已启动。以后从本助手启动、备份、停服和更新；不要再启动原目录的ERP。'


def finish_onboarding(manager, password, nas):
    with manager.lock():
        if not manager.state.get('onboarding_pending'):
            raise ValueError('当前没有待完成的首次接入')
        return _finish_locked(manager, password, nas)

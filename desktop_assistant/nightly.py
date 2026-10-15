"""Due-time backup checks serialized with all other maintenance operations."""
from datetime import datetime, timedelta
from pathlib import Path

from desktop_assistant.credential_store import unprotect_backup
from desktop_assistant.manager import CN
from desktop_assistant.operation_lock import BUSY
from desktop_assistant.storage import read_json, write_json
from desktop_assistant import mac_backup_storage


DATABASE_INTERVAL = timedelta(minutes=15)
DATABASE_STALE_AFTER = timedelta(minutes=30)
DATABASE_HEALTH = 'control/database-backup-health.json'


def database_backup_health(manager, *, now=None):
    """Return a small, non-secret status for the independent DB snapshot."""
    current = now or datetime.now(CN)
    state = manager.state
    path = manager.root/DATABASE_HEALTH
    previous = read_json(path) if path.is_file() else {}
    failures = previous.get('consecutive_failures', 0)
    if type(failures) is not int or failures < 0:
        failures = 0
    last = state.get('last_database_snapshot_at')
    try:
        completed = datetime.fromisoformat(last) if isinstance(last, str) else None
        if completed is not None and completed.utcoffset() is None:
            completed = None
    except ValueError:
        completed = None
    age = max(0, int((current - completed).total_seconds())) if completed else None
    saved = state.get('last_database_snapshot')
    point = Path(saved) if isinstance(saved, str) else None
    directory = manager.root/'backups/database-points'
    available = bool(point and point.parent == directory and point.suffix == '.tmdb'
                     and point.is_file() and not point.is_symlink())
    stale = not available or age is None or age > DATABASE_STALE_AFTER.total_seconds()
    return {
        'type': 'tianming.database-backup-health.v1',
        'checked_at': current.isoformat(),
        'last_success_at': last if completed else None,
        'age_seconds': age,
        'last_point_available': available,
        'consecutive_failures': failures,
        'stale': stale,
        'alert_code': 'DATABASE_BACKUP_STALE' if stale else None,
    }


def _record_database_health(manager, *, now, failed=False):
    status = database_backup_health(manager, now=now)
    if failed:
        status['consecutive_failures'] += 1
    else:
        status['consecutive_failures'] = 0
    status['last_attempt_at'] = now.isoformat()
    status['last_attempt_status'] = 'failed' if failed else 'verified'
    write_json(manager.root/DATABASE_HEALTH, status)
    return status


def run_database_snapshot_once(manager, *, now=None, admission=None):
    """Create the frequent local DB-only point without stopping ERP or using NAS.

    This schedule is deliberately separate from the daily full archive.  The
    returned ``.tmdb`` is not a complete recovery point and the v2 restore gate
    will reject it as one.
    """
    try:
        with manager.lock():
            state = manager.state
            if (not state.get('current') or state.get('onboarding_pending')
                    or state.get('manual_stop')):
                return {'status': 'skipped'}
            operation = state.get('operation', '')
            if operation == 'migration_running' or 'failed' in operation:
                return {'status': 'maintenance-attention'}
            if admission is not None:
                permitted = admission()
                if type(permitted) is not bool:
                    raise ValueError('数据库备份启动门禁无效')
                if not permitted:
                    return {'status': 'system-disabled'}
            current = now or datetime.now(CN)
            previous = state.get('last_database_snapshot_at')
            health = database_backup_health(manager, now=current)
            if previous and not health['stale'] and current - datetime.fromisoformat(previous) < DATABASE_INTERVAL:
                return {'status': 'up-to-date'}
            try:
                settings = read_json(manager.root/'preferences.json')
                password = unprotect_backup(settings['protected_password'])
                destination = manager.root/'backups'/'database-points'
                destination.mkdir(mode=0o700, exist_ok=True)
                stamp = current.strftime('%Y%m%d-%H%M%S')
                import uuid
                output = destination/(stamp+'-'+uuid.uuid4().hex[:8]+'.tmdb')
                from desktop_assistant import data_recovery
                result = data_recovery.create(
                    manager.root/'shared',
                    manager.root/'packages'/(state['current']+'.zip'),
                    output,
                    password,
                    manager.public_key,
                    database_only=True,
                    status_path=manager.root/'control/database-backup-status.json',
                    log_path=manager.root/'control/database-backup.log',
                )
                final = manager.state
                if final != state:
                    raise ValueError('数据库备份期间管理状态变化，恢复点保留待核')
                final['last_database_snapshot'] = str(output)
                final['last_database_snapshot_at'] = current.isoformat()
                final['last_database_snapshot_sha256'] = result['backup_sha256']
                write_json(manager.root/'state.json', final)
                health = _record_database_health(manager, now=current)
                return {'status': 'verified', 'backup': str(output), 'result': result,
                        'health': health}
            except Exception:
                _record_database_health(manager, now=current, failed=True)
                raise
    except ValueError as error:
        if str(error) == BUSY:
            return {'status': 'maintenance-busy'}
        raise


def run_once(manager, *, now=None, resume_when_stopped=False, admission=None, restart_allowed=None):
    stream_plan=None;stream_destination=None;password=None
    try:
        with manager.lock():
            state = manager.state
            if not state.get('current') or state.get('onboarding_pending') or state.get('manual_stop'):
                return {'status':'skipped'}
            operation = state.get('operation', '')
            if operation == 'migration_running' or 'failed' in operation:
                return {'status':'maintenance-attention'}
            if admission is not None:
                permitted = admission()  # Fixed root control, under the same maintenance lock.
                if type(permitted) is not bool: raise ValueError('系统备份启动门禁无效')
                if not permitted: return {'status':'system-disabled'}
            if restart_allowed is not None and not restart_allowed():
                return {'status':'shutting-down'}
            try:
                current = now or datetime.now(CN)
                due = current.replace(hour=23, minute=0, second=0, microsecond=0)
                if current < due:
                    due -= timedelta(days=1)
                latest = mac_backup_storage.verified_at(manager.root, state, now=current)
                if latest and datetime.fromisoformat(latest) >= due:
                    if resume_when_stopped:
                        manager.start()
                    return {'status':'up-to-date'}
                settings = read_json(manager.root/'preferences.json')
                from desktop_assistant import local_backup_only as local_only
                local_policy=local_only.enabled(manager)
                destination = manager.root/'backups' if local_policy else Path(settings['nas'])
                if not local_policy:mac_backup_storage.prepare_destination(destination)
                password = unprotect_backup(settings['protected_password'])
                from desktop_assistant import mac_smb_stream_target as stream
                if not local_policy and stream.selected(destination) and stream.rotation_enabled(destination):
                    stream_destination=destination
                    stream_plan=manager._capture_stream_locked(password,destination,restart_allowed=restart_allowed)
                else:
                    backup = (manager._backup_locked(password, destination, restart_allowed=restart_allowed)
                              if restart_allowed is not None else manager._backup_locked(password, destination))
                    if resume_when_stopped:manager.start()
                    return {'status':'verified', 'backup':str(backup)}
            except Exception:
                # Set a public status while still holding the maintenance lock.
                # Do not persist native credential errors or their local values.
                state = manager.state
                state['backup_error'] = '自动备份未完成，请检查服务用户凭据、NAS连接及维护状态'
                write_json(manager.root/'state.json', state)
                raise
        # Long transfer/independent restore never owns the maintenance lock.
        try:
            backup=manager._finish_stream_backup(password,stream_destination,stream_plan)
            return {'status':'verified','backup':str(backup)}
        except Exception:
            with manager.lock():
                state=manager.state;state['backup_error']='自动备份独立验证或两份保留尚未完成；保留当前任务，请管理员核查'
                write_json(manager.root/'state.json',state)
            raise
        finally:password=None
    except ValueError as error:
        if str(error) == BUSY:
            return {'status':'maintenance-busy'}
        raise

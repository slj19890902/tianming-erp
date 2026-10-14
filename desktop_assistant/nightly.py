"""Due-time backup checks serialized with all other maintenance operations."""
from datetime import datetime, timedelta
from pathlib import Path

from desktop_assistant.credential_store import unprotect_backup
from desktop_assistant.manager import CN
from desktop_assistant.operation_lock import BUSY
from desktop_assistant.storage import read_json, write_json
from desktop_assistant import mac_backup_storage


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

"""Due-time backup checks serialized with all other maintenance operations."""
from datetime import datetime, timedelta
from pathlib import Path

from desktop_assistant.credential_store import unprotect_backup
from desktop_assistant.manager import CN
from desktop_assistant.operation_lock import BUSY
from desktop_assistant.storage import read_json, write_json


def run_once(manager, *, now=None, resume_when_stopped=False):
    try:
        with manager.lock():
            state = manager.state
            if not state.get('current') or state.get('onboarding_pending') or state.get('manual_stop'):
                return {'status':'skipped'}
            operation = state.get('operation', '')
            if operation == 'migration_running' or 'failed' in operation:
                return {'status':'maintenance-attention'}
            try:
                current = now or datetime.now(CN)
                due = current.replace(hour=23, minute=0, second=0, microsecond=0)
                if current < due:
                    due -= timedelta(days=1)
                latest = state.get('last_backup_at')
                if latest and datetime.fromisoformat(latest) >= due:
                    if resume_when_stopped:
                        manager.start()
                    return {'status':'up-to-date'}
                settings = read_json(manager.root/'preferences.json')
                password = unprotect_backup(settings['protected_password'])
                destination = Path(settings['nas'])
                backup = manager._backup_locked(password, destination)
                if resume_when_stopped:
                    manager.start()
                return {'status':'verified', 'backup':str(backup)}
            except Exception:
                # Set a public status while still holding the maintenance lock.
                # Do not persist native credential errors or their local values.
                state = manager.state
                state['backup_error'] = '自动备份未完成，请检查服务用户凭据、NAS连接及维护状态'
                write_json(manager.root/'state.json', state)
                raise
    except ValueError as error:
        if str(error) == BUSY:
            return {'status':'maintenance-busy'}
        raise

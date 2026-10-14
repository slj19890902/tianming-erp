"""A same-schema switch must not precede its durable recovery proof."""
from pathlib import Path
import os
from unittest.mock import patch
import pytest
from desktop_assistant import local_code_update as local
from desktop_assistant.storage import sha, write_json
from tests.desktop_assistant.test_stopped_update import case, PASSWORD, forbidden_runtime


def test_recovery_files_and_directories_are_durable_before_state_switch(case):
    manager = case.manager
    candidate = case.release('two')
    events = []
    real_fsync = os.fsync
    real_write = local.write_json
    def sync(fd):
        identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        matches = [p for p in manager.root.rglob('*') if not p.is_symlink()
                   and (p.stat().st_dev, p.stat().st_ino) == identity]
        if identity == (manager.root.stat().st_dev, manager.root.stat().st_ino):
            matches.append(manager.root)
        events.extend(('fsync', str(p.relative_to(manager.root))) for p in matches)
        return real_fsync(fd)
    def write(path, value):
        events.append(('write', str(path.relative_to(manager.root))))
        return real_write(path, value)
    with manager.lock(), forbidden_runtime(manager), patch.object(local.os, 'fsync', sync), patch.object(local, 'write_json', write):
        local.update(manager, candidate, PASSWORD, sha(manager.root/'state.json'))
    job = next((manager.root/'staging').glob('local-code-update-*'))
    state_write = events.index(('write', 'state.json'))
    for name in ['backups', str(job.relative_to(manager.root)), 'staging']:
        assert ('fsync', name) in events and events.index(('fsync', name)) < state_write
    assert any(event[0] == 'fsync' and event[1].startswith('backups/') for event in events[:state_write])
    assert any(event[0] == 'fsync' and event[1].endswith('/verified.json.pending') for event in events[:state_write])
    assert events.index(('fsync', '.')) > state_write


@pytest.mark.parametrize('directory', ['backups', 'job', 'staging', 'root'])
def test_directory_sync_failure_never_claims_a_completed_switch(case, directory):
    manager = case.manager
    before = manager.state
    candidate = case.release('two')
    real_sync = os.fsync
    failed = []
    def fail(fd):
        target = next((manager.root/'staging').glob('local-code-update-*'), None) if directory == 'job' else manager.root if directory == 'root' else manager.root/directory
        if target and os.path.samestat(os.fstat(fd), target.stat()):
            failed.append(True)
            raise OSError('injected directory durability failure')
        return real_sync(fd)
    with manager.lock(), forbidden_runtime(manager), patch.object(local.os, 'fsync', fail), pytest.raises(OSError, match='durability'):
        local.update(manager, candidate, PASSWORD, sha(manager.root/'state.json'))
    assert failed
    if directory == 'root':
        assert manager.state['current'] == sha(candidate)
        assert manager.state['previous'] == before['current']
    else:
        assert manager.state == before
    jobs = list((manager.root/'staging').glob('local-code-update-*'))
    assert jobs and any(p.name != 'verified.json' for p in jobs[0].iterdir())
    assert list((manager.root/'backups').glob('*.tmbackup'))

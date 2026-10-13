from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import pytest
from desktop_assistant.local_code_update import update
from desktop_assistant.storage import sha, write_json, decrypt_file, extract_verified
from tests.desktop_assistant.test_stopped_update import case, PASSWORD, forbidden_runtime, program


def test_complete_local_backup_preserves_remote_attention_and_all_data(case):
    manager = case.manager
    state = manager.state
    state['backup_error'] = 'remote failure retained'
    write_json(manager.root/'state.json', state)
    old = deepcopy(manager.state)
    database = manager.root/'shared/data/carton_erp.sqlite3'
    before = sha(database)
    package = case.release('two')
    with manager.lock(), forbidden_runtime(manager):
        proof = update(manager, package, PASSWORD, sha(manager.root/'state.json'))
    assert sha(database) == before
    after = manager.state
    assert after['backup_error'] == old['backup_error']
    assert after.get('last_backup') == old.get('last_backup')
    assert after.get('last_backup_at') == old.get('last_backup_at')
    assert after['current'] == sha(package) and after['previous'] == old['current']
    assert proof['nas_verified'] is False and proof['independent_device'] is False
    assert proof['restored_verified'] is True and after['manual_stop'] is True
    jobs = list((manager.root/'staging').glob('local-code-update-*'))
    assert len(jobs) == 1 and {p.name for p in jobs[0].iterdir()} == {'verified.json'}
    raw = case.root/'verified-local.zip'
    assert decrypt_file(Path(after['update_backup']), raw, PASSWORD) == proof['backup_sha256']
    extract_verified(raw, case.root/'verified-local-recovery')
    assert sha(case.root/'verified-local-recovery/shared/data/carton_erp.sqlite3') == before


@pytest.mark.parametrize('field,value', [('manual_stop',False), ('onboarding_pending',True),
    ('backup_work_retained',True), ('operation','migration_running')])
def test_unfinished_instance_rejected(case, field, value):
    state = case.manager.state; state[field] = value
    write_json(case.manager.root/'state.json', state)
    with case.manager.lock(), pytest.raises(ValueError):
        update(case.manager, case.release('two'), PASSWORD, sha(case.manager.root/'state.json'))
    assert case.manager.state == state


def test_state_hash_and_sidecar_rejected(case):
    package = case.release('two')
    with case.manager.lock(), pytest.raises(ValueError):
        update(case.manager, package, PASSWORD, '0'*64)
    sidecar = Path(str(case.manager.root/'shared/data/carton_erp.sqlite3')+'-journal')
    sidecar.touch()
    with case.manager.lock(), pytest.raises(ValueError):
        update(case.manager, package, PASSWORD, sha(case.manager.root/'state.json'))


def test_signature_or_schema_change_rejected_without_state_switch(case):
    before = case.manager.state
    package = program(case, 'different', change_model=lambda s:s.replace(
        '    note: Mapped[str]', '    note: Mapped[str]\n    extra: Mapped[str]'))
    with case.manager.lock(), pytest.raises(ValueError):
        update(case.manager, package, PASSWORD, sha(case.manager.root/'state.json'))
    assert case.manager.state == before


def test_corrupt_independent_copy_keeps_original_current(case):
    before = case.manager.state
    package = case.release('two')
    with case.manager.lock(), patch('desktop_assistant.local_code_update.shutil.copyfileobj',
            side_effect=lambda src,dst,size:dst.write(b'bad')), pytest.raises(ValueError):
        update(case.manager, package, PASSWORD, sha(case.manager.root/'state.json'))
    assert case.manager.state == before
    assert list((case.manager.root/'staging').glob('local-code-update-*'))


def test_approval_changes_before_publication_keep_current(case):
    before = case.manager.state
    package = case.release('two')
    checks = []
    def revoked():
        checks.append(True)
        if len(checks) > 1:
            raise ValueError('approval changed')
    with case.manager.lock(), pytest.raises(ValueError):
        update(case.manager, package, PASSWORD, sha(case.manager.root/'state.json'), approval_check=revoked)
    assert case.manager.state == before
    assert list((case.manager.root/'backups').glob('*.tmbackup'))

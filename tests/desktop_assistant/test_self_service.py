from pathlib import Path
from unittest.mock import patch

import pytest

from desktop_assistant.gui import nightly
from desktop_assistant.manager import Manager
from desktop_assistant.onboarding import onboard, finish_onboarding, stop_original
from desktop_assistant.storage import read_json, sha, write_json
from tests.desktop_assistant import test_recovery as recovery
TestManager = recovery.TestManager
PASSWORD = recovery.PASSWORD


@pytest.fixture
def example():
    case = recovery.RecoveryTests()
    case.setUp()
    import shutil
    source = case.root / 'original'
    shutil.copytree(case.manager.root / 'shared', source)
    (source / '.env').write_text('ERP_PORT=18080\nERP_BIND_HOST=127.0.0.1\n', encoding='utf8')
    target = TestManager(case.root / 'new-install', case.public)
    yield case, source, target
    case.tearDown()


def offline():
    from unittest.mock import MagicMock
    socket = MagicMock()
    socket.__enter__.return_value.connect_ex.return_value = 1
    return socket


def test_first_setup_preserves_source_and_builds_recoverable_backup(example):
    case, source, target = example
    original = sha(source / 'data/carton_erp.sqlite3')
    def stop(*args):
        record = read_json(target.root / 'control/first-setup.json')
        assert sha(Path(record['backup'])) == record['sha256']
        assert not target.state.get('current')
    with patch('desktop_assistant.onboarding.stop_original', side_effect=stop), \
            patch('desktop_assistant.import_existing.psutil.process_iter', return_value=[]), \
            patch('desktop_assistant.import_existing.socket.socket', return_value=offline()):
        result = onboard(target, source, case.package, PASSWORD, case.nas)
    assert '首次接入完成' in result
    assert sha(source / 'data/carton_erp.sqlite3') == original
    assert not target.state['onboarding_pending']
    assert (source / 'data/runtime/erp_managed_installation.json').exists()
    recovered = TestManager(case.root / 'restore-proof', case.public)
    recovered.restore(Path(target.state['last_backup']), PASSWORD)
    assert sha(recovered.root / 'shared/data/carton_erp.sqlite3') == original


def test_missing_nas_or_nested_install_cannot_stop_source(example):
    case, source, target = example
    with patch('desktop_assistant.onboarding.stop_original') as stop:
        with pytest.raises(ValueError, match='共享'):
            onboard(target, source, case.package, PASSWORD, case.root / 'missing-nas')
        nested = TestManager(source / 'nested', case.public)
        with pytest.raises(ValueError, match='独立目录'):
            onboard(nested, source, case.package, PASSWORD, case.nas)
        stop.assert_not_called()
    assert target.state['current'] is None


def test_failed_full_backup_blocks_start_and_can_finish_after_repair(example):
    case, source, target = example
    with patch('desktop_assistant.onboarding.stop_original'), \
            patch('desktop_assistant.import_existing.psutil.process_iter', return_value=[]), \
            patch('desktop_assistant.import_existing.socket.socket', return_value=offline()), \
            patch.object(target, '_backup_stopped', side_effect=ValueError('NAS disconnected')):
        with pytest.raises(ValueError, match='NAS disconnected'):
            onboard(target, source, case.package, PASSWORD, case.nas)
    assert target.state['onboarding_pending']
    with pytest.raises(ValueError, match='首次接入'):
        Manager.start(target)
    finish_onboarding(target, PASSWORD, case.nas)
    assert not target.state['onboarding_pending']


def test_changed_source_refuses_stale_first_setup(example):
    case, source, target = example
    state = case.manager.state.copy()
    state.update(imported_source=str(source), source_sha256='0' * 64, onboarding_pending=True)
    write_json(case.manager.root / 'state.json', state)
    with patch.object(case.manager, '_backup_stopped') as backup:
        with pytest.raises(ValueError, match='发生变化'):
            finish_onboarding(case.manager, PASSWORD, case.nas)
        backup.assert_not_called()


def test_copy_failure_never_publishes_partial_shared_data(example):
    case, source, target = example
    with patch('desktop_assistant.onboarding.stop_original'), \
            patch('desktop_assistant.import_existing.psutil.process_iter', return_value=[]), \
            patch('desktop_assistant.import_existing.socket.socket', return_value=offline()), \
            patch('desktop_assistant.import_existing.shutil.copytree', side_effect=OSError('disk full')):
        with pytest.raises(OSError, match='disk full'):
            onboard(target, source, case.package, PASSWORD, case.nas)
    assert target.state['current'] is None
    assert not any((target.root / 'shared').iterdir())


def test_manual_pause_backups_then_stays_stopped_and_nightly_respects_it(example):
    case, source, target = example
    with patch.object(case.manager, '_process', return_value=True), \
            patch.object(case.manager, 'start') as start:
        case.manager.pause_after_backup(PASSWORD, case.nas)
        assert case.manager.state['manual_stop']
        start.assert_not_called()
        with patch('desktop_assistant.gui.preferences', side_effect=AssertionError('must skip')):
            nightly(case.manager)
    assert Path(case.manager.state['last_backup']).is_file()


def test_failed_pause_restores_running_service(example):
    case, source, target = example
    with patch.object(case.manager, '_process', return_value=True), \
            patch.object(case.manager, '_backup_stopped', side_effect=ValueError('backup failed')), \
            patch.object(case.manager, 'start') as start:
        with pytest.raises(ValueError, match='backup failed'):
            case.manager.pause_after_backup(PASSWORD, case.nas)
        start.assert_called_once()
    assert not case.manager.state.get('manual_stop')


def test_unverified_original_stop_script_never_runs(tmp_path):
    source, release = tmp_path / 'source', tmp_path / 'release'
    for root, text in [(source, 'unverified'), (release, 'verified')]:
        path = root / 'scripts/admin/release_erp.ps1'
        path.parent.mkdir(parents=True)
        path.write_text(text)
    with patch('desktop_assistant.onboarding.subprocess.run') as run:
        with pytest.raises(ValueError, match='签名安装包不同'):
            stop_original(source, release)
        run.assert_not_called()


def test_empty_installation_nightly_never_starts_a_database(example):
    case, source, target = example
    with patch.object(target, 'start') as start:
        nightly(target)
        start.assert_not_called()


def test_installer_rejects_original_erp_subdirectory(tmp_path):
    from desktop_assistant.installer import validate_install_directory
    source = tmp_path / 'source'
    (source / 'data').mkdir(parents=True)
    (source / '.env').write_text('test')
    (source / 'data/carton_erp.sqlite3').write_bytes(b'test')
    with pytest.raises(ValueError, match='分开安装'):
        validate_install_directory(source / 'TianmingERP')
    assert validate_install_directory(tmp_path / 'independent') == tmp_path / 'independent'


def test_external_config_is_rejected_before_stop(example):
    case, source, target = example
    (source / '.env').write_text('ERP_SECRET_KEY_FILE=../outside.key', encoding='utf8')
    with patch('desktop_assistant.onboarding.stop_original') as stop:
        with pytest.raises(ValueError, match='尚未停服'):
            onboard(target, source, case.package, PASSWORD, case.nas)
        stop.assert_not_called()

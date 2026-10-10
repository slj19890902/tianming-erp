import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from desktop_assistant.onboarding import onboard
from desktop_assistant.storage import sha
from tests.desktop_assistant.test_self_service import example, offline, PASSWORD, TestManager


def junction(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        link.symlink_to(target, target_is_directory=True)
        assert link.is_symlink()
        return
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-Command',
                    f"$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} | Out-Null"],
                   check=True, capture_output=True)
    assert link.is_junction()


def test_development_dependency_link_is_ignored_and_business_files_restore(example):
    case, source, target = example
    dependency = case.root / 'tool-dependencies'
    dependency.mkdir()
    (dependency / 'tool.js').write_text('not ERP data')
    link = source / 'data/work/report/node_modules'
    junction(link, dependency)
    (link.parent / 'report.txt').write_text('keep source work file')
    rehearsal = source / 'data/release_rehearsals/old.sqlite3'
    rehearsal.parent.mkdir()
    rehearsal.write_bytes(b'isolated rehearsal copy')
    original = sha(source / 'data/carton_erp.sqlite3')
    with patch('desktop_assistant.onboarding.stop_original'), \
            patch('desktop_assistant.import_existing.psutil.process_iter', return_value=[]), \
            patch('desktop_assistant.import_existing.socket.socket', return_value=offline()):
        assert '首次接入完成' in onboard(target, source, case.package, PASSWORD, case.nas)
    assert (link.is_junction() or link.is_symlink()) and (dependency / 'tool.js').is_file()
    assert sha(source / 'data/carton_erp.sqlite3') == original
    assert not (target.root / 'shared/data/work/report/node_modules').exists()
    assert not (target.root / 'shared/data/release_rehearsals').exists()
    assert rehearsal.read_bytes() == b'isolated rehearsal copy'
    restored = TestManager(case.root / 'restored-without-dependencies', case.public)
    restored.restore(Path(target.state['last_backup']), PASSWORD)
    assert sha(restored.root / 'shared/data/carton_erp.sqlite3') == original
    assert (restored.root / 'shared/data/drawing.pdf').read_bytes() == b'synthetic attachment'
    assert (restored.root / 'shared/data/work/report/report.txt').read_text() == 'keep source work file'


@pytest.mark.parametrize('relative', ['data/private_uploads', 'static/uploads',
                                      'factory_twin/data', 'static', 'data/work/linked-project',
                                      'data/private_uploads/node_modules'])
def test_business_links_are_identified_before_original_is_stopped(example, relative):
    case, source, target = example
    external = case.root / 'business-files'
    external.mkdir()
    junction(source / relative, external)
    with patch('desktop_assistant.onboarding.stop_original') as stop:
        with pytest.raises(ValueError, match=relative + '.*尚未停服'):
            onboard(target, source, case.package, PASSWORD, case.nas)
        stop.assert_not_called()
    assert target.state['current'] is None
    assert not any((target.root / 'shared').iterdir())


def test_registered_attachment_in_excluded_directory_blocks_before_stop(example):
    import sqlite3
    case, source, target = example
    attachment = source / 'data/release_rehearsals/business.pdf'
    attachment.parent.mkdir()
    attachment.write_bytes(b'must not silently omit a referenced attachment')
    with sqlite3.connect(source / 'data/carton_erp.sqlite3') as db:
        db.execute('CREATE TABLE product_drawings(id INTEGER PRIMARY KEY, image_path TEXT, thumbnail_path TEXT)')
        db.execute('INSERT INTO product_drawings VALUES(1,?,NULL)', (str(attachment),))
    db.close()
    with patch('desktop_assistant.onboarding.stop_original') as stop:
        with pytest.raises(ValueError, match='附件.*尚未停服'):
            onboard(target, source, case.package, PASSWORD, case.nas)
        stop.assert_not_called()


def test_configured_business_path_in_rehearsal_is_not_silently_omitted(example):
    case, source, target = example
    (source / '.env').write_text('ERP_PDF_DIR=data/release_rehearsals')
    with patch('desktop_assistant.onboarding.stop_original') as stop:
        with pytest.raises(ValueError, match='ERP_PDF_DIR.*尚未停服'):
            onboard(target, source, case.package, PASSWORD, case.nas)
        stop.assert_not_called()


def test_new_business_link_after_preflight_never_publishes_partial_copy(example):
    case, source, target = example
    external = case.root / 'changed-files'
    external.mkdir()
    def stop(*args):
        junction(source / 'data/private_uploads', external)
    with patch('desktop_assistant.onboarding.stop_original', side_effect=stop), \
            patch('desktop_assistant.import_existing.psutil.process_iter', return_value=[]), \
            patch('desktop_assistant.import_existing.socket.socket', return_value=offline()):
        with pytest.raises(ValueError, match='data/private_uploads'):
            onboard(target, source, case.package, PASSWORD, case.nas)
    assert target.state['current'] is None
    assert not any((target.root / 'shared').iterdir())
    assert not (source / 'data/runtime/erp_managed_installation.json').exists()


def test_dangling_file_link_is_rejected_before_stop(example):
    case, source, target = example
    (source / 'data/lost.pdf').symlink_to(case.root / 'missing.pdf')
    with patch('desktop_assistant.onboarding.stop_original') as stop:
        with pytest.raises(ValueError, match='data/lost.pdf.*尚未停服'):
            onboard(target, source, case.package, PASSWORD, case.nas)
        stop.assert_not_called()


def test_copy_callback_rechecks_links_created_after_scan(example):
    import shutil
    case, source, target = example
    external = case.root / 'changed-during-copy'
    external.mkdir()
    real_copy = shutil.copytree
    def changed_copy(src, dst, **kwargs):
        junction(source / 'data/private_uploads', external)
        return real_copy(src, dst, **kwargs)
    with patch('desktop_assistant.onboarding.stop_original'), \
            patch('desktop_assistant.import_existing.psutil.process_iter', return_value=[]), \
            patch('desktop_assistant.import_existing.socket.socket', return_value=offline()), \
            patch('desktop_assistant.import_existing.shutil.copytree', side_effect=changed_copy):
        with pytest.raises(ValueError, match='data/private_uploads'):
            onboard(target, source, case.package, PASSWORD, case.nas)
    assert target.state['current'] is None
    assert not any((target.root / 'shared').iterdir())

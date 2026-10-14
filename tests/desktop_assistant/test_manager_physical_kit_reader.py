"""Manager execution/backup uses signed readers, never cached cap claims."""
import json
import sqlite3
from pathlib import Path
import pytest
from desktop_assistant.manager import Manager
from desktop_assistant.storage import pack_tree,write_json,sha
from desktop_assistant.physical_kit_reader_contract import CAPABILITY,SOURCE
from tests.desktop_assistant.test_stopped_update import case


def mark(case):
    path=case.manager.root/'shared/data/carton_erp.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE inventory_lots(id INTEGER PRIMARY KEY,source_ref_type TEXT,cost_snapshot_source TEXT)')
        db.execute('INSERT INTO inventory_lots VALUES(1,?,NULL)',(SOURCE,))
    return path


def test_manager_compatibility_and_backup_reject_old_signed_package_on_real_marker(case):
    mark(case);identity=case.manager.state['current']
    assert not case.manager.compatible(identity,'r1')
    with pytest.raises(ValueError,match='读取能力'):case.manager._backup_inputs()


def test_cached_reader_claim_cannot_upgrade_old_signature(case):
    mark(case);identity=case.manager.state['current']
    path=case.manager.root/'releases'/identity/'manifest.json'
    value=json.loads(path.read_text());value['reader_capabilities']={CAPABILITY:1};write_json(path,value)
    with pytest.raises(ValueError,match='签名原包'):case.manager._check_physical_kit_reader(identity)


def test_actual_supported_signature_allows_facts_and_start_guard_runs_before_process(case,monkeypatch):
    mark(case)
    package=case.root/'signed-kit-reader.zip'
    pack_tree(case.root/'source-one',package,{'type':'tianming.release.v1','version':'reader','revision':'r1',
        'reader_capabilities':{CAPABILITY:1}},case.key)
    supported=case.manager.stage_release(package)
    case.manager._check_physical_kit_reader(supported['id'])
    monkeypatch.setattr(case.manager,'_runtime_python',lambda identity:Path('/synthetic/not-executed'))
    monkeypatch.setattr(case.manager,'_process',lambda:pytest.fail('unsupported start must fail before process adoption'))
    with pytest.raises(ValueError,match='读取能力'):Manager.start(case.manager)

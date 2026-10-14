"""Small offline Manager.restore integration for compact rotation evidence.

Uses the existing signed synthetic release and fake host credential store.
No NAS connection, service runtime, production path or real secret is used.
"""
import hashlib
import json
import zipfile

import pytest

from desktop_assistant import nas_backup_rotation as rotation
from desktop_assistant.storage import sha, write_json
from tests.desktop_assistant.test_credential_restore import recovery_case, host_store
from tests.desktop_assistant.test_credential_transfer import PASSWORD
from tests.desktop_assistant import test_recovery as recovery_fixtures


def restore_case(case, *, legacy_map=True):
    shared = case.manager.root / 'shared'
    if legacy_map:
        maps = shared / 'factory_twin_data'
        maps.mkdir(exist_ok=True)
        write_json(maps / 'layout.json', {'revision':'synthetic-map-v1','areas':[]})
    source_attachment = shared / 'data/drawing.pdf'
    original_attachment = sha(source_attachment)
    backup = case.manager.backup(PASSWORD, case.nas)
    target = recovery_fixtures.TestManager(case.root / 'rotation-restored', case.public)
    result = target.restore(backup, PASSWORD, expected_backup_sha256=sha(backup))
    assert result['started'] is False and result['credentials']['status'] == 'restored'
    raw_zips = list((target.root / 'staging').glob('*/recovery.zip'))
    assert len(raw_zips) == 1
    raw_zip = raw_zips[0]
    # Exercise the real rename performed by Manager.restore. A fabricated
    # payload tree would conceal the production path-lifetime regression.
    assert not (raw_zip.parent / 'payload/shared').exists()
    assert (target.root / 'shared').is_dir()
    assert sha(target.root / 'shared/data/drawing.pdf') == original_attachment
    with zipfile.ZipFile(raw_zip) as archive:
        raw_manifest = archive.read('manifest.json')
        manifest = json.loads(raw_manifest)
    receipt = dict(sha256=sha(backup), raw_sha256=sha(raw_zip),
        contract_sha256=hashlib.sha256(b'offline-synthetic-receipt-contract').hexdigest(),
        database=manifest['database'])
    receipt_sha = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    return target, receipt, receipt_sha, backup, raw_zip, raw_manifest


def assert_complete_report(case):
    target, receipt, receipt_sha, backup, raw_zip, raw_manifest = case
    report = rotation.capture_verified_recovery(target, receipt, receipt_sha, backup, raw_zip)
    assert rotation.validate_recovery(report, receipt, receipt_sha) == target.state['source_time']
    assert report['isolated_root'] == str(target.root)
    assert report['backup_sha256'] == sha(backup)
    assert report['raw_sha256'] == sha(raw_zip)
    assert report['programs']['release'] == target.state['current']
    assert report['credentials']['status'] == 'restored'
    for name in ('inventory','shared','maps'):
        summary = report[name]
        assert summary['source_count'] == summary['restored_count'] > 0
        assert summary['source_sha256'] == summary['restored_sha256']
    assert report['maps']['source_count'] == 1
    assert report['database']['source'] == report['database']['restored'] == receipt['database']
    encoded = json.dumps(report, sort_keys=True).encode()
    assert len(encoded) < 65536
    assert PASSWORD.encode() not in encoded and b'synthetic-secret' not in encoded
    return report


def test_actual_restore_moves_payload_shared_then_captures_valid_complete_report(recovery_case, host_store):
    assert_complete_report(restore_case(recovery_case))
    assert host_store  # Credentials were really rewrapped by the restore path.


def test_actual_restore_immutable_attachment_change_cannot_emit_valid_report(recovery_case, host_store):
    case = restore_case(recovery_case)
    assert_complete_report(case)
    target = case[0]
    (target.root / 'shared/data/drawing.pdf').write_bytes(b'tampered-after-real-restore')
    with pytest.raises(ValueError, match='shared.*digest/count coverage incomplete'):
        rotation.capture_verified_recovery(*case[:5])
    assert not (case[4].parent / 'payload/shared').exists()


def test_actual_restore_large_manifest_keeps_protected_report_compact(recovery_case, host_store):
    # A few thousand one-byte files make the authentic filename/hash inventory
    # exceed the protected record reader's 1MiB bound, without a large database.
    folder = recovery_case.manager.root / 'shared/documents'
    for index in range(6):
        folder /= f'fixture-{index}-' + 'x' * 80
    folder.mkdir(parents=True)
    for index in range(2048):
        (folder / f'tiny-{index:04d}.txt').write_bytes(b'x')
    case = restore_case(recovery_case)
    assert len(case[5]) > 1024 * 1024
    report = assert_complete_report(case)
    assert report['shared']['source_count'] >= 2050
    assert len(json.dumps(report, sort_keys=True).encode()) < 65536


CANONICAL_MAP_FILES=('data/layout_runtime/twin_layout_v1.json',
    'data/layout_drafts/twin_layout_v1.draft.json',
    'data/layout_backups/twin_layout_v1.previous.json')


def canonical_restore(case):
    for name in CANONICAL_MAP_FILES:
        path=case.manager.root/'shared'/name;path.parent.mkdir(parents=True,exist_ok=True)
        write_json(path,{'revision':'synthetic-canonical-map','areas':[]})
    return restore_case(case,legacy_map=False)


def test_canonical_maps_without_legacy_directory_have_exact_authenticated_coverage(recovery_case,host_store):
    case=canonical_restore(recovery_case)
    report=rotation.capture_verified_recovery(*case[:5])
    assert report['maps']['source_count']==report['maps']['restored_count']==3
    expected={'shared/'+name:sha(case[0].root/'shared'/name) for name in CANONICAL_MAP_FILES}
    assert report['maps']['source_sha256']==report['maps']['restored_sha256']==rotation.digest(expected)
    assert not (case[0].root/'shared/factory_twin_data').exists()
    assert rotation.validate_recovery(report,case[1],case[2])==case[0].state['source_time']


@pytest.mark.parametrize('name',CANONICAL_MAP_FILES)
def test_canonical_map_content_change_still_rejects_recovery(recovery_case,host_store,name):
    case=canonical_restore(recovery_case)
    (case[0].root/'shared'/name).write_bytes(b'changed canonical map')
    with pytest.raises(ValueError,match='digest/count coverage incomplete'):
        rotation.capture_verified_recovery(*case[:5])

import json

import pytest

from desktop_assistant.manager import Manager


def fixture_manager(tmp_path, monkeypatch, *, capabilities=None, activation=True):
    import desktop_assistant.manager as module
    manager = Manager(tmp_path, b'test')
    state = {'current': 'old', 'previous': 'older'}
    if activation:
        state['order_inventory_activation'] = {'reader_capability': 'order_inventory_v1', 'version': 1}
    (tmp_path / 'state.json').write_text(json.dumps(state))
    (tmp_path / 'packages/new.zip').write_bytes(b'package')
    manifest = {'revision': 'eg1008sc', 'reader_capabilities': capabilities or {}}
    monkeypatch.setattr(manager, 'manifest', lambda release=None: manifest)
    monkeypatch.setattr(module, 'sha', lambda path: 'new')
    monkeypatch.setattr(module, 'signed_release_manifest', lambda *args: manifest)
    return manager


@pytest.mark.parametrize('capabilities,expected', [({}, False), ({'order_inventory_v1': 1}, True),
    ({'order_inventory_v1': 2}, False), ({'order_inventory_v1': True}, False)])
def test_same_schema_still_checks_signed_reader(tmp_path, monkeypatch, capabilities, expected):
    manager = fixture_manager(tmp_path, monkeypatch, capabilities=capabilities)
    assert manager.compatible('new', 'eg1008sc') is expected


def test_missing_or_invalid_signature_and_mutable_manifest_fail_closed(tmp_path, monkeypatch):
    import desktop_assistant.manager as module
    manager = fixture_manager(tmp_path, monkeypatch, capabilities={'order_inventory_v1': 1})
    assert not manager.compatible('missing', 'eg1008sc')
    monkeypatch.setattr(module, 'signed_release_manifest', lambda *args: (_ for _ in ()).throw(ValueError('signature')))
    assert not manager.compatible('new', 'eg1008sc')
    monkeypatch.setattr(module, 'signed_release_manifest', lambda *args: {'revision': 'eg1008sc', 'reader_capabilities': {'order_inventory_v1': 1}, 'signed': True})
    assert not manager.compatible('new', 'eg1008sc')


def test_existing_installation_without_activation_preserves_old_behavior(tmp_path, monkeypatch):
    manager = fixture_manager(tmp_path, monkeypatch, activation=False)
    assert manager.compatible('new', 'eg1008sc')


def test_unknown_activation_is_not_downgraded_to_known_contract(tmp_path, monkeypatch):
    manager = fixture_manager(tmp_path, monkeypatch, capabilities={'order_inventory_v1': 1})
    state = manager.state
    state['order_inventory_activation']['version'] = 2
    (tmp_path / 'state.json').write_text(json.dumps(state))
    assert not manager.compatible('new', 'eg1008sc')


def test_activation_is_persisted_before_start_and_clears_unsafe_previous(tmp_path, monkeypatch):
    import desktop_assistant.manager as module
    import desktop_assistant.nas_probe as probe
    manager = fixture_manager(tmp_path, monkeypatch, activation=False)
    monkeypatch.setattr(manager, 'stage_release', lambda p: {'id': 'new', 'revision': 'eg1008sc', 'version': 'candidate'})
    monkeypatch.setattr(manager, '_order_inventory_reader', lambda release: release == 'new')
    monkeypatch.setattr(module, 'database_info', lambda p: {'revision': 'eg1008sc'})
    monkeypatch.setattr(probe, 'check_before_stop', lambda p: None)
    monkeypatch.setattr(manager, 'stop', lambda: None)
    monkeypatch.setattr(manager, '_backup_stopped', lambda *a: tmp_path / 'verified-backup')
    observed = []
    monkeypatch.setattr(manager, 'start', lambda: observed.append(manager.state))
    assert manager._update_locked(tmp_path / 'package', 'secret', tmp_path) == 'candidate'
    assert observed[0]['previous'] is None
    assert observed[0]['order_inventory_activation']['version'] == 1
    assert observed[0]['order_inventory_activation']['pre_activation_package'] == 'old'
    monkeypatch.setattr(module, 'write_json', lambda *args: (_ for _ in ()).throw(OSError('disk')))
    # Restore old state to exercise a failed activation write.
    (tmp_path / 'state.json').write_text(json.dumps({'current': 'old'}))
    observed.clear()
    with pytest.raises(OSError):
        manager._update_locked(tmp_path / 'package', 'secret', tmp_path)
    assert not observed

"""A real root backup pause must not disable factory ERP startup."""
import pytest
from unittest.mock import Mock
from desktop_assistant import mac_factory_runtime as runtime, mac_system_service as system

@pytest.fixture
def admission(monkeypatch, tmp_path):
    monkeypatch.setattr(system, 'BASE', tmp_path)
    monkeypatch.setattr(runtime.keychain, 'load_context', lambda: {'uid':499, 'gid':499})
    monkeypatch.setattr(system, 'check_start', Mock())
    monkeypatch.setattr(system, 'management_service_state', lambda: {
        'registered':True, 'enabled':True, 'scheduled_backups':False})
    records = {
        tmp_path/'automatic-backup-paused.json':runtime.boot.canonical({
            'type':'tianming.automatic-backup-pause.v1',
            'context_sha256':runtime.boot.digest(b'context'), 'paused':True}),
        runtime.keychain.CONTEXT:b'context',
        system.CONTROL:runtime.boot.canonical({'package_sha256':'a'*64}),
    }
    monkeypatch.setattr(system.publication, 'read_record', lambda path: records[path])
    monkeypatch.setattr(system, 'metadata', lambda package: {'package':package})
    manifest = {'runtime_capabilities':{'service_manage_v1':1,'system_nightly_v1':1},
                'files':{system.MANAGE_ENTRY:'a'*64,system.MANAGE_MODULE:'b'*64}}
    monkeypatch.setattr(system, '_program_manifest', lambda meta: manifest)
    history = Mock(); canary = Mock()
    monkeypatch.setattr(runtime, '_authenticate_current_mac_start', history)
    monkeypatch.setattr(system, 'verify_keychain', canary)
    return records, manifest, history, canary

def test_exact_pause_preserves_history_and_canary_checks(admission):
    _, _, history, canary = admission
    runtime.require_factory_service()
    history.assert_called_once(); canary.assert_called_once()

@pytest.mark.parametrize('fault', ['foreign_context','not_paused','extra_field','missing_protocol','not_registered','disabled'])
def test_bad_pause_or_service_never_admits(admission, monkeypatch, fault):
    records, manifest, history, canary = admission
    path = system.BASE/'automatic-backup-paused.json'
    pause = runtime.boot.decode(records[path])
    if fault=='foreign_context':pause['context_sha256']='f'*64
    elif fault=='not_paused':pause['paused']=False
    elif fault=='extra_field':pause['unexpected']=True
    elif fault=='missing_protocol':manifest['runtime_capabilities']['system_nightly_v1']=0
    elif fault=='not_registered':
        monkeypatch.setattr(system, 'management_service_state', lambda: {
            'registered':False,'enabled':True,'scheduled_backups':False})
    elif fault=='disabled':
        monkeypatch.setattr(system, 'management_service_state', lambda: {
            'registered':True,'enabled':False,'scheduled_backups':False})
    records[path]=runtime.boot.canonical(pause)
    with pytest.raises(ValueError):runtime.require_factory_service()
    history.assert_not_called(); canary.assert_not_called()

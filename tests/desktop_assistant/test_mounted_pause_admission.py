"""Root timer pause is preserved for mounted admission, never a service waiver."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest
from desktop_assistant import current_mac_target_audit as audit,mac_system_service as system
from desktop_assistant import current_mac_cutover_contract as c
from tests.desktop_assistant.test_owner_activation_install import scene,activate


@pytest.fixture
def paused(tmp_path,monkeypatch):
    monkeypatch.setattr(system,'BASE',tmp_path)
    path = tmp_path/'automatic-backup-paused.json'
    raw = c.canonical(dict(type='tianming.automatic-backup-pause.v1',context_sha256='a'*64,paused=True))
    path.write_bytes(raw)
    def protected(p):
        p = Path(p)
        if p.is_symlink():raise ValueError('synthetic protected root link rejected')
        return p.read_bytes()
    monkeypatch.setattr(system.publication,'read_record',protected)
    status = dict(registered=True,enabled=True,scheduled_backups=False)
    monkeypatch.setattr(system,'management_service_state',lambda:deepcopy(status))
    return SimpleNamespace(path=path,raw=raw,status=status)


def test_paused_mounted_admission_preserves_true_record(paused):
    assert audit._service_ready('a'*64,mounted=True) == paused.raw
    assert paused.path.read_bytes() == paused.raw and paused.status['scheduled_backups'] is False


def test_existing_smb_admission_still_requires_scheduled_backups(paused):
    with pytest.raises(ValueError):audit._service_ready('a'*64)
    assert paused.path.read_bytes() == paused.raw


def test_absent_pause_is_accepted_only_with_real_scheduled_timer(paused):
    paused.path.unlink();paused.status['scheduled_backups'] = True
    assert audit._service_ready('a'*64,mounted=True) is None
    paused.status['scheduled_backups'] = False
    with pytest.raises(ValueError):audit._service_ready('a'*64,mounted=True)


@pytest.mark.parametrize('fault',['unregistered','disabled','scheduled-true','false-pause','wrong-context','wrong-type','extra','symlink'])
def test_pause_never_waives_registration_identity_or_public_record(paused,fault):
    if fault == 'unregistered':paused.status['registered'] = False
    elif fault == 'disabled':paused.status['enabled'] = False
    elif fault == 'scheduled-true':paused.status['scheduled_backups'] = True
    elif fault == 'symlink':
        saved = paused.path.with_name('saved.json');paused.path.rename(saved);paused.path.symlink_to(saved)
    else:
        value = c.decode(paused.raw)
        if fault == 'false-pause':value['paused'] = False
        elif fault == 'wrong-context':value['context_sha256'] = '0'*64
        elif fault == 'wrong-type':value['type'] = 'other'
        else:value['extra'] = True
        paused.path.write_bytes(c.canonical(value))
    with pytest.raises(ValueError):audit._service_ready('a'*64,mounted=True)


def test_pause_change_during_service_observation_is_rejected(paused,monkeypatch):
    def changed():
        value = c.decode(paused.raw);value['context_sha256'] = '0'*64
        paused.path.write_bytes(c.canonical(value))
        return deepcopy(paused.status)
    monkeypatch.setattr(system,'management_service_state',changed)
    with pytest.raises(ValueError):audit._service_ready('a'*64,mounted=True)


def test_owner_publication_keeps_existing_pause_true(scene):
    path = scene.base/'automatic-backup-paused.json'
    raw = c.canonical(dict(type='tianming.automatic-backup-pause.v1',
        context_sha256=scene.prep['context_sha256'],paused=True))
    path.write_bytes(raw);path.chmod(0o644)
    assert activate(scene)['factory_activated'] is True
    assert path.read_bytes() == raw and scene.state_path.read_bytes() == scene.state_raw


@pytest.mark.parametrize('fault',['wrong-context','changed-during-audit','removed-during-audit'])
def test_owner_publication_binds_the_existing_pause_record(scene,fault):
    path = scene.base/'automatic-backup-paused.json'
    raw = c.canonical(dict(type='tianming.automatic-backup-pause.v1',
        context_sha256=scene.prep['context_sha256'],paused=True))
    path.write_bytes(raw);path.chmod(0o644)
    if fault == 'wrong-context':
        value = c.decode(raw);value['context_sha256'] = '0'*64;path.write_bytes(c.canonical(value))
    else:
        original = scene.worker.side_effect
        def changed(*args):
            result = original(*args)
            if fault == 'removed-during-audit':path.unlink()
            else:
                value = c.decode(raw);value['paused'] = False;path.write_bytes(c.canonical(value))
            return result
        scene.worker.side_effect = changed
    with pytest.raises((ValueError,OSError)):activate(scene)
    assert not (scene.base/'factory-activation.json').exists()
    assert scene.state_path.read_bytes() == scene.state_raw


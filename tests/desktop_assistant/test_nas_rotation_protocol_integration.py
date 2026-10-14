"""Archive source compatibility and new rotation-only signed execution boundary."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest
from desktop_assistant import mac_smb_stream_runtime_contract as cfg
from desktop_assistant import mac_smb_stream_runtime as runtime
from desktop_assistant import mac_smb_stream_setup as setup


def old_manifest():
    return {'runtime_capabilities':{cfg.CAPABILITY:1},'files':{name:'a'*64 for name in cfg.FILES}}


def test_real_old_archive_source_does_not_gain_rotation_or_fail_its_old_protocol():
    old=old_manifest();cfg.capability(old)
    assert not cfg.ROTATION_FILES & old['files'].keys()
    with pytest.raises(ValueError):cfg.rotation_capability(old)
    new=deepcopy(old);new['runtime_capabilities'][cfg.ROTATION_CAPABILITY]=1
    new['files'].update({name:'b'*64 for name in cfg.ROTATION_FILES})
    cfg.rotation_capability(new)
    for missing in cfg.ROTATION_FILES:
        changed=deepcopy(new);del changed['files'][missing]
        with pytest.raises(ValueError):cfg.rotation_capability(changed)
    new['runtime_capabilities'][cfg.ROTATION_CAPABILITY]=True
    with pytest.raises(ValueError):cfg.rotation_capability(new)


def test_upgrade_metadata_authenticates_old_and_complete_new_target(tmp_path,monkeypatch):
    folder=tmp_path/'published'/'synthetic-package'/'migration';folder.mkdir(parents=True)
    (folder.parent/'package.zip').write_bytes(b'signed-synthetic-placeholder')
    meta={'tree':folder};manifest=old_manifest()
    monkeypatch.setattr(setup.service,'metadata',lambda package:meta)
    monkeypatch.setattr(setup.publication,'inspect',lambda source:(None,manifest,None))
    assert setup.metadata('original-approved-stream')==meta
    manifest=deepcopy(manifest);manifest['runtime_capabilities'][cfg.ROTATION_CAPABILITY]=1
    with pytest.raises(ValueError):setup.metadata('incomplete-new-target')
    manifest['files'].update({name:'a'*64 for name in cfg.ROTATION_FILES})
    assert setup.metadata('complete-new-target')==meta


def test_retirement_request_cannot_supply_file_or_arbitrary_delete_path():
    binding=SimpleNamespace(control_sha='a'*64,control={'contract_sha256':'b'*64})
    request=dict(challenge='c'*32,control_sha256='a'*64,contract_sha256='b'*64,action='retire',socket_fd=100,attempt_id='d'*32)
    assert runtime._request(request,binding)==request
    for extra in ('filename','file_fd','path','job','index_sha256'):
        with pytest.raises(ValueError):runtime._request(dict(request,**{extra:'caller-selection'}),binding)
    runtime._observation({'status':'idle','removed_bytes':0},runtime.ROTATION_RESULT,request,binding)
    with pytest.raises(ValueError):runtime._observation({'status':'removed','removed_bytes':1,'filename':'../other','index_sha256':'e'*64},runtime.ROTATION_RESULT,request,binding)

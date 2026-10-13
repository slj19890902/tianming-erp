"""Real Mac signatures and schema AST with synthetic protected root authority."""
from copy import deepcopy
import json
from pathlib import Path
import zipfile

import pytest

from desktop_assistant import schema_contract as schema, storage, mac_publisher_trust as trust
from tests.desktop_assistant.test_mac_publisher_trust import scene
from tests.desktop_assistant.test_schema_contract import _sources


def package(s, *, declared=True, change=None):
    sources=_sources();tree=s.tmp/'schema-tree';tree.mkdir()
    for name,raw in sources.items():
        path=tree/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    contract=schema.schema_contract_from_sources(sources,'r1')
    metadata=dict(type='tianming.release.v1',version='schema-fixture',revision='r1')
    if declared:metadata['schema_contract']=deepcopy(contract)
    if change:change(metadata)
    archive=s.tmp/'schema-mac.zip';storage.pack_tree(tree,archive,metadata,s.mac)
    s.packages['mac']=archive;s.write(s.authorized())
    return archive,contract


@pytest.mark.parametrize('declared',[True,False])
def test_exact_authorized_mac_schema_uses_unified_trust(scene,declared):
    s=scene;archive,expected=package(s,declared=declared)
    manifest,contract=schema.schema_contract_from_signed_release(archive,trust.protected_read(trust.LEGACY_PUBLIC))
    assert contract==expected and manifest['revision']=='r1'
    assert ('schema_contract' in manifest)==declared


@pytest.mark.parametrize('fault',['unapproved','archive-changed','manifest-sha'])
def test_schema_never_bypasses_exact_archive_and_manifest_authority(scene,fault):
    s=scene;archive,_=package(s)
    if fault=='unapproved':s.write(s.empty)
    elif fault=='archive-changed':
        with archive.open('ab') as out:out.write(b'archive differs from authorized exact bytes')
    else:
        record=s.authorized();next(iter(record['packages'].values()))['manifest_sha256']='d'*64;s.write(record)
    with pytest.raises(ValueError):schema.schema_contract_from_signed_release(archive,trust.protected_read(trust.LEGACY_PUBLIC))


def test_schema_consumption_rechecks_root_authority_after_ast(scene,monkeypatch):
    s=scene;archive,_=package(s);original=schema.schema_contract_from_sources
    def changed(*args,**kwargs):
        result=original(*args,**kwargs);s.write(s.empty);return result
    monkeypatch.setattr(schema,'schema_contract_from_sources',changed)
    with pytest.raises(ValueError):schema.schema_contract_from_signed_release(archive,trust.protected_read(trust.LEGACY_PUBLIC))


def test_schema_noncanonical_columns_still_fail_closed(scene):
    s=scene
    archive,_=package(s,change=lambda value:value['schema_contract']['tables']['examples'].reverse())
    with pytest.raises(ValueError,match='表列'):
        schema.schema_contract_from_signed_release(archive,trust.protected_read(trust.LEGACY_PUBLIC))


def test_schema_model_file_hash_checks_remain_after_mac_signature(scene):
    s=scene;archive,_=package(s);changed=s.tmp/'tampered-schema.zip'
    with zipfile.ZipFile(archive) as source,zipfile.ZipFile(changed,'x') as output:
        for info in source.infolist():
            data=source.read(info.filename)
            if info.filename=='app/models/fixture.py':data+=b'# wrong consumed byte\n'
            output.writestr(info,data)
    s.packages['mac']=changed;s.write(s.authorized())
    with pytest.raises(ValueError,match='模型文件校验失败'):
        schema.schema_contract_from_signed_release(changed,trust.protected_read(trust.LEGACY_PUBLIC))

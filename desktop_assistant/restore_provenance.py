"""Keep authenticated original program bytes through later backups/restores."""
from pathlib import Path
import re

from desktop_assistant.release_request import inspect_package
from desktop_assistant.reader_contract import capabilities
from desktop_assistant.storage import sha

CAPABILITY = 'restore_provenance_v1'
RELEASE = 'restored_release_sha256'
AUTHORITY = 'restored_schema_authority_sha256'
STATE_KEYS = (RELEASE, AUTHORITY)
MEMBERS = {RELEASE:'restored-release.zip', AUTHORITY:'restored-schema-authority.zip'}


def supported(manifest):
    try:
        return capabilities(manifest).get(CAPABILITY) == 1
    except ValueError:
        return False


def identities(value):
    if not any(key in value for key in STATE_KEYS): return {}
    if (not all(key in value for key in STATE_KEYS) or not isinstance(value[RELEASE], str)
            or not re.fullmatch('[0-9a-f]{64}', value[RELEASE])
            or (value[AUTHORITY] is not None and (not isinstance(value[AUTHORITY], str)
                or not re.fullmatch('[0-9a-f]{64}', value[AUTHORITY])))):
        raise ValueError('恢复来源身份声明不完整或无效')
    return {key:value[key] for key in STATE_KEYS}


def verify(values, paths, public):
    manifests = {}
    for key, identity in values.items():
        if identity is None: continue
        path = paths[MEMBERS[key]]
        if sha(path) != identity: raise ValueError('恢复来源原包摘要不符')
        _, manifests[key] = inspect_package(path, public)
        if sha(path) != identity: raise ValueError('恢复来源原包在核验期间变化')
    if values.get(AUTHORITY) is not None:
        contract = manifests[AUTHORITY].get('migration') or {}
        if (contract.get('policy') != 'preserve_existing_facts_v1'
                or contract.get('rollback_package_sha256') != values[RELEASE]):
            raise ValueError('历史结构授权没有绑定原恢复程序')


def for_backup(root, state, current_manifest, public):
    values = identities(state)
    if values and not supported(current_manifest):
        raise ValueError('当前程序不能完整保留恢复来源，未创建备份')
    paths = {MEMBERS[key]:Path(root)/'packages'/(value+'.zip')
             for key, value in values.items() if value is not None}
    verify(values, paths, public)
    return values, paths


def for_recovery(payload, metadata, current_manifest, public):
    values = identities(metadata)
    expected = {MEMBERS[key] for key, value in values.items() if value is not None}
    present = set(MEMBERS.values()) & metadata['files'].keys()
    if present != expected or (values and not supported(current_manifest)):
        raise ValueError('恢复来源声明、签名能力与原包成员不一致')
    paths = {name:Path(payload)/name for name in expected}
    verify(values, paths, public)
    return values, paths

"""Carry the persistent order/inventory reader boundary through full recovery.

The historical activation record is retained verbatim when available. Older
backups did not include it; their authenticated release can still establish
the reader requirement without inventing the lost activation history.
"""
from copy import deepcopy
import re


CAPABILITY = 'order_inventory_v1'
STATE_KEY = 'order_inventory_activation'
ERROR = '订单库存 reader 保护记录不完整或与签名程序不一致，未启用恢复数据'


def signed_capability(manifest):
    capabilities = manifest.get('reader_capabilities', {})
    if (not isinstance(capabilities, dict)
            or (CAPABILITY in capabilities
                and (type(capabilities[CAPABILITY]) is not int or capabilities[CAPABILITY] != 1))):
        raise ValueError(ERROR)
    value = capabilities.get(CAPABILITY) if isinstance(capabilities, dict) else None
    return type(value) is int and value == 1


def validate(value):
    # Compatibility historically accepts these two mandatory fields. Preserve
    # all extra provenance exactly; it may name historical Windows paths.
    if (not isinstance(value, dict) or value.get('reader_capability') != CAPABILITY
            or type(value.get('version')) is not int or value['version'] != 1
            or ('next_release_must_preserve_reader_contract' in value
                and value['next_release_must_preserve_reader_contract'] is not True)):
        raise ValueError(ERROR)
    return value


def for_backup(state, authenticated_manifest):
    capable = signed_capability(authenticated_manifest)
    value = state.get(STATE_KEY)
    if value is None:
        return None
    validate(value)
    if not capable:
        raise ValueError(ERROR)
    return deepcopy(value)


def for_recovery(metadata, authenticated_manifest, backup_sha256):
    capable = signed_capability(authenticated_manifest)
    value = metadata.get(STATE_KEY)
    if value is not None:
        validate(value)
        if not capable:
            raise ValueError(ERROR)
        return deepcopy(value)
    if not capable:
        return None
    if not isinstance(backup_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', backup_sha256):
        raise ValueError(ERROR)
    # This is evidence for preserving the gate, not an invented activation
    # date/package lineage. A later complete backup preserves this marker too.
    return {'reader_capability': CAPABILITY, 'version': 1,
            'next_release_must_preserve_reader_contract': True,
            'recovery_evidence': {'kind': 'authenticated_backup_signed_reader_v1',
                                  'backup_sha256': backup_sha256,
                                  'original_activation_record_included': False}}

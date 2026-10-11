"""Public, root-approved SMB runtime binding. No credential value is a field."""
import hashlib
import json
from pathlib import Path
import re

from desktop_assistant import mac_smb_contract as cfg

BASE = Path('/Library/TianmingERP')
CONTROL = BASE/'smb-service.json'
CONTRACT = BASE/'archive-contract.json'
TYPE = 'tianming.mac-smb-service.v1'
CAPABILITY = 'archive_smb_v1'
ENTRY = 'desktop_assistant/service_smb_entry.py'
MODULE = 'desktop_assistant/mac_smb_runtime.py'
FILES = {ENTRY, MODULE, 'desktop_assistant/mac_smb_runtime_contract.py',
         'desktop_assistant/mac_smb_client.py', 'desktop_assistant/mac_smb_contract.py',
         'desktop_assistant/mac_smb_mount.py', 'desktop_assistant/mac_smb_storage.py',
         'desktop_assistant/mac_backup_storage.py'}
FIELDS = {'type','attempt_id','plan_sha256','package_sha256','context_sha256',
          'publisher_sha256','contract_sha256','credential_account'}
ERROR = '专用SMB后台连接未确认；保留现场，未允许备份写入'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(value): return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
def digest(value): return hashlib.sha256(value).hexdigest()


def account(context_sha, contract_sha, attempt):
    # Separate namespace from mailbox/AI/backup encryption references. This
    # account is host-specific and never part of the Windows credential bundle.
    return 'nas-smb-v1:'+digest(canonical({'context':context_sha, 'contract':contract_sha, 'attempt':attempt}))


def validate(value, contract, context_sha, uid):
    cfg.validate(contract, context_sha, uid)
    if (not isinstance(value, dict) or set(value) != FIELDS or value['type'] != TYPE
            or any(not sha(value[k]) for k in FIELDS if k.endswith('_sha256'))
            or value['context_sha256'] != context_sha
            or value['contract_sha256'] != digest(canonical(contract))
            or not isinstance(value['attempt_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['attempt_id'])
            or value['credential_account'] != account(context_sha, value['contract_sha256'], value['attempt_id'])):
        raise ValueError(ERROR)
    return value


def capability(manifest):
    cap = manifest.get('runtime_capabilities', {}).get(CAPABILITY)
    if type(cap) is not int or cap != 1 or not FILES <= manifest.get('files', {}).keys():
        raise ValueError('已批准程序缺少SMB后台连接协议')


def profile(contract, context_sha, uid):
    cfg.validate(contract, context_sha, uid)
    # Requested boundary, NOT evidence that the OS accepts this syntax. Current
    # macOS rejects exact non-loopback addresses; verify_network_policy blocks
    # worker dispatch on that result. Never substitute a wildcard fallback.
    # Interpolation is canonical RFC1918 IPv4, not names or credentials.
    return ('(version 1)(allow default)(deny network-outbound)'
            '(allow network-outbound (remote ip "'+contract['server_address']+':445"))')

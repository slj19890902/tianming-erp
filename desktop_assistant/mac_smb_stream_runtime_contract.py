"""Distinct root approval for stream workers; never a mounted-NAS proof."""
import hashlib
import re
from pathlib import Path

from . import mac_smb_stream_contract as archive

BASE = Path('/Library/TianmingERP')
CONTROL = BASE/'smb-stream-service.json'
CONTRACT = BASE/'smb-stream-archive.json'
TYPE = 'tianming.mac-smb-stream-service.v1'
CAPABILITY = 'archive_smb_stream_v1'
ENTRY = 'desktop_assistant/service_smb_stream_entry.py'
MODULE = 'desktop_assistant/mac_smb_stream_runtime.py'
FILES = {ENTRY, MODULE, 'desktop_assistant/mac_smb_stream_runtime_contract.py',
         'desktop_assistant/mac_smb_stream_contract.py',
         'desktop_assistant/mac_smb_stream_transport.py',
         'desktop_assistant/mac_smb_stream_session.py',
         'desktop_assistant/mac_smb_stream_files.py',
         'desktop_assistant/mac_smb_stream_backup.py'}
FIELDS = {'type','attempt_id','plan_sha256','package_sha256','context_sha256',
          'publisher_sha256','contract_sha256','credential_account'}
PROFILE = '(version 1)(allow default)(deny network-outbound)'
ERROR = 'SMB流后台传输未确认；保留现场，不覆盖或自动重试'
canonical = archive.canonical


def sha(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def digest(raw): return hashlib.sha256(raw).hexdigest()


def account(context, contract, attempt):
    return 'nas-smb-stream-v1:'+digest(canonical(dict(context=context,contract=contract,attempt=attempt)))


def validate(value, contract, context_sha, uid):
    archive.validate(contract,context_sha,uid)
    if (not isinstance(value,dict) or set(value)!=FIELDS or value['type']!=TYPE
            or any(not sha(value[k]) for k in FIELDS if k.endswith('_sha256'))
            or value['context_sha256']!=context_sha
            or value['contract_sha256']!=archive.digest(contract)
            or not isinstance(value['attempt_id'],str)
            or not re.fullmatch('[0-9a-f]{32}',value['attempt_id'])
            or value['credential_account']!=account(context_sha,value['contract_sha256'],value['attempt_id'])):
        raise ValueError(ERROR)
    return value


def capability(manifest):
    caps=manifest.get('runtime_capabilities',{})
    if (type(caps.get(CAPABILITY)) is not int or caps[CAPABILITY]!=1
            or not FILES <= manifest.get('files',{}).keys()):
        raise ValueError(ERROR)

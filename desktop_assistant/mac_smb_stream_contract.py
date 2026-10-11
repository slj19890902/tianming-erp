"""SMB stream archive identity; does not claim an installed or mounted NAS."""
import hashlib
import ipaddress
import json
import re
import unicodedata

TYPE = 'tianming.mac-smb-stream-archive.v1'
DIRECTORY = 'BoxERP/mac-server-backups'
FIELDS = {'type','context_sha256','uid','server_address','port','username','share',
          'relative_directory','server_guid','protection'}
PRIVATE = tuple(ipaddress.IPv4Network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
ERROR = 'SMB流归档合同无效；未允许连接或备份'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value): return hashlib.sha256(canonical(value)).hexdigest()


def name(value, limit):
    # Unicode is preserved exactly. Unlike the native statshares backend, this
    # protocol has no lossy shell parser. Reject path/realm ambiguity and aliases.
    return (isinstance(value, str) and 0 < len(value.encode('utf-8')) <= limit
        and unicodedata.normalize('NFC', value) == value and value == value.strip()
        and not value.endswith('.') and value not in ('.','..')
        and not any(ord(c)<32 or ord(c)==127 or c in '\\/:@<>"|?*' for c in value)
        and value.casefold() not in {'guest','anonymous','nobody','ipc$'})


def validate(value, context_sha, uid):
    try:
        valid = (isinstance(value, dict) and set(value) == FIELDS and value['type'] == TYPE
            and isinstance(context_sha,str) and re.fullmatch('[0-9a-f]{64}',context_sha)
            and value['context_sha256'] == context_sha and type(uid) is int and 400 <= uid < 500
            and type(value['uid']) is int and value['uid'] == uid
            and type(value['port']) is int and value['port'] == 445
            and name(value['username'],128) and name(value['share'],240)
            and value['relative_directory'] == DIRECTORY
            and value['protection'] == 'smb311-session-aes-gcm'
            and isinstance(value['server_guid'],str) and re.fullmatch('[0-9a-f]{32}',value['server_guid'])
            and value['server_guid'] != '0'*32 and isinstance(value['server_address'],str))
        if not valid: raise ValueError(ERROR)
        address=ipaddress.IPv4Address(value['server_address'])
        if str(address)!=value['server_address'] or not any(address in net for net in PRIVATE):
            raise ValueError(ERROR)
    except (TypeError, UnicodeError): raise ValueError(ERROR) from None
    return value


def filename(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.tmbackup',value):
        raise ValueError(ERROR)
    return value


def remote_path(value, basename):
    if not isinstance(value,dict): raise ValueError(ERROR)
    validate(value,value.get('context_sha256'),value.get('uid'))
    return DIRECTORY.replace('/', '\\')+'\\'+filename(basename)

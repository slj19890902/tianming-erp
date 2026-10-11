"""Public SMB archive configuration and active-connection checks.

This is not an installed contract or backup receipt. The existing NFS consumer
does not accept this type. Passwords and personal-session fallback have no field.
"""
import ipaddress
from pathlib import Path
import re

TYPE = 'tianming.mac-smb-archive.v1'
OBSERVATION = 'tianming.mac-smb-observation.v1'
MOUNT = Path('/System/Volumes/Data/Library/TianmingERP/archive')
RELATIVE = 'BoxERP/mac-server-backups'
FIELDS = {'type','context_sha256','uid','mountpoint','destination','server_address',
          'port','username','share','relative_directory','protection'}
ERROR = 'SMB归档身份、连接或实际加密状态未确认'
PRIVATE = tuple(ipaddress.IPv4Network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
DIALECTS = {'SMB_3.0','SMB_3.0.2','SMB_3.1.1'}
CIPHERS = {'AES-128-CCM','AES-128-GCM','AES-256-CCM','AES-256-GCM'}


def _name(value, limit):
    # Current Apple's statshares cannot reliably decode a Chinese share name.
    # Do not approve one through a lossy parser or the research-only private ABI.
    return (isinstance(value, str) and 0 < len(value) <= limit
            and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*', value) is not None
            and value.casefold() not in {'guest','anonymous','nobody','ipc$'})


def validate(value, context_sha, uid):
    if (not isinstance(value, dict) or set(value) != FIELDS or value['type'] != TYPE
            or not isinstance(context_sha, str) or re.fullmatch('[0-9a-f]{64}', context_sha) is None
            or value['context_sha256'] != context_sha
            or type(uid) is not int or not 0 < uid < 500 or type(value['uid']) is not int or value['uid'] != uid
            or value['mountpoint'] != str(MOUNT) or value['relative_directory'] != RELATIVE
            or value['destination'] != str(MOUNT/RELATIVE)
            or type(value['port']) is not int or value['port'] != 445
            or value['protection'] != 'share-encryption'
            or not isinstance(value['server_address'], str)
            or not _name(value['username'], 128) or not _name(value['share'], 80)):
        raise ValueError(ERROR)
    address = ipaddress.IPv4Address(value['server_address'])
    if (str(address) != value['server_address']
            or not any(address in network for network in PRIVATE)):
        raise ValueError(ERROR)
    return value


def create(context_sha, uid, address, username, share):
    value = {'type':TYPE,'context_sha256':context_sha,'uid':uid,'mountpoint':str(MOUNT),
        'destination':str(MOUNT/RELATIVE),'relative_directory':RELATIVE,'server_address':address,
        'port':445,'username':username,'share':share,'protection':'share-encryption'}
    return validate(value, context_sha, uid)


def source(value):
    return '//'+value['username']+'@'+value['server_address']+'/'+value['share']


def kernel_matches(value, kernel):
    if (not isinstance(kernel, dict)
            or set(kernel) != {'fsid','filesystem','mountpoint','source','owner','flags','subtype'}
            or any(type(kernel[k]) is not int or not 0 <= kernel[k] <= 0xffffffff for k in ('owner','flags','subtype'))
            or not isinstance(kernel['fsid'], list) or len(kernel['fsid']) != 2
            or any(type(x) is not int or not -(2**31) <= x < 2**31 for x in kernel['fsid'])
            or kernel['fsid'] == [0,0]):
        return False
    return (kernel['filesystem'] == 'smbfs' and kernel['mountpoint'] == value['mountpoint']
        and kernel['source'] == source(value) and kernel['owner'] == value['uid']
        and not kernel['flags'] & (0x1000 | 0x1 | 0x40) and kernel['flags'] & 0x18 == 0x18)


def require_observation(value, observation):
    if (not isinstance(observation, dict) or set(observation) != {'type','kernel','session','share'}
            or observation['type'] != OBSERVATION or not kernel_matches(value, observation['kernel'])):
        raise ValueError(ERROR)
    session, share = observation['session'], observation['share']
    if (not isinstance(session, dict) or set(session) != {'address','port','uuid','auth_type','username'}
            or session['address'] != value['server_address'] or type(session['port']) is not int or session['port'] != 445
            or session['auth_type'] != 'user' or session['username'] != value['username']
            or not isinstance(session['uuid'], str) or re.fullmatch('[0-9a-f]{32}',session['uuid']) is None
            or session['uuid'] == '0'*32
            or not isinstance(share, dict)
            or set(share) != {'name','server_name','uid','dialect','cipher','encrypted','signing','reconnect_count'}
            or share['name'] != value['share'] or type(share['uid']) is not int or share['uid'] != value['uid']
            or not isinstance(share['server_name'], str) or not 0 < len(share['server_name'].encode('utf-8')) <= 255
            or any(ord(c) < 32 or ord(c) == 127 for c in share['server_name'])
            or not isinstance(share['dialect'], str) or share['dialect'] not in DIALECTS
            or not isinstance(share['cipher'], str) or share['cipher'] not in CIPHERS
            or share['encrypted'] is not True or type(share['signing']) is not bool
            or type(share['reconnect_count']) is not int or not 0 <= share['reconnect_count'] <= 0xffffffff):
        raise ValueError(ERROR)
    return observation

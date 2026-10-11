"""Explicit negative examples for the independent SMB archive contract."""
from copy import deepcopy
import pytest
from desktop_assistant import mac_smb_contract as c

CONTEXT='c'*64

def value():return c.create(CONTEXT,499,'192.168.1.245','erp_admin','erp_archive')

def kernel():
    return dict(fsid=[-167,30],filesystem='smbfs',mountpoint=str(c.MOUNT),
        source='//erp_admin@192.168.1.245/erp_archive',owner=499,flags=0x300018,subtype=6)

def observation():
    return dict(type=c.OBSERVATION,kernel=kernel(),session=dict(address='192.168.1.245',port=445,
        uuid='a'*32,auth_type='user',username='erp_admin'),share=dict(name='erp_archive',server_name='NAS',
        uid=499,dialect='SMB_3.1.1',cipher='AES-128-GCM',encrypted=True,signing=False,reconnect_count=0))

def test_bound_encrypted_smb3_and_private_service_identity():
    v=value();o=observation()
    assert c.validate(v,CONTEXT,499)==v and c.kernel_matches(v,o['kernel'])
    assert c.require_observation(v,o)==o
    # SMB3 AEAD supplies integrity when separate signing is not active.
    assert o['share']['signing'] is False

@pytest.mark.parametrize('key,bad',[
    ('uid',0),('uid',501),('uid',True),('context_sha256','d'*64),('type','tianming.mac-autofs-archive.v1'),
    ('mountpoint','/Volumes/Archive'),('destination',str(c.MOUNT/'other')),
    ('relative_directory','../backups'),('port',139),('port',True),('protection','available'),
    ('server_address','127.0.0.1'),('server_address','0.0.0.0'),('server_address','169.254.1.2'),
    ('server_address','224.0.0.1'),('server_address','8.8.8.8'),('server_address','::1'),
    ('server_address','192.168.001.245'),('server_address','nas.local'),('server_address',3232236021),
    ('server_address',None),('server_address',[]),('username','guest'),('username','anonymous'),
    ('username','root:password'),('username','a@b'),('username','a/b'),('username','a\n'),
    ('username','a'*129),('share','共享文件'),('share','..'),('share','a'*81),('share','a%2Fb'),
    ('share','IPC$'),('share','a b'),('share',''),('share',[]),
])
def test_contract_cannot_select_proxy_other_path_credentials_or_unverifiable_share(key,bad):
    v=value();v[key]=bad
    with pytest.raises(ValueError):c.validate(v,CONTEXT,499)

@pytest.mark.parametrize('change',['extra_password','missing','bool_uid','ordinary','root','wrong_context'])
def test_only_complete_exact_public_contract(change):
    v=value();uid=499;context=CONTEXT
    if change=='extra_password':v['password']='SYNTHETIC'
    elif change=='missing':del v['port']
    elif change=='bool_uid':uid=True
    elif change=='ordinary':uid=501
    elif change=='root':uid=0
    else:context='incorrect'
    with pytest.raises(ValueError):c.validate(v,context,uid)

@pytest.mark.parametrize('key,bad',[
    ('filesystem','apfs'),('filesystem','nfs'),('owner',501),('owner',True),('owner',-1),
    ('mountpoint','/Volumes/user'),('source','//guest@192.168.1.245/erp_archive'),
    ('source','//erp_admin@127.0.0.1/erp_archive'),('source','//erp_admin:secret@192.168.1.245/erp_archive'),
    ('flags',0x300019),('flags',0x300058),('flags',0x301018),('flags',0x300008),('flags',0x300010),
    ('flags',True),('flags',2**32+0x18),('subtype',-1),('fsid',[0,0]),('fsid',[True,30]),('fsid',[2**32,30]),
])
def test_foreign_readonly_unmounted_or_unprotected_kernel_rejected(key,bad):
    o=observation();o['kernel'][key]=bad
    assert not c.kernel_matches(value(),o['kernel'])
    with pytest.raises(ValueError):c.require_observation(value(),o)

@pytest.mark.parametrize('section,key,bad',[
    ('session','auth_type','guest'),('session','auth_type','anonymous'),('session','auth_type','kerberos'),
    ('session','username','other'),('session','address','192.168.1.246'),('session','port',139),
    ('session','uuid','0'*32),('session','uuid','A'*32),('session','uuid',None),
    ('share','name','other'),('share','uid',501),('share','uid',True),
    ('share','dialect','SMB_2.1'),('share','dialect',[]),('share','cipher',[]),('share','cipher',None),
    ('share','encrypted',False),('share','encrypted',1),('share','signing',1),
    ('share','server_name',''),('share','server_name','NAS\n'),('share','server_name','x'*256),
    ('share','reconnect_count',-1),('share','reconnect_count',True),('share','reconnect_count',2**32),
])
def test_actual_peer_and_share_crypto_must_match(section,key,bad):
    o=observation();o[section][key]=bad
    with pytest.raises(ValueError):c.require_observation(value(),o)

def test_negotiated_cipher_and_server_capabilities_do_not_prove_active_encryption():
    o=observation();o['share'].update(encrypted=False,signing=True)
    with pytest.raises(ValueError):c.require_observation(value(),o)

@pytest.mark.parametrize('section',[None,'kernel','session','share'])
def test_extra_observation_fields_do_not_form_approval(section):
    o=observation();target=o if section is None else o[section];target['unexpected']=True
    with pytest.raises(ValueError):c.require_observation(value(),o)

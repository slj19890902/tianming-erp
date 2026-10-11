"""Public network proposal tests. No sockets, real credentials or configuration."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from desktop_assistant import factory_cutover as cutover, factory_network as network
from desktop_assistant import factory_target_contract as contract
from desktop_assistant.signing import public_bytes


def lan():
    return {'ERP_HOME_REHEARSAL':'0','ERP_PRODUCTION_TRANSPORT':'lan_http',
        'ERP_BIND_HOST':'0.0.0.0','ERP_PORT':'8000',
        'ERP_ALLOWED_ORIGINS':'http://192.168.3.80:8000', 'ERP_TRUSTED_HOSTS':'192.168.3.80',
        'ERP_TRUSTED_PROXY_IPS':'','ERP_SESSION_COOKIE_SECURE':'false',
        'ERP_BROWSER_URL':'http://192.168.3.80:8000/','ERP_HEALTH_URL':'http://192.168.3.80:8000/api/health',
        'ERP_LAN_HTTP_ORIGIN':'','ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS':'',
        'ERP_SHELF_LABEL_ORIGIN':'http://192.168.3.80:8000','ERP_MOBILE_QR_ORIGIN':''}


def https():
    value=lan();value.update(ERP_PRODUCTION_TRANSPORT='https_proxy',ERP_BIND_HOST='127.0.0.1',
        ERP_ALLOWED_ORIGINS='https://erp.example.test',ERP_TRUSTED_HOSTS='erp.example.test,192.168.3.80,192.168.3.81',
        ERP_TRUSTED_PROXY_IPS='127.0.0.1,::1',ERP_SESSION_COOKIE_SECURE='true',
        ERP_BROWSER_URL='https://erp.example.test/',ERP_HEALTH_URL='https://erp.example.test/api/health',
        ERP_LAN_HTTP_ORIGIN='http://192.168.3.80:8000',ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS='http://192.168.3.81:8000',
        ERP_SHELF_LABEL_ORIGIN='http://192.168.3.80:8000')
    return value


def test_network_request_rejects_unvalidated_environment():
    with pytest.raises(ValueError):cutover.make_network_request({}, 'a'*64, {'ERP_HOME_REHEARSAL':'0'}, b'not a key')


@pytest.mark.parametrize('configuration',[lan,https])
def test_proposal_is_compatible_with_current_application_settings_without_io(monkeypatch,tmp_path,configuration):
    value=configuration();assert network.validate(value)==value
    from app.core import config
    for key in list(config.os.environ):
        if key.startswith('ERP_'):monkeypatch.delenv(key,raising=False)
    for key,val in value.items():monkeypatch.setenv(key,val)
    monkeypatch.setenv('ERP_ENVIRONMENT','production');monkeypatch.setenv('ERP_SECRET_KEY','synthetic-only-session-key-'*3)
    monkeypatch.setenv('ERP_DATABASE_PATH',str(tmp_path/'unused.sqlite'))
    monkeypatch.setenv('ERP_BACKUP_DIR',str(tmp_path/'unused-backups'))
    monkeypatch.setenv('ERP_SECRET_KEY_FILE',str(tmp_path/'not-created'))
    result=config.load_settings()
    assert result.production_transport==value['ERP_PRODUCTION_TRANSPORT']
    assert result.bind_host==value['ERP_BIND_HOST'] and result.port==8000
    assert result.browser_url==value['ERP_BROWSER_URL'] and result.health_url==value['ERP_HEALTH_URL']
    assert result.allowed_origins==tuple(value['ERP_ALLOWED_ORIGINS'].split(','))+result.private_http_origins
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('key,bad',[
 ('ERP_HOME_REHEARSAL','1'),('ERP_HOME_REHEARSAL',False),('ERP_HOME_REHEARSAL','0\n'),
 ('ERP_PRODUCTION_TRANSPORT','http'),('ERP_BIND_HOST','8.8.8.8'),('ERP_BIND_HOST','localhost'),
 ('ERP_BIND_HOST','127.0.0.1'),('ERP_BIND_HOST','192.168.3.79'),('ERP_PORT','08000'),('ERP_PORT','443'),('ERP_PORT','65536'),
 ('ERP_ALLOWED_ORIGINS','http://8.8.8.8:8000'),('ERP_ALLOWED_ORIGINS','http://169.254.3.80:8000'),
 ('ERP_ALLOWED_ORIGINS','http://192.168.3.80:8000/'),('ERP_ALLOWED_ORIGINS','http://192.168.3.80:8001'),
 ('ERP_ALLOWED_ORIGINS','http://x:y@192.168.3.80:8000'),('ERP_ALLOWED_ORIGINS','http://192.168.3.80:8000?secret=x'),
 ('ERP_ALLOWED_ORIGINS','http://192.168.3.80:8000#secret'),('ERP_ALLOWED_ORIGINS','http://192.168.3.80:8000, http://192.168.3.81:8000'),
 ('ERP_ALLOWED_ORIGINS','http://192.168.3.80:8000,http://192.168.3.80:8000'),('ERP_TRUSTED_HOSTS','*'),
 ('ERP_TRUSTED_HOSTS','192.168.3.81'),('ERP_TRUSTED_PROXY_IPS','127.0.0.1'),('ERP_SESSION_COOKIE_SECURE','true'),
 ('ERP_BROWSER_URL','http://192.168.3.80:8000'),('ERP_HEALTH_URL','http://192.168.3.80:8000/health'),
 ('ERP_BROWSER_URL','http://192.168.3.81:8000/'),('ERP_SHELF_LABEL_ORIGIN','https://unknown.test'),
 ('ERP_LAN_HTTP_ORIGIN','http://192.168.3.80:8000'),('ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS','http://192.168.3.81:8000'),
 ('ERP_MOBILE_QR_ORIGIN','https://unknown.test'),
])
def test_invalid_direct_network_refuses_without_normalization(key,bad):
    value=lan();value[key]=bad
    with pytest.raises(ValueError):network.validate(value)


@pytest.mark.parametrize('key,bad',[
 ('ERP_BIND_HOST','0.0.0.0'),('ERP_TRUSTED_PROXY_IPS',''),('ERP_TRUSTED_PROXY_IPS','192.168.3.1'),
 ('ERP_SESSION_COOKIE_SECURE','false'),('ERP_ALLOWED_ORIGINS','https://*.example.test'),
 ('ERP_ALLOWED_ORIGINS','https://erp.example.test:0'),('ERP_ALLOWED_ORIGINS','https://erp.example.test:0443'),
 ('ERP_ALLOWED_ORIGINS','https://erp.example.test.'),('ERP_ALLOWED_ORIGINS','https://ERP.EXAMPLE.TEST'),
 ('ERP_ALLOWED_ORIGINS','https://0127.0.0.1'),('ERP_ALLOWED_ORIGINS','https://erp.example.test\t'),
 ('ERP_HEALTH_URL','https://other.example.test/api/health'),('ERP_LAN_HTTP_ORIGIN','http://8.8.8.8:8000'),
 ('ERP_LAN_HTTP_ORIGIN','http://192.168.3.80'),('ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS','http://192.168.3.80:8000'),
 ('ERP_SHELF_LABEL_ORIGIN','https://erp.example.test'),
])
def test_invalid_https_configuration_refuses(key,bad):
    value=https();value[key]=bad
    with pytest.raises(ValueError):network.validate(value)


@pytest.mark.parametrize('fault',['missing','extra-secret','list','too-long'])
def test_only_complete_public_fields_are_accepted(fault):
    value=lan()
    if fault=='missing':value.pop('ERP_LAN_HTTP_ORIGIN')
    if fault=='extra-secret':value['OPENAI_API_KEY']='synthetic-forbidden'
    if fault=='list':value=[]
    if fault=='too-long':value['ERP_ALLOWED_ORIGINS']='x'*4097
    with pytest.raises(ValueError):network.validate(value)


def test_signed_source_authorization_binds_exact_network_and_preserves_existing_codec():
    key=Ed25519PrivateKey.generate();public=public_bytes(key);now=datetime(2026,10,11,tzinfo=timezone.utc)
    target={'host_sha256':'a'*64,'installation_id':'b'*32,'service_uid':499,'service_gid':499,
            'service_account':'_tianmingerp','root':'/Library/TianmingERP/instance'}
    value=https();request=cutover.make_network_request(target,'c'*64,value,public,now=now)
    assert contract.validate_request(request)==request and request['network_sha256']==contract.digest(value)
    snapshot={**{k:'d'*64 for k in cutover.SNAPSHOT_HASHES},'revision':'synthetic','tables':1,'files':1,'registered_attachments':0}
    evidence={'source_sha256':'e'*64,'backup_sha256':'f'*64,'source_package_sha256':'d'*64,
        'backup_created_at':cutover._stamp(now+timedelta(minutes=1)), 'manual_stop':True,'process_stopped':True,
        'credential_purposes':['backup'],'snapshot':snapshot}
    raw=cutover.sign_authorization(request,evidence,key,now=now+timedelta(minutes=2))
    assert cutover.verify_authorization(raw,public,request)['source']==evidence
    changed=deepcopy(request);changed['network_sha256']=contract.digest(lan())
    with pytest.raises(ValueError):cutover.verify_authorization(raw,public,changed)
    assert cutover.NETWORK_FIELDS==network.FIELDS and cutover.DOMAIN==contract.DOMAIN


def test_validated_https_proposal_is_accepted_by_actual_mobile_qr_parser(monkeypatch):
    from app.services.mobile_qr import mobile_qr_origin
    value=network.validate(https())
    monkeypatch.delenv('ERP_MOBILE_QR_ORIGIN',raising=False)
    for key,val in value.items():monkeypatch.setenv(key,val)
    assert mobile_qr_origin()==value['ERP_SHELF_LABEL_ORIGIN']

@pytest.mark.parametrize('mode',['mobile-https','browser-fallback'])
def test_qr_explicit_override_and_empty_fallback_follow_bound_network(monkeypatch,mode):
    from app.services.mobile_qr import mobile_qr_origin
    value=https()
    if mode=='mobile-https':value['ERP_MOBILE_QR_ORIGIN']='https://erp.example.test'
    else:
        value.update(ERP_LAN_HTTP_ORIGIN='',ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS='',ERP_SHELF_LABEL_ORIGIN='',
                     ERP_TRUSTED_HOSTS='erp.example.test')
    network.validate(value)
    for key,val in value.items():monkeypatch.setenv(key,val)
    assert mobile_qr_origin(configured_default='https://erp.example.test/')=='https://erp.example.test'


def test_mobile_origin_is_a_bound_deployment_field():
    assert 'ERP_MOBILE_QR_ORIGIN' in cutover.NETWORK_FIELDS
    value=https();before=contract.digest(network.validate(value))
    value['ERP_MOBILE_QR_ORIGIN']='https://erp.example.test'
    assert contract.digest(network.validate(value)) != before

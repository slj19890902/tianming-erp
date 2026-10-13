"""Separate owner-current-Mac authority; historical cutover contracts keep their meaning.

The owner declaration is an explicit claim, never invented Windows process proof.
Preparation uses the existing audited v3 fact structure. Consumers still authenticate
the stopped UID499 instance, fresh mounted backup and original restoration anchors.
"""
import base64
from datetime import timedelta
import hashlib

from desktop_assistant import current_mac_cutover_contract as facts, factory_network

TYPE = 'tianming.factory-owner-current-mac.activation.v1'
REQUEST = 'tianming.factory-owner-current-mac.target.v1'
AUTHORIZATION = 'tianming.factory-owner-current-mac.authorization.v1'
DECLARATION = 'tianming.factory-owner-current-mac.owner-declaration.v1'
DOMAIN = b'TIANMING-ERP-FACTORY-ACTIVATION\x00owner-current-mac-v1\x00'
STATEMENT = '旧ERP已清除，Mac为唯一业务事实源'
CAPABILITY = 'factory_owner_current_mac_v1'
PLAN = 'tianming.factory-owner-current-mac.plan.v1'
REPLY = 'tianming.factory-owner-current-mac.reply.v1'
GATE = 'tianming.factory-owner-current-mac.publication.v1'
GATE_NAME = 'owner-current-mac-activation-publication.json'
FAILED = 'owner-current-mac-activation-failed.json'
MODULE = 'desktop_assistant/owner_activation.py'
FILES = frozenset({MODULE, 'desktop_assistant/owner_activation_contract.py',
    'desktop_assistant/mac_publisher_trust.py', 'desktop_assistant/mac_factory_runtime.py',
    'desktop_assistant/current_mac_target_audit.py', 'desktop_assistant/mounted_nas_proof.py'})
canonical, decode, digest = facts.canonical, facts.decode, facts.digest
require, keys = facts._require, facts._keys


def validate_request(value, *, preparation=None):
    keys(value, facts.REQUEST_FIELDS | {'mac_publisher_sha256'})
    require(value['type'] == REQUEST)
    facts._hash(value['mac_publisher_sha256'])
    require(value['mac_publisher_sha256'] != value['publisher_sha256'])
    legacy = {k:v for k,v in value.items() if k != 'mac_publisher_sha256'}
    facts.validate_request({**legacy,'type':facts.REQUEST}, preparation=preparation)
    return value


def make_request(preparation, network_sha256, mac_publisher_sha256, *, request_id, now):
    prep = facts.validate_preparation(decode(canonical(preparation)))
    value = facts.make_request(prep, network_sha256, prep['publisher_sha256'], request_id=request_id, now=now)
    value.update(type=REQUEST,mac_publisher_sha256=mac_publisher_sha256)
    return validate_request(value,preparation=prep)


def validate_declaration(value, request):
    keys(value, {'type','statement','old_erp_removed','mac_is_only_business_source','attested_at','request_sha256'})
    require(value['type'] == DECLARATION and value['statement'] == STATEMENT
        and value['old_erp_removed'] is True and value['mac_is_only_business_source'] is True
        and value['request_sha256'] == digest(validate_request(request)))
    require(facts._time(request['created_at']) <= facts._time(value['attested_at']) < facts._time(request['expires_at']))
    return value


def make_declaration(request, *, confirmed, now):
    require(confirmed is True)
    return validate_declaration(dict(type=DECLARATION,statement=STATEMENT,old_erp_removed=True,
        mac_is_only_business_source=True,attested_at=facts._stamp(now),request_sha256=digest(request)),request)


def validate_body(value):
    keys(value, {'type','request','preparation','owner_declaration','issued_at','install_before'})
    require(value['type'] == AUTHORIZATION)
    prep = facts.validate_preparation(value['preparation'])
    request = validate_request(value['request'],preparation=prep)
    declaration = validate_declaration(value['owner_declaration'],request)
    issued,before = facts._time(value['issued_at']),facts._time(value['install_before'])
    require(facts._time(declaration['attested_at']) <= issued < before <= facts._time(request['expires_at'])
        and before-issued <= timedelta(hours=8))
    return value


def make_body(request, preparation, declaration, *, now):
    stamp = facts._current(now).replace(microsecond=0)
    return validate_body(decode(canonical(dict(type=AUTHORIZATION,request=request,preparation=preparation,
        owner_declaration=declaration,issued_at=facts._stamp(stamp),
        install_before=facts._stamp(min(stamp+timedelta(hours=8),facts._time(request['expires_at'])))))))


def authorization_body(raw, expected_request):
    envelope = decode(raw);keys(envelope, {'body','signature'})
    body = validate_body(envelope['body'])
    try:
        signature = base64.b64decode(envelope['signature'],validate=True)
    except (ValueError,TypeError):
        raise ValueError(facts.ERROR) from None
    require(len(signature) == 64 and base64.b64encode(signature).decode('ascii') == envelope['signature']
        and canonical(body['request']) == canonical(validate_request(expected_request)))
    return body


def require_install_window(body, *, now):
    validate_body(body)
    require(facts._time(body['issued_at']) <= facts._current(now) < facts._time(body['install_before']))


def validate_record(value):
    keys(value, facts.RECORD_FIELDS - {'operator_attestation'})
    require(value['type'] == TYPE)
    request = validate_request(value['request'])
    require(digest(factory_network.validate(value['network'])) == request['network_sha256'])
    for key in ('authorization_sha256','context_sha256','registry_sha256'):facts._hash(value[key])
    require(type(value['authorization']) is str)
    raw = value['authorization'].encode('utf-8')
    require(hashlib.sha256(raw).hexdigest() == value['authorization_sha256'])
    body = authorization_body(raw,request);prep = body['preparation']
    require(all(value[k] == prep[k] for k in ('context_sha256','registry_sha256','target_snapshot')))
    require_install_window(body,now=facts._time(value['activated_at']))
    return body


def make_record(request,network,authorization,*,activated_at):
    body = authorization_body(authorization,request);require_install_window(body,now=activated_at)
    prep = body['preparation']
    value = decode(canonical(dict(type=TYPE,request=request,network=network,
        authorization=authorization.decode('utf-8'),authorization_sha256=hashlib.sha256(authorization).hexdigest(),
        context_sha256=prep['context_sha256'],registry_sha256=prep['registry_sha256'],
        activated_at=facts._stamp(activated_at),target_snapshot=prep['target_snapshot'])))
    validate_record(value);return value


def validate_plan(value):
    keys(value, {'type','attempt_id','code_manifest_sha256','restore_attempt_id','preparation',
        'request','network','authorization'})
    require(value['type'] == PLAN)
    facts._id(value['attempt_id']);facts._id(value['restore_attempt_id']);facts._hash(value['code_manifest_sha256'])
    prep = facts.validate_preparation(value['preparation'])
    request = validate_request(value['request'],preparation=prep)
    require(digest(factory_network.validate(value['network'])) == request['network_sha256'])
    require(type(value['authorization']) is str)
    body = authorization_body(value['authorization'].encode('utf-8'),request)
    require(canonical(body['preparation']) == canonical(prep))
    return value


def validate_gate(value, *, activation_raw=None, ready=None):
    keys(value, {'type','attempt_id','plan_sha256','activation_sha256','request_sha256','ready'})
    require(value['type'] == GATE and type(value['ready']) is bool)
    facts._id(value['attempt_id'])
    for key in ('plan_sha256','activation_sha256','request_sha256'):facts._hash(value[key])
    if ready is not None:require(value['ready'] is ready)
    if activation_raw is not None:
        record = decode(activation_raw);validate_record(record)
        require(value['activation_sha256'] == hashlib.sha256(activation_raw).hexdigest()
            and value['request_sha256'] == digest(record['request']))
    return value


def validate_reply(value,plan,plan_sha256,challenge):
    validate_plan(plan)
    keys(value, {'type','attempt_id','plan_sha256','challenge','identity','record'})
    require(value['type'] == REPLY and value['attempt_id'] == plan['attempt_id']
        and value['plan_sha256'] == plan_sha256 and value['challenge'] == challenge
        and value['identity'] == dict(uid=499,euid=499,gid=499,egid=499,kernel_groups=[499]))
    body = validate_record(value['record'])
    require(canonical(body['preparation']) == canonical(plan['preparation'])
        and canonical(body['request']) == canonical(plan['request'])
        and value['record']['authorization'] == plan['authorization']
        and value['record']['network'] == plan['network'])
    return value

"""Public protocol only; no installed instance, credentials, NAS or launchd."""
from copy import deepcopy
import json

import pytest

from desktop_assistant import current_mac_cutover_contract as v3
from desktop_assistant import current_mac_worker_contract as c
from tests.desktop_assistant.test_current_mac_cutover_contract import (
    valid_body, valid_network, envelope, h, at)


@pytest.fixture
def chain():
    body = valid_body(v3)
    prep = body['preparation']
    created = int(at('2026-10-12T02:00:00Z').timestamp())
    common = {k:h(100+i) for i,k in enumerate(sorted(c.COMMON)) if k.endswith('_sha256')}
    common.update(type=c.PLAN, attempt_id='a'*32, phase='update',
        created_at=created, expires_at=created+c.MAX_AGE, uid=499, gid=499,
        restore_attempt_id='b'*32)
    for key in ('context_sha256', 'publisher_sha256', 'registry_sha256',
            'execution_package_sha256', 'monitor_package_sha256',
            'system_control_sha256', 'system_plist_sha256'):
        common[key] = prep[key]
    for key in ('transport_package_sha256', 'transport_control_sha256', 'transport_contract_sha256'):
        common[key] = prep['backup'][key]
    common['restore_receipt_sha256'] = prep['restore']['restore_receipt_sha256']
    common['network_sha256'] = body['request']['network_sha256']
    plans = {}
    update = dict(common, current_package_sha256=h(99), state_before_sha256=h(98))
    plans['update'] = update
    stopped = dict(update_result_file_sha256=h(97), state_stopped_sha256=prep['state_sha256'],
        backup_sha256=prep['backup']['sha256'], backup_receipt_sha256=prep['backup']['receipt_sha256'],
        request_id=body['request']['request_id'])
    plans['prepare'] = dict(common, **stopped, phase='prepare')
    activate = {k:v for k,v in stopped.items() if k != 'update_result_file_sha256'}
    activate.update(preparation_file_sha256=h(96), preparation_sha256=v3.digest(prep),
        request_file_sha256=h(95), request_sha256=v3.digest(body['request']),
        authorization_file_sha256=c.raw_digest(envelope(v3, body)),
        operator_attestation={k:True for k in v3.ATTESTATION})
    plans['activate'] = dict(common, **activate, phase='activate')
    requests, replies, hashes = {}, {}, {}
    identity = dict(uid=499,euid=499,gid=499,egid=499,kernel_groups=[499])
    result = dict(state_before_sha256=h(98), state_after_sha256=prep['state_sha256'],
        previous_package_sha256=h(99), current_package_sha256=prep['execution_package_sha256'],
        backup_sha256=prep['backup']['sha256'], backup_receipt_sha256=prep['backup']['receipt_sha256'],
        nas_independent_readback=True)
    for phase, plan in plans.items():
        hashes[phase] = c.raw_digest(c.canonical(plan))
        requests[phase] = dict(type=c.REQUEST, attempt_id=plan['attempt_id'], phase=phase,
            plan_sha256=hashes[phase],challenge='c'*32,lock_fd=11 if phase=='activate' else None)
        if phase == 'update': payload=result
        elif phase == 'prepare': payload=dict(preparation=prep,request=body['request'])
        else:
            record=v3.make_record(body['request'],valid_network(),envelope(v3,body),
                activated_at=at(),operator_attestation=plan['operator_attestation'])
            payload=dict(record=record)
        replies[phase] = dict(type=c.REPLY,attempt_id=plan['attempt_id'],phase=phase,
            plan_sha256=hashes[phase],challenge='c'*32,identity=deepcopy(identity),
            manual_stop=True,process_stopped=True,home_rehearsal=True,
            borrowed_lock=phase=='activate',result=deepcopy(payload))
    raw=json.dumps(replies['update'],indent=2).encode()
    plans['prepare']['update_result_file_sha256']=c.raw_digest(raw)
    hashes['prepare']=c.raw_digest(c.canonical(plans['prepare']))
    requests['prepare']['plan_sha256']=replies['prepare']['plan_sha256']=hashes['prepare']
    return plans,requests,replies,hashes,raw


@pytest.mark.parametrize('phase',sorted(c.PHASES))
def test_three_closed_phases_and_unmodified_inputs(chain,phase):
    before=deepcopy(chain)
    plans,requests,replies,hashes,_=chain
    assert c.validate_reply(replies[phase],requests[phase],plans[phase],hashes[phase]) is replies[phase]
    c.validate_plan(plans[phase],now=plans[phase]['created_at'])
    assert chain==before
    assert c.plan_path('a'*32,phase)==('/Library/TianmingERP/current-mac-cutover-'+'a'*32+'/'+phase+'-plan.json')


def test_prior_reply_binds_raw_file_and_all_complete_backup_facts(chain):
    p,q,r,hsh,raw=chain
    assert c.raw_digest(raw)!=v3.digest(r['update'])
    c.validate_update_link(r['update'],q['update'],p['update'],hsh['update'],p['prepare'],update_result_raw=raw)
    for key in ('state_stopped_sha256','backup_sha256','backup_receipt_sha256',
                'context_sha256','monitor_package_sha256','transport_package_sha256'):
        other=deepcopy(p['prepare']);other[key]=h(700)
        with pytest.raises(ValueError):
            c.validate_update_link(r['update'],q['update'],p['update'],hsh['update'],other,update_result_raw=raw)
    with pytest.raises(ValueError):
        c.validate_update_link(r['update'],q['update'],p['update'],hsh['update'],p['prepare'],update_result_raw=c.canonical(r['update']))


@pytest.mark.parametrize('phase',sorted(c.PHASES))
@pytest.mark.parametrize('section',['plan','request','reply'])
@pytest.mark.parametrize('change',['missing','extra'])
def test_closed_fields_refuse_injection(chain,phase,section,change):
    p,q,r,hsh,_=chain
    value={'plan':p,'request':q,'reply':r}[section][phase]
    if change=='missing':value.pop(next(iter(value)))
    else:value['password']='should-never-be-a-public-field'
    with pytest.raises(ValueError):c.validate_reply(r[phase],q[phase],p[phase],hsh[phase])


@pytest.mark.parametrize('phase',sorted(c.PHASES))
@pytest.mark.parametrize('fd',[False,True,0,2,-1,3.0,'11',c.MAX_FD+1])
def test_activation_requires_only_a_real_fd_number(chain,phase,fd):
    p,q,r,hsh,_=chain;q[phase]['lock_fd']=fd
    with pytest.raises(ValueError):c.validate_request(q[phase],p[phase],hsh[phase])


@pytest.mark.parametrize('phase',['update','prepare'])
def test_parent_lock_cannot_be_injected_into_manager_owned_phase(chain,phase):
    p,q,r,hsh,_=chain;q[phase]['lock_fd']=11
    with pytest.raises(ValueError):c.validate_request(q[phase],p[phase],hsh[phase])


@pytest.mark.parametrize('phase',sorted(c.PHASES))
@pytest.mark.parametrize('field',['uid','euid','gid','egid','kernel_groups'])
def test_reply_requires_exact_kernel_identity(chain,phase,field):
    p,q,r,hsh,_=chain
    r[phase]['identity'][field]=[499,12] if field=='kernel_groups' else True
    with pytest.raises(ValueError):c.validate_reply(r[phase],q[phase],p[phase],hsh[phase])


@pytest.mark.parametrize('phase',sorted(c.PHASES))
@pytest.mark.parametrize('field',['manual_stop','process_stopped','home_rehearsal','borrowed_lock'])
def test_no_truthy_or_false_success_claims(chain,phase,field):
    p,q,r,hsh,_=chain;r[phase][field]=1
    with pytest.raises(ValueError):c.validate_reply(r[phase],q[phase],p[phase],hsh[phase])


@pytest.mark.parametrize('phase',sorted(c.PHASES))
@pytest.mark.parametrize('field',['attempt_id','phase','plan_sha256','challenge'])
def test_request_replay_and_cross_phase_refused(chain,phase,field):
    p,q,r,hsh,_=chain
    q[phase][field]={'attempt_id':'d'*32,'phase':'stop','plan_sha256':h(500),'challenge':'X'*32}[field]
    with pytest.raises(ValueError):c.validate_reply(r[phase],q[phase],p[phase],hsh[phase])


@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),-1,253402300800,'now'])
def test_plan_execution_clock_strict(chain,value):
    p,*_=chain
    with pytest.raises(ValueError):c.validate_plan(p['update'],now=value)


def test_plan_expiry_and_locator_injection(chain):
    p,*_=chain
    c.validate_plan(p['update'],now=p['update']['expires_at']-0.1)
    with pytest.raises(ValueError):c.validate_plan(p['update'],now=p['update']['expires_at'])
    with pytest.raises(ValueError):c.validate_plan(p['update'],now=p['update']['created_at']-0.1)
    for attempt,phase in [('../escape','update'),('a'*32,'../plan'),(True,'update')]:
        with pytest.raises(ValueError):c.plan_path(attempt,phase)


@pytest.mark.parametrize('raw',[b'{"a":1,"a":2}',b'{"n":1.0}',b'{"n":NaN}',
    b'{"n":Infinity}',b'[]',b'\xff',b'{}'+b' '*c.LIMIT])
def test_bounded_strict_wire_decode(raw):
    with pytest.raises(ValueError):c.decode(raw)


@pytest.mark.parametrize('phase',['prepare','activate'])
@pytest.mark.parametrize('location',['state','backup','transport','restore','request_id','network'])
def test_prepared_evidence_cannot_be_substituted(chain,phase,location):
    p,q,r,hsh,_=chain
    # Mutate the plan instead: changing the authorization would invalidate v3 first.
    key={'state':'state_stopped_sha256','backup':'backup_sha256','transport':'transport_package_sha256',
         'restore':'restore_receipt_sha256','request_id':'request_id','network':'network_sha256'}[location]
    p[phase][key]='e'*32 if key=='request_id' else h(800)
    with pytest.raises(ValueError):c.validate_reply(r[phase],q[phase],p[phase],hsh[phase])


def test_activation_requires_explicit_attestation_and_raw_authorization_binding(chain):
    p,q,r,hsh,_=chain
    for key in v3.ATTESTATION:
        other=deepcopy(p['activate']);other['operator_attestation'][key]=False
        with pytest.raises(ValueError):c.validate_plan(other)
    p['activate']['authorization_file_sha256']=h(900)
    with pytest.raises(ValueError):c.validate_reply(r['activate'],q['activate'],p['activate'],hsh['activate'])


def test_contract_acceptance_does_not_claim_crypto_or_io(chain):
    p,q,r,hsh,_=chain
    # The fixture signature is 64 zero bytes: strict shape accepts it deliberately.
    # Future workers still have to authenticate the original publisher's signature.
    c.validate_reply(r['activate'],q['activate'],p['activate'],hsh['activate'])
    assert not hasattr(c,'run') and not hasattr(c,'install')


@pytest.mark.parametrize('field,value',[
    ('nas_independent_readback',False),('nas_independent_readback',1),
    ('state_before_sha256',h(901)),('state_after_sha256',h(98)),
    ('previous_package_sha256',h(902)),('current_package_sha256',h(903)),
    ('backup_sha256','A'*64),('backup_receipt_sha256',None)])
def test_update_cannot_report_old_or_unverified_backup(chain,field,value):
    p,q,r,hsh,_=chain;r['update']['result'][field]=value
    with pytest.raises(ValueError):c.validate_reply(r['update'],q['update'],p['update'],hsh['update'])


def test_prior_reply_bytes_must_contain_the_exact_validated_reply(chain):
    p,q,r,hsh,raw=chain
    changed=deepcopy(r['update']);changed['challenge']='f'*32
    other_raw=c.canonical(changed)
    p['prepare']['update_result_file_sha256']=c.raw_digest(other_raw)
    with pytest.raises(ValueError):
        c.validate_update_link(r['update'],q['update'],p['update'],hsh['update'],p['prepare'],update_result_raw=other_raw)

"""Fixed root stage plans and results, under separately approved code.

Internal API only: an external SHA-approved loader must authenticate this entire
administrator pack first. No secret, arbitrary file path, service start, restore
or source signature is accepted/created. Partial attempts stay exclusive.
"""
from contextlib import ExitStack
from datetime import datetime,timezone
import os
from pathlib import Path
import time
import uuid

from desktop_assistant import current_mac_activation_install as activation
from desktop_assistant import current_mac_worker_contract as c
from desktop_assistant import current_mac_cutover_contract as v3
from desktop_assistant import mac_system_upgrade as root
from desktop_assistant import mac_smb_stream_runtime_contract as stream
from desktop_assistant import factory_network, mounted_nas_proof as mounted

MODULE='desktop_assistant/current_mac_plan_install.py'
BASE=Path('/Library/TianmingERP')
ROOT=BASE/'instance'
ERROR='Current Mac stage unconfirmed; preserve scene; no retry, start or cleanup'

def require(ok):
    if not ok:raise ValueError(ERROR)

def _code():
    require(Path(__file__).absolute()==Path(activation.__file__).absolute().parent.parent/MODULE)
    return activation.verify_code()

def _request(reply):
    return dict(type=c.REQUEST,**{k:reply[k] for k in ('attempt_id','phase','plan_sha256','challenge')},lock_fd=None)

def _read(stack,parent,name,mode=0o644):
    item=stack.enter_context(root.Pin(parent.path/name,mode=mode,parent=parent))
    return item,item.read()

def _facts(stack,base,instance,plan):
    """Pin public control bytes; worker independently verifies full facts."""
    require(_code()==plan['code_manifest_sha256'])
    context=None;public=None
    for role in ('execution','monitor','transport'):
        meta=root.system.metadata(plan[role+'_package_sha256'])
        require(meta['context']['uid']==meta['context']['gid']==499
            and all(meta[k]==plan[k] for k in ('context_sha256','publisher_sha256','registry_sha256')))
        if context is None:context=meta['context'];public=meta['public']
        else:require(meta['context']==context and meta['public']==public)
    pins=[]
    artifacts=[(root.system.CONTROL,'system_control_sha256'),(root.system.PLIST,'system_plist_sha256')]
    if plan['phase']!='checkpoint':
        artifacts += ([(mounted.CONTROL,'transport_control_sha256'),(mounted.CONTRACT,'transport_contract_sha256')]
            if plan['type']==c.MOUNTED_PLAN else
            [(stream.CONTROL,'transport_control_sha256'),(stream.CONTRACT,'transport_contract_sha256')])
    for path,field in artifacts:
        for parent in path.parents:root.boot.protected(parent,directory=True)
        pin=stack.enter_context(root.Pin(path,mode=0o644));raw=pin.read()
        observed=c.raw_digest(raw)
        if path in (stream.CONTRACT,mounted.CONTRACT):observed=c.raw_digest(c.canonical(c.decode(raw)))
        require(observed==plan[field]);pins.append((pin,raw))
    home=stack.enter_context(root.Pin(BASE/'home',uid=499,gid=499,mode=0o700,directory=True,mutable=True,parent=base))
    original=stack.enter_context(root.Pin(home.path/('service-restore-'+plan['restore_attempt_id']+'.json'),
        uid=499,gid=499,mode=0o600,parent=home))
    raw=original.read();require(c.raw_digest(raw)==plan['restore_receipt_sha256']);pins.append((original,raw))
    def check():
        base.check();instance.check();home.check();root._no_pending(instance)
        activation._pending(base)
        require(not os.path.lexists(BASE/'factory-activation.json')
            and not os.path.lexists(BASE/activation.gate.FAILED)
            and _code()==plan['code_manifest_sha256'])
        c.validate_plan(plan,now=time.time())
        for item,raw in pins:require(item.read()==raw)
        for role in ('execution','monitor','transport'):
            meta=root.system.metadata(plan[role+'_package_sha256'])
            require(meta['context']==context and meta['public']==public
                and all(meta[k]==plan[k] for k in ('context_sha256','publisher_sha256','registry_sha256')))
    check();return check

def _state(instance,expected,current):
    raw,state=root._state(instance,499,499)
    require(root.boot.digest(raw)==expected and state.get('current')==current
        and state.get('manual_stop') is True and state.get('pid') is None)
    return raw

def _parents(stack):
    base=stack.enter_context(root.Pin(BASE,mode=0o755,directory=True,mutable=True))
    instance=stack.enter_context(root.Pin(ROOT,uid=499,gid=499,mode=0o700,directory=True,mutable=True,parent=base))
    return base,instance

def _slot(stack,base,attempt):
    require(c.token(attempt))
    return stack.enter_context(root.Pin(BASE/('current-mac-cutover-'+attempt),mode=0o755,
        directory=True,mutable=True,parent=base))

def _store(slot,name,raw):
    root._new(slot,name,raw,mode=0o644)

def _receipt(slot,phase,plan_raw,reply_raw):
    raw=c.canonical(dict(type='tianming.current-mac-stage-result.v1',phase=phase,
        plan_sha256=c.raw_digest(plan_raw),reply_sha256=c.raw_digest(reply_raw)))
    _store(slot,phase+'-completed.json',raw)

def _previous(stack,slot,phase):
    held=[]
    for name in (phase+'-plan.json',phase+'-result.json',phase+'-completed.json'):
        held.append(_read(stack,slot,name))
    plan_raw,reply_raw,complete=(raw for _,raw in held)
    plan=c.validate_plan(c.decode(plan_raw),now=time.time());reply=c.decode(reply_raw)
    require(plan_raw==c.canonical(plan) and reply_raw==c.canonical(reply))
    c.validate_reply(reply,_request(reply),plan,c.raw_digest(plan_raw))
    require(complete==c.canonical(dict(type='tianming.current-mac-stage-result.v1',phase=phase,
        plan_sha256=c.raw_digest(plan_raw),reply_sha256=c.raw_digest(reply_raw))))
    def check():
        slot.check()
        for item,raw in held:require(item.read()==raw)
    check();return plan,reply,reply_raw,check

def update_plan(attempt_id,execution,monitor,transport,restore_attempt,restore_receipt_sha,network_raw):
    """Create public fields from protected current records, no disk writes."""
    root._entry()
    require(c.token(attempt_id) and c.token(restore_attempt)
        and all(c.sha(x) for x in (execution,monitor,transport,restore_receipt_sha)))
    network=factory_network.validate(c.decode(network_raw))
    with root._publication(),ExitStack() as stack:
        base,instance=_parents(stack)
        state_raw,state=root._state(instance,499,499)
        meta=root.system.metadata(execution);created=int(time.time())
        plan=dict(type=c.PLAN,attempt_id=attempt_id,phase='update',created_at=created,
            expires_at=created+c.MAX_AGE,uid=499,gid=499,code_manifest_sha256=_code(),
            **{k:meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')},
            execution_package_sha256=execution,monitor_package_sha256=monitor,
            transport_package_sha256=transport,restore_attempt_id=restore_attempt,
            restore_receipt_sha256=restore_receipt_sha,current_package_sha256=state.get('current'),
            state_before_sha256=root.boot.digest(state_raw),network_file_sha256=c.raw_digest(network_raw),
            network_sha256=v3.digest(network))
        for path,field in ((root.system.CONTROL,'system_control_sha256'),
                (root.system.PLIST,'system_plist_sha256'),(stream.CONTROL,'transport_control_sha256'),
                (stream.CONTRACT,'transport_contract_sha256')):
            for parent in path.parents:root.boot.protected(parent,directory=True)
            pin=stack.enter_context(root.Pin(path,mode=0o644));raw=pin.read()
            plan[field]=stream.archive.digest(c.decode(raw)) if path==stream.CONTRACT else c.raw_digest(raw)
        c.validate_plan(plan,now=time.time())
        check=_facts(stack,base,instance,plan);check()
        _state(instance,plan['state_before_sha256'],plan['current_package_sha256'])
        return c.canonical(plan)

def update(plan_raw,network_raw):
    """Exclusive first stage from a fixed, reviewed public update plan."""
    root._entry();plan=c.validate_plan(c.decode(plan_raw),now=time.time())
    require(plan['phase']=='update' and plan_raw==c.canonical(plan))
    network=factory_network.validate(c.decode(network_raw))
    require(c.raw_digest(network_raw)==plan['network_file_sha256'] and v3.digest(network)==plan['network_sha256'])
    with root._publication(),ExitStack() as stack:
        base,instance=_parents(stack);check=_facts(stack,base,instance,plan)
        _state(instance,plan['state_before_sha256'],plan['current_package_sha256'])
        with os.scandir(base.fd) as entries:
            require(not any(e.name.startswith('current-mac-cutover-') for e in entries))
        name='current-mac-cutover-'+plan['attempt_id']
        os.mkdir(name,0o755,dir_fd=base.fd)
        fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=base.fd)
        try:os.fchmod(fd,0o755);os.fsync(fd)
        finally:os.close(fd)
        os.fsync(base.fd);slot=_slot(stack,base,plan['attempt_id'])
        _store(slot,'network.json',network_raw);_store(slot,'update-plan.json',plan_raw)
        root._new(slot,'update-attempt.json',c.canonical(dict(type='tianming.current-mac-stage-attempt.v1',
            phase='update',plan_sha256=c.raw_digest(plan_raw))),mode=0o600)
        check();reply=activation.dispatch.dispatch(plan_raw,verify_code=_code);check()
        c.validate_reply(reply,_request(reply),plan,c.raw_digest(plan_raw))
        _state(instance,reply['result']['state_after_sha256'],plan['execution_package_sha256'])
        raw=c.canonical(reply);_store(slot,'update-result.json',raw);check()
        _state(instance,reply['result']['state_after_sha256'],plan['execution_package_sha256'])
        _receipt(slot,'update',plan_raw,raw)
        return reply

def prepare(attempt_id):
    """Derive the next plan only from protected prior complete result bytes."""
    root._entry()
    with root._publication(),ExitStack() as stack:
        base,instance=_parents(stack);slot=_slot(stack,base,attempt_id)
        old,reply,reply_raw,prior_check=_previous(stack,slot,'update')
        require(old['phase']=='update' and old['attempt_id']==attempt_id)
        result=reply['result'];created=int(time.time())
        plan={k:old[k] for k in c.COMMON}
        plan.update(phase='prepare',created_at=created,update_result_file_sha256=c.raw_digest(reply_raw),
            state_stopped_sha256=result['state_after_sha256'],backup_sha256=result['backup_sha256'],
            backup_receipt_sha256=result['backup_receipt_sha256'],request_id=uuid.uuid4().hex)
        raw=c.canonical(c.validate_plan(plan,now=time.time()))
        c.validate_update_link(reply,_request(reply),old,c.raw_digest(c.canonical(old)),plan,update_result_raw=reply_raw)
        facts_check=_facts(stack,base,instance,plan)
        network,network_raw=_read(stack,slot,'network.json')
        require(c.raw_digest(network_raw)==plan['network_file_sha256'])
        def check():
            prior_check();facts_check();require(network.read()==network_raw)
            _state(instance,plan['state_stopped_sha256'],plan['execution_package_sha256'])
        check();_store(slot,'prepare-plan.json',raw)
        root._new(slot,'prepare-attempt.json',c.canonical(dict(type='tianming.current-mac-stage-attempt.v1',
            phase='prepare',plan_sha256=c.raw_digest(raw))),mode=0o600)
        reply=activation.dispatch.dispatch(raw,verify_code=_code);check()
        c.validate_reply(reply,_request(reply),plan,c.raw_digest(raw))
        result_raw=c.canonical(reply);_store(slot,'prepare-result.json',result_raw)
        _store(slot,'preparation.json',c.canonical(reply['result']['preparation']))
        _store(slot,'request.json',c.canonical(reply['result']['request']))
        check();_receipt(slot,'prepare',raw,result_raw)
        return reply

def activation_plan(attempt_id,authorization_raw,*,factory_cutover_authorized=False,source_isolation_confirmed=False):
    """Plan only; existing activation transaction still authenticates/signs."""
    root._entry();require(factory_cutover_authorized is True and source_isolation_confirmed is True)
    with root._publication(),ExitStack() as stack:
        base,instance=_parents(stack);slot=_slot(stack,base,attempt_id)
        old,reply,reply_raw,prior_check=_previous(stack,slot,'prepare')
        require(old['phase']=='prepare' and old['attempt_id']==attempt_id)
        prep=reply['result']['preparation'];request=reply['result']['request']
        pp,prep_raw=_read(stack,slot,'preparation.json');rp,request_raw=_read(stack,slot,'request.json')
        np,network_raw=_read(stack,slot,'network.json')
        require(c.raw_digest(network_raw)==old['network_file_sha256']
            and v3.digest(factory_network.validate(c.decode(network_raw)))==old['network_sha256'])
        require(c.decode(prep_raw)==prep and c.decode(request_raw)==request)
        body=v3.authorization_body(authorization_raw,request)
        require(c.canonical(body['preparation'])==c.canonical(prep))
        v3.require_install_window(body,now=datetime.now(timezone.utc))
        plan={k:old[k] for k in c.COMMON}
        plan.update(phase='activate',created_at=int(time.time()),state_stopped_sha256=old['state_stopped_sha256'],
            backup_sha256=old['backup_sha256'],backup_receipt_sha256=old['backup_receipt_sha256'],
            request_id=old['request_id'],preparation_file_sha256=c.raw_digest(prep_raw),
            preparation_sha256=v3.digest(prep),request_file_sha256=c.raw_digest(request_raw),
            request_sha256=v3.digest(request),authorization_file_sha256=c.raw_digest(authorization_raw),
            operator_attestation={k:True for k in v3.ATTESTATION})
        if old['type']==c.MOUNTED_PLAN:plan['local_update_receipt_sha256']=old['local_update_receipt_sha256']
        raw=c.canonical(c.validate_plan(plan,now=time.time()));check=_facts(stack,base,instance,plan)
        prior_check();_state(instance,plan['state_stopped_sha256'],plan['execution_package_sha256'])
        _store(slot,'authorization.json',authorization_raw);_store(slot,'activate-plan.json',raw)
        check();prior_check();require(pp.read()==prep_raw and rp.read()==request_raw and np.read()==network_raw)
        _state(instance,plan['state_stopped_sha256'],plan['execution_package_sha256'])
        return dict(attempt_id=attempt_id,plan_sha256=c.raw_digest(raw),started=False,factory_activated=False)


def checkpoint_plan(attempt_id,execution,monitor,auditor,restore_attempt,restore_receipt_sha,network_raw):
    """Root-approved read-only plan for a new backup, with execution unchanged."""
    root._entry();require(c.token(attempt_id) and c.token(restore_attempt))
    for value in (execution,monitor,auditor,restore_receipt_sha):require(c.sha(value))
    network=factory_network.validate(c.decode(network_raw))
    with root._publication(),ExitStack() as stack:
        base,instance=_parents(stack);state_raw,state=root._state(instance,499,499)
        require(state.get('current')==execution and state.get('manual_stop') is True and state.get('pid') is None)
        mounted._original_proof(state)
        meta=root.system.metadata(auditor);created=int(time.time())
        plan=dict(type=c.MOUNTED_PLAN,attempt_id=attempt_id,phase='checkpoint',created_at=created,
            expires_at=created+c.MAX_AGE,uid=499,gid=499,code_manifest_sha256=_code(),
            **{k:meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')},
            execution_package_sha256=execution,monitor_package_sha256=monitor,transport_package_sha256=auditor,
            restore_attempt_id=restore_attempt,restore_receipt_sha256=restore_receipt_sha,
            state_before_sha256=root.boot.digest(state_raw),network_file_sha256=c.raw_digest(network_raw),
            network_sha256=v3.digest(network))
        for path,field in ((root.system.CONTROL,'system_control_sha256'),(root.system.PLIST,'system_plist_sha256')):
            for parent in path.parents:root.boot.protected(parent,directory=True)
            item=stack.enter_context(root.Pin(path,mode=0o644));plan[field]=c.raw_digest(item.read())
        c.validate_plan(plan,now=time.time());check=_facts(stack,base,instance,plan);check()
        return c.canonical(plan)


def checkpoint(plan_raw,network_raw):
    """Produce one independently restored local checkpoint; no restart/update."""
    root._entry();plan=c.validate_plan(c.decode(plan_raw),now=time.time())
    require(plan['type']==c.MOUNTED_PLAN and plan['phase']=='checkpoint' and plan_raw==c.canonical(plan)
        and c.raw_digest(network_raw)==plan['network_file_sha256']
        and v3.digest(factory_network.validate(c.decode(network_raw)))==plan['network_sha256'])
    with root._publication(),ExitStack() as stack:
        base,instance=_parents(stack);check=_facts(stack,base,instance,plan)
        _state(instance,plan['state_before_sha256'],plan['execution_package_sha256'])
        name='current-mac-cutover-'+plan['attempt_id']
        os.mkdir(name,0o755,dir_fd=base.fd);os.fsync(base.fd)
        slot=_slot(stack,base,plan['attempt_id'])
        _store(slot,'network.json',network_raw);_store(slot,'checkpoint-plan.json',plan_raw)
        root._new(slot,'checkpoint-attempt.json',c.canonical(dict(type='tianming.current-mac-stage-attempt.v1',
            phase='checkpoint',plan_sha256=c.raw_digest(plan_raw))),mode=0o600)
        check();reply=activation.dispatch.dispatch(plan_raw,verify_code=_code);check()
        c.validate_reply(reply,_request(reply),plan,c.raw_digest(plan_raw))
        _state(instance,plan['state_before_sha256'],plan['execution_package_sha256'])
        raw=c.canonical(reply);_store(slot,'checkpoint-result.json',raw);_receipt(slot,'checkpoint',plan_raw,raw)
        return reply


def prepare_mounted(attempt_id):
    """Admit a completed local checkpoint and genuine mounted proof directly."""
    root._entry()
    with root._publication(),ExitStack() as stack:
        base,instance=_parents(stack);slot=_slot(stack,base,attempt_id)
        old,previous,previous_raw,prior_check=_previous(stack,slot,'checkpoint')
        require(old['type']==c.MOUNTED_PLAN and old['phase']=='checkpoint')
        bound=mounted.load();require(bound['contract']['checkpoint_attempt_id']==attempt_id
            and bound['contract']['checkpoint_result_sha256']==c.raw_digest(previous_raw)
            and bound['control']['package_sha256']==old['transport_package_sha256'])
        state_raw,state=root._state(instance,499,499);mounted._original_proof(state)
        require(state.get('current')==old['execution_package_sha256']
            and state.get('manual_stop') is True and state.get('pid') is None)
        created=int(time.time());plan={k:old[k] for k in c.COMMON if k in old}
        plan.update(phase='prepare',created_at=created,expires_at=created+c.MAX_AGE,
            state_stopped_sha256=c.raw_digest(state_raw),backup_sha256=bound['receipt']['sha256'],
            backup_receipt_sha256=bound['control']['receipt_sha256'],request_id=uuid.uuid4().hex,
            local_update_receipt_sha256=bound['contract']['local_update_receipt_sha256'],
            transport_control_sha256=bound['control_sha'],transport_contract_sha256=bound['control']['contract_sha256'])
        raw=c.canonical(c.validate_plan(plan,now=time.time()));facts_check=_facts(stack,base,instance,plan)
        def check():
            prior_check();facts_check();require(mounted.load()['raw']==bound['raw'])
            _state(instance,plan['state_stopped_sha256'],plan['execution_package_sha256'])
        check();_store(slot,'prepare-plan.json',raw)
        root._new(slot,'prepare-attempt.json',c.canonical(dict(type='tianming.current-mac-stage-attempt.v1',
            phase='prepare',plan_sha256=c.raw_digest(raw))),mode=0o600)
        reply=activation.dispatch.dispatch(raw,verify_code=_code);check()
        c.validate_reply(reply,_request(reply),plan,c.raw_digest(raw))
        reply_raw=c.canonical(reply);_store(slot,'prepare-result.json',reply_raw)
        _store(slot,'preparation.json',c.canonical(reply['result']['preparation']))
        _store(slot,'request.json',c.canonical(reply['result']['request']))
        check();_receipt(slot,'prepare',raw,reply_raw);return reply

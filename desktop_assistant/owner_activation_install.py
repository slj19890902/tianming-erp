"""Root owner activation: sign locally, audit as UID499, publish while stopped.

The caller must authenticate the closed administrator pack before importing it.
The supplied signer holds the root Keychain identity; no private key is accepted,
written, returned or passed to UID499. This module never starts or restores ERP.
"""
from contextlib import ExitStack
from datetime import datetime,timezone
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import uuid

from desktop_assistant import owner_activation_contract as c
from desktop_assistant import mac_system_upgrade as root,current_mac_dispatch as dispatch
from desktop_assistant import mounted_nas_proof as mounted,factory_network
from desktop_assistant.home_process import command
from desktop_assistant.current_mac_activation_install import CODE_FILES as AUDIT_CODE_FILES

BASE = Path('/Library/TianmingERP')
ROOT = BASE/'instance'
MODULE = 'desktop_assistant/owner_activation_install.py'
CODE_FILES = AUDIT_CODE_FILES | frozenset({MODULE,'desktop_assistant/owner_activation_contract.py',
    'desktop_assistant/mac_publisher_trust.py'})


def _pending(base,allowed=None):
    with os.scandir(base.fd) as entries:
        for entry in entries:
            if entry.name.endswith('.pending.json') and entry.name.startswith(
                    ('factory-activation-','current-mac-activation-','owner-current-mac-activation-')):
                c.require(entry.name == allowed)


def _dispatch(plan_raw,lock_fd,nas_fd,verify_code):
    plan = c.validate_plan(c.decode(plan_raw));prep = plan['preparation']
    meta = root.system.metadata(prep['backup']['transport_package_sha256'])
    manifest = root.system._program_manifest(meta)
    c.require(type(manifest.get('runtime_capabilities',{}).get(c.CAPABILITY)) is int
        and manifest['runtime_capabilities'][c.CAPABILITY] == 1 and c.FILES <= manifest['files'].keys())
    challenge = uuid.uuid4().hex;plan_sha = hashlib.sha256(plan_raw).hexdigest()
    args = [str(meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(meta['tree']/c.MODULE),
        '--attempt',plan['attempt_id'],'--plan-sha256',plan_sha,'--challenge',challenge,
        '--lock-fd',str(lock_fd),'--mounted-fd',str(nas_fd)]
    environment = dict(HOME=str(BASE/'home'),PATH='/usr/bin:/bin:/usr/sbin:/sbin',LANG='en_US.UTF-8',ERP_HOME_REHEARSAL='1')
    c.require(verify_code() == plan['code_manifest_sha256'])
    child = subprocess.Popen(command(args,environment),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,user=499,group=499,extra_groups=[],close_fds=True,pass_fds=(lock_fd,nas_fd),
        umask=0o077,cwd='/',env=environment)
    raw = dispatch._collect(child)
    c.require(verify_code() == plan['code_manifest_sha256'])
    return c.validate_reply(c.decode(raw),plan,plan_sha,challenge)


def activate(attempt_id,preparation_raw,network_raw,restore_attempt,*,signer,verify_code,owner_declaration_confirmed=False):
    """One exclusive attempt, external code approval and explicit owner claim."""
    from desktop_assistant import mac_publisher_trust as trust
    root._entry();c.require(owner_declaration_confirmed is True and callable(signer) and callable(verify_code))
    c.facts._id(attempt_id);c.facts._id(restore_attempt)
    prep = c.facts.validate_preparation(c.decode(preparation_raw));network = factory_network.validate(c.decode(network_raw))
    c.require(preparation_raw == c.canonical(prep))
    approved_code = verify_code();c.facts._hash(approved_code)
    with root._publication(),ExitStack() as stack:
        base = stack.enter_context(root.Pin(BASE,mode=0o755,directory=True,mutable=True))
        instance = stack.enter_context(root.Pin(ROOT,uid=499,gid=499,mode=0o700,directory=True,mutable=True,parent=base))
        control = stack.enter_context(root.Pin(ROOT/'control',uid=499,gid=499,mode=0o700,directory=True,mutable=True,parent=instance))
        lock = stack.enter_context(root.Pin(control.path/'operation.lock',uid=499,gid=499,mode=0o600,parent=control))
        pins = []
        def public(path):
            item = stack.enter_context(root.Pin(path,mode=0o644,parent=base));raw = item.read()
            pins.append((item,raw));return raw
        mac_public = public(trust.MAC_PUBLIC);trust_raw = public(trust.RECORD)
        pause_path = BASE/'automatic-backup-paused.json'
        pause_raw = public(pause_path) if os.path.lexists(pause_path) else None
        if pause_raw is not None:
            pause = c.decode(pause_raw)
            c.require(set(pause) == {'type','context_sha256','paused'}
                and pause['type'] == 'tianming.automatic-backup-pause.v1'
                and pause['context_sha256'] == prep['context_sha256'] and pause['paused'] is True)
        authority = trust.read_authority(expected_context_sha256=prep['context_sha256'],
            expected_legacy_anchor_sha256=prep['publisher_sha256'],
            expected_mac_publisher_sha256=trust.public_fingerprint(mac_public))
        c.require(authority['mac_public'] == mac_public and authority['raw'] == trust_raw)
        bound = mounted.load();backup = prep['backup']
        c.require(bound['contract']['checkpoint_attempt_id'] is not None
            and backup['release_sha256'] == prep['execution_package_sha256']
            and bound['control']['package_sha256'] == backup['transport_package_sha256']
            and bound['control']['context_sha256'] == prep['context_sha256']
            and bound['control']['publisher_sha256'] == prep['publisher_sha256']
            and bound['control_sha'] == backup['transport_control_sha256']
            and bound['control']['contract_sha256'] == backup['transport_contract_sha256']
            and bound['control']['receipt_sha256'] == backup['receipt_sha256']
            and bound['receipt']['sha256'] == backup['sha256'] and bound['receipt']['size'] == backup['size'])
        for path,raw in bound['raw'].items():c.require(public(path) == raw)
        for path,expected in ((root.system.CONTROL,prep['system_control_sha256']),(root.system.PLIST,prep['system_plist_sha256'])):
            for parent in path.parents:root.boot.protected(parent,directory=True)
            item = stack.enter_context(root.Pin(path,mode=0o644));raw = item.read()
            c.require(hashlib.sha256(raw).hexdigest() == expected);pins.append((item,raw))
        target_name = None;network_name = None;pending_name = None;published = [];body = None
        def check():
            base.check();instance.check();control.check();lock.check()
            c.require(verify_code() == approved_code and lock.read() == b'0')
            c.require(os.path.lexists(pause_path) == (pause_raw is not None))
            if body is not None:c.require_install_window(body,now=datetime.now(timezone.utc))
            root._no_pending(instance);_pending(base,pending_name)
            state_raw,state = root._state(instance,499,499)
            c.require(hashlib.sha256(state_raw).hexdigest() == prep['state_sha256']
                and state.get('current') == prep['execution_package_sha256']
                and state.get('manual_stop') is True and state.get('pid') is None)
            for item,raw in pins+published:c.require(item.read() == raw)
            fresh = trust.read_authority(expected_context_sha256=prep['context_sha256'],
                expected_legacy_anchor_sha256=prep['publisher_sha256'],
                expected_mac_publisher_sha256=trust.public_fingerprint(mac_public))
            c.require(fresh['raw'] == trust_raw and fresh['mac_public'] == mac_public)
            for identity in (prep['execution_package_sha256'],prep['monitor_package_sha256'],backup['transport_package_sha256']):
                meta = root.system.metadata(identity)
                manifest = root.system._program_manifest(meta)
                c.require(meta['context']['uid'] == meta['context']['gid'] == 499
                    and all(meta[k] == prep[k] for k in ('context_sha256','publisher_sha256','registry_sha256'))
                    and type(manifest.get('runtime_capabilities',{}).get(c.CAPABILITY)) is int
                    and manifest['runtime_capabilities'][c.CAPABILITY] == 1 and c.FILES <= manifest['files'].keys())
            c.require(not os.path.lexists(BASE/c.FAILED))
            if os.path.lexists(BASE/'factory-activation.json'):
                c.require(any(item.path == BASE/'factory-activation.json' for item,_ in published))
        for name in ('factory-activation.json',c.GATE_NAME,c.FAILED):c.require(not os.path.lexists(BASE/name))
        with root._locked(lock):
            check()
            now = datetime.now(timezone.utc).replace(microsecond=0)
            request = c.make_request(prep,c.digest(network),trust.public_fingerprint(mac_public),request_id=uuid.uuid4().hex,now=now)
            declaration = c.make_declaration(request,confirmed=owner_declaration_confirmed,now=now)
            body = c.make_body(request,prep,declaration,now=now)
            signature = signer(c.DOMAIN+c.canonical(body))
            c.require(type(signature) is bytes and len(signature) == 64)
            authorization = c.canonical(dict(body=body,signature=base64.b64encode(signature).decode('ascii')))
            # Shape only here. UID499 independently verifies the actual signature
            # before any activation record can be published.
            c.authorization_body(authorization,request)
            plan = c.validate_plan(dict(type=c.PLAN,attempt_id=attempt_id,code_manifest_sha256=approved_code,
                restore_attempt_id=restore_attempt,preparation=prep,request=request,network=network,authorization=authorization.decode()))
            plan_raw = c.canonical(plan);plan_sha = hashlib.sha256(plan_raw).hexdigest()
            slot_name = 'owner-current-mac-'+attempt_id
            os.mkdir(slot_name,0o755,dir_fd=base.fd);os.fsync(base.fd)
            slot = stack.enter_context(root.Pin(BASE/slot_name,mode=0o755,directory=True,mutable=True,parent=base))
            root._new(slot,'plan.json',plan_raw,mode=0o644)
            plan_item = stack.enter_context(root.Pin(slot.path/'plan.json',mode=0o644,parent=slot));pins.append((plan_item,plan_raw))
            pending_name = 'owner-current-mac-activation-'+attempt_id+'.pending.json'
            root._new(base,pending_name,c.canonical(dict(type=c.PLAN,attempt_id=attempt_id,plan_sha256=plan_sha)),mode=0o600)
            try:
                check();c.require_install_window(body,now=datetime.now(timezone.utc))
                broker_plan = dict(transport_package_sha256=backup['transport_package_sha256'],
                    transport_control_sha256=backup['transport_control_sha256'],transport_contract_sha256=backup['transport_contract_sha256'],
                    backup_receipt_sha256=backup['receipt_sha256'])
                with mounted.broker(broker_plan) as nas_fd:
                    c.require(mounted.full_hash(nas_fd,backup['size']) == backup['sha256'])
                    check();reply = _dispatch(plan_raw,lock.fd,nas_fd,verify_code)
                    record = reply['record'];c.validate_record(record)
                    c.require(record['authorization'].encode() == authorization
                        and c.canonical(record['request']) == c.canonical(request)
                        and c.canonical(c.validate_record(record)['preparation']) == c.canonical(prep)
                        and record['network'] == network)
                    c.require(mounted.full_hash(nas_fd,backup['size']) == backup['sha256'])
                    check();c.require_install_window(body,now=datetime.now(timezone.utc))
                record['activated_at'] = c.facts._stamp(datetime.now(timezone.utc));c.validate_record(record)
                activation_raw = c.canonical(record)
                gate = dict(type=c.GATE,attempt_id=attempt_id,plan_sha256=plan_sha,
                    activation_sha256=hashlib.sha256(activation_raw).hexdigest(),request_sha256=c.digest(request),ready=False)
                c.validate_gate(gate,activation_raw=activation_raw,ready=False)
                gate_raw = c.canonical(gate);root._new(base,c.GATE_NAME,gate_raw,mode=0o644)
                target_name = 'factory-target-'+request['request_id']+'.json';network_name = 'factory-network-'+request['request_id']+'.json'
                for name,raw in ((target_name,c.canonical(request)),(network_name,c.canonical(network)),('factory-activation.json',activation_raw)):
                    check();c.require_install_window(body,now=datetime.now(timezone.utc));root._new(base,name,raw,mode=0o644)
                    item = stack.enter_context(root.Pin(BASE/name,mode=0o644,parent=base));published.append((item,raw));check()
                ready = c.canonical({**gate,'ready':True});check()
                root._cas(base,c.GATE_NAME,gate_raw,ready,'owner-current-mac-activation-'+attempt_id+'.commit.pending.json')
                with root.Pin(BASE/c.GATE_NAME,mode=0o644,parent=base) as item:
                    c.validate_gate(c.decode(item.read()),activation_raw=activation_raw,ready=True)
                check();return dict(factory_activated=True,started=False,manual_stop=True,attempt_id=attempt_id,
                    activation_sha256=hashlib.sha256(activation_raw).hexdigest(),owner_declaration=declaration)
            except BaseException:
                if not os.path.lexists(BASE/c.FAILED):
                    root._new(base,c.FAILED,c.canonical(dict(type=c.GATE,attempt_id=attempt_id,plan_sha256=plan_sha,preserve_scene=True)),mode=0o644)
                raise

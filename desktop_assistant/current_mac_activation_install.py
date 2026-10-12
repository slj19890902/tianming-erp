"""Root publication of a freshly audited, stopped current-Mac target.

Candidate internal API only. An external fixed-SHA loader must approve the
closed administrator code pack before import; verify_code is not approval.
Original source signature and full business/credential audit occur in the
fixed signed UID499 worker. Root only reads public state/control/input data.
No start, restore, source operation, cleanup, or implicit retry is provided.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import time

from desktop_assistant import mac_system_upgrade as root
from desktop_assistant import current_mac_dispatch as dispatch
from desktop_assistant import current_mac_worker_contract as c
from desktop_assistant import current_mac_cutover_contract as v3
from desktop_assistant import current_mac_publication as gate
from desktop_assistant import factory_network
from desktop_assistant import mac_smb_stream_runtime_contract as stream

BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
MODULE = 'desktop_assistant/current_mac_activation_install.py'
CODE_FILES = root.CODE_FILES | frozenset({MODULE, 'desktop_assistant/current_mac_dispatch.py',
    'desktop_assistant/current_mac_worker_contract.py',
    'desktop_assistant/current_mac_cutover_contract.py',
    'desktop_assistant/current_mac_publication.py'})
ERROR = 'Current Mac root publication unconfirmed; preserve scene; no retry or start'


def require(ok):
    if not ok:
        raise ValueError(ERROR)


def _pending(base, allowed=None):
    # Preserve the original v1 pending-publication gate as well as this API's
    # retained attempts. Recovery from either requires a separate reviewed path.
    with os.scandir(base.fd) as entries:
        for entry in entries:
            if entry.name.endswith('.pending.json') and entry.name.startswith(
                    ('factory-activation-','current-mac-activation-')):
                require(entry.name == allowed)


def verify_code():
    """Recheck the separately approved, closed stdlib administrator pack."""
    folder = Path(__file__).absolute().parent.parent
    for parent in (folder, *folder.parents):
        root.boot.protected(parent, directory=True, readable=True)
    for module, name in ((root, root.MODULE), (dispatch, 'desktop_assistant/current_mac_dispatch.py'),
            (c, 'desktop_assistant/current_mac_worker_contract.py'),
            (v3, 'desktop_assistant/current_mac_cutover_contract.py'),
            (gate, 'desktop_assistant/current_mac_publication.py'),
            (factory_network, 'desktop_assistant/factory_network.py'),
            (root.boot, 'desktop_assistant/mac_keychain_install.py'),
            (root.system, 'desktop_assistant/mac_system_service.py'),
            (root.publication, 'desktop_assistant/mac_release_publish.py'),
            (root.contract, root.CONTRACT_MODULE)):
        require(Path(module.__file__).absolute() == folder / name)
    raw = root.boot.protected(folder / 'install-manifest.json', readable=True)
    value = c.decode(raw)
    require(set(value) == {'type','source','files'} and value['type'] == 'tianming.keychain-code.v1'
        and type(value['source']) is str and re.fullmatch('[0-9a-f]{40}',value['source'])
        and type(value['files']) is dict and set(value['files']) == CODE_FILES
        and all(c.sha(item) for item in value['files'].values()))
    allowed = {str(parent) for name in CODE_FILES for parent in Path(name).parents if str(parent) != '.'}
    actual, pending = set(), [folder]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path); require(not entry.is_symlink())
                if entry.is_dir(follow_symlinks=False):
                    require(path.relative_to(folder).as_posix() in allowed)
                    root.boot.protected(path,directory=True,readable=True); pending.append(path)
                else:
                    require(entry.is_file(follow_symlinks=False))
                    actual.add(path.relative_to(folder).as_posix())
    require(actual == CODE_FILES | {'install-manifest.json'})
    for name in CODE_FILES:
        require(root.boot.digest(root.boot.protected(folder/name,readable=True)) == value['files'][name])
    return c.raw_digest(raw)


def activate(attempt_id, plan_sha256, *, factory_cutover_authorized=False,
        source_isolation_confirmed=False):
    root._entry()
    require(c.token(attempt_id) and c.sha(plan_sha256)
        and factory_cutover_authorized is True and source_isolation_confirmed is True)
    path = BASE / ('current-mac-cutover-' + attempt_id) / 'activate-plan.json'
    require(str(path) == c.plan_path(attempt_id,'activate'))
    with root._publication(), ExitStack() as stack:
        base = stack.enter_context(root.Pin(BASE,mode=0o755,directory=True,mutable=True))
        instance = stack.enter_context(root.Pin(ROOT,uid=499,gid=499,mode=0o700,
            directory=True,mutable=True,parent=base))
        control = stack.enter_context(root.Pin(ROOT/'control',uid=499,gid=499,mode=0o700,
            directory=True,mutable=True,parent=instance))
        lock = stack.enter_context(root.Pin(control.path/'operation.lock',uid=499,gid=499,
            mode=0o600,parent=control))
        slot = stack.enter_context(root.Pin(path.parent,mode=0o755,directory=True,parent=base))
        pins = []
        def read(name, expected):
            item = stack.enter_context(root.Pin(slot.path/name,mode=0o644,parent=slot))
            raw = item.read(); require(c.raw_digest(raw) == expected)
            pins.append((item,raw)); return raw
        plan_raw = read('activate-plan.json',plan_sha256)
        plan = c.validate_plan(c.decode(plan_raw),now=time.time())
        require(plan['phase'] == 'activate' and plan['attempt_id'] == attempt_id
            and verify_code() == plan['code_manifest_sha256'])
        network_raw = read('network.json',plan['network_file_sha256'])
        prep_raw = read('preparation.json',plan['preparation_file_sha256'])
        request_raw = read('request.json',plan['request_file_sha256'])
        authorization = read('authorization.json',plan['authorization_file_sha256'])
        network = factory_network.validate(c.decode(network_raw))
        prep = v3.validate_preparation(c.decode(prep_raw))
        request = v3.validate_request(c.decode(request_raw),preparation=prep)
        body = v3.authorization_body(authorization,request)  # Shape only; worker authenticates signature.
        c._preparation(prep,plan);c._target_request(request,prep,plan)
        require(v3.digest(network) == plan['network_sha256']
            and v3.digest(prep) == plan['preparation_sha256']
            and v3.digest(request) == plan['request_sha256']
            and request['request_id'] == plan['request_id']
            and c.canonical(body['preparation']) == c.canonical(prep))
        target_name = 'factory-target-' + request['request_id'] + '.json'
        network_name = 'factory-network-' + request['request_id'] + '.json'
        for artifact,expected in ((root.system.CONTROL,plan['system_control_sha256']),
                (root.system.PLIST,plan['system_plist_sha256']),
                (stream.CONTROL,plan['transport_control_sha256']),
                (stream.CONTRACT,plan['transport_contract_sha256'])):
            for parent in artifact.parents:root.boot.protected(parent,directory=True)
            item = stack.enter_context(root.Pin(artifact,mode=0o644))
            raw = item.read()
            observed = stream.archive.digest(c.decode(raw)) if artifact == stream.CONTRACT else c.raw_digest(raw)
            require(observed == expected);pins.append((item,raw))
        for name in ('factory-activation.json',gate.NAME,gate.FAILED,target_name,network_name):
            require(not os.path.lexists(BASE/name))
        _pending(base)
        published = []
        pending_name = None
        def check():
            base.check();instance.check();control.check();slot.check();lock.check()
            require(lock.read() == b'0' and verify_code() == plan['code_manifest_sha256'])
            c.validate_plan(plan,now=time.time())
            v3.require_install_window(body,now=datetime.now(timezone.utc))
            root._no_pending(instance)
            _pending(base,pending_name)
            state_raw,state = root._state(instance,499,499)
            require(root.boot.digest(state_raw) == plan['state_stopped_sha256']
                and state.get('current') == plan['execution_package_sha256']
                and state.get('manual_stop') is True and state.get('pid') is None)
            for item,raw in pins:require(item.read() == raw)
            for item,raw in published:require(item.read() == raw)
            require(not os.path.lexists(BASE/gate.FAILED))
            if os.path.lexists(BASE/'factory-activation.json'):
                require(any(item.path == BASE/'factory-activation.json' for item,_ in published))
            public = None
            for role in ('execution','monitor','transport'):
                meta = root.system.metadata(plan[role+'_package_sha256'])
                require(all(meta[key] == plan[key] for key in
                    ('context_sha256','publisher_sha256','registry_sha256'))
                    and meta['context']['uid'] == meta['context']['gid'] == 499)
                if public is None:public = meta['public']
                else:require(public == meta['public'])
        with root._locked(lock):
            check()
            pending = 'current-mac-activation-' + attempt_id + '.pending.json'
            root._new(base,pending,c.canonical(dict(type='tianming.current-mac-activation-attempt.v1',
                attempt_id=attempt_id,plan_sha256=plan_sha256)),mode=0o600)
            pending_name = pending
            attempt = stack.enter_context(root.Pin(BASE/pending,mode=0o600,parent=base))
            pins.append((attempt,attempt.read()))
            try:
                reply = dispatch.dispatch(plan_raw,verify_code=verify_code,inherited_lock=lock.fd)
                record = reply['result']['record']; v3.validate_record(record)
                require(c.canonical(record['request']) == c.canonical(request)
                    and c.canonical(record['network']) == c.canonical(network)
                    and record['authorization'].encode() == authorization
                    and record['operator_attestation'] == plan['operator_attestation'])
                check()
                # Root time is freshly checked while the same maintenance FD is held.
                record['activated_at'] = v3._stamp(datetime.now(timezone.utc));v3.validate_record(record)
                activation_raw = c.canonical(record)
                claim = dict(type=gate.TYPE,attempt_id=attempt_id,plan_sha256=plan_sha256,
                    activation_sha256=c.raw_digest(activation_raw),request_sha256=v3.digest(request),ready=False)
                gate.validate(claim,activation_raw=activation_raw,ready=False)
                gate_raw = c.canonical(claim)
                root._new(base,gate.NAME,gate_raw,mode=0o644)
                for name,raw in ((target_name,request_raw),(network_name,network_raw),
                        ('factory-activation.json',activation_raw)):
                    check();root._new(base,name,raw,mode=0o644)
                    item = stack.enter_context(root.Pin(BASE/name,mode=0o644,parent=base))
                    published.append((item,raw));check()
                ready = c.canonical({**claim,'ready':True})
                check()
                root._cas(base,gate.NAME,gate_raw,ready,
                    'current-mac-activation-' + attempt_id + '.commit.pending.json')
                check()
                with root.Pin(BASE/gate.NAME,mode=0o644,parent=base) as item:
                    gate.validate(c.decode(item.read()),activation_raw=activation_raw,ready=True)
                return dict(type='tianming.current-mac-activation-result.v1',
                    activation_sha256=c.raw_digest(activation_raw),attempt_id=attempt_id,
                    factory_activated=True,started=False,manual_stop=True,
                    source_isolation_attested=True,source_physical_isolation_verified=False,
                    network_reachability_verified=False,logout_reboot_verified=False)
            except BaseException:
                # Create a fail-closed sentinel before releasing the parent's lock.
                # Its mere presence rejects runtime, even if a write is partial.
                if not os.path.lexists(BASE/gate.FAILED):
                    root._new(base,gate.FAILED,c.canonical(dict(type='tianming.current-mac-activation-failed.v1',
                        attempt_id=attempt_id,plan_sha256=plan_sha256,preserve_scene=True)),mode=0o644)
                raise

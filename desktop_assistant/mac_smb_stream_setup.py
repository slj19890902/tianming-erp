"""Exclusive first provisioning; root never receives the NAS password.

Root orchestration uses only the reviewed stdlib bootstrap pack. The signed
service worker imports SMB/Keychain code only after authenticating its pending
plan. No ERP control, preferences switch, backup, replacement or cleanup here.
"""
import getpass
import hmac
import os
from pathlib import Path
from contextlib import contextmanager, ExitStack
import re
import socket
import subprocess
import sys
import time
import uuid
import warnings

from . import mac_keychain_install as boot, mac_release_publish as publication
from . import mac_system_service as service, mac_mount as mounts
from . import mac_smb_stream_runtime_contract as cfg, mac_smb_stream_contract as archive

ENTRY = 'desktop_assistant/service_smb_stream_setup_entry.py'
MODULE = 'desktop_assistant/mac_smb_stream_setup.py'
TYPE = 'tianming.mac-smb-stream-install-plan.v1'
RESULT = 'tianming.mac-smb-stream-install-worker.v1'
FIELDS = {'type','attempt_id','created_at','expires_at','code_manifest_sha256',
          'package_sha256','context_sha256','publisher_sha256','registry_sha256','contract'}
PHASES = {'identity','provision','verify'}
SIZE = 16384
ERROR = 'NAS流配置未确认；保留本次现场，不覆盖重试，不输出密码或异常详情'


def require(ok):
    if not ok: raise ValueError(ERROR)


def token(value): return isinstance(value,str) and re.fullmatch('[0-9a-f]{32}',value) is not None


def pending(attempt):
    require(token(attempt))
    return cfg.BASE / ('smb-stream-install-'+attempt+'.pending.json')


def metadata(package):
    meta = service.metadata(package)
    with (meta['tree'].parent/'package.zip').open('rb') as source:
        _, manifest, _ = publication.inspect(source)
    cfg.capability(manifest)
    return meta



def _root_local_addresses():
    """Closed stdlib root pack: query kernel interfaces without site packages."""
    result = subprocess.run(['/sbin/ifconfig', '-a'], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, cwd='/',
        env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL':'C'}, close_fds=True)
    require(result.returncode == 0 and 0 < len(result.stdout) <= 1024**2)
    addresses = set()
    for line in result.stdout.decode('ascii', 'strict').splitlines():
        fields = line.split()
        if fields and fields[0] in ('inet', 'inet6'):
            require(len(fields) >= 2)
            address = mounts.ip(fields[1].split('%', 1)[0])
            require(address.version == (4 if fields[0] == 'inet' else 6))
            addresses.add(str(address))
    # An empty/partial OS inventory must not silently admit a self-backup.
    require('127.0.0.1' in addresses and '::1' in addresses)
    return addresses


def _local_addresses():
    if sys.platform == 'darwin' and os.geteuid() == 0:
        return _root_local_addresses()
    return mounts.local_addresses()

def binding(plan, meta):
    require(all(plan[key] == meta[key] for key in
        ('context_sha256','publisher_sha256','registry_sha256')))
    archive.validate(plan['contract'],plan['context_sha256'],meta['context']['uid'])
    require(plan['contract']['server_address'] not in _local_addresses())


def vacant():
    for path in (cfg.CONTROL,cfg.CONTRACT):
        try: path.lstat()
        except FileNotFoundError: pass
        else: require(False)
    with os.scandir(cfg.BASE) as entries:
        require(not any(entry.name.startswith('smb-stream-install-') for entry in entries))


def make_plan(package, contract):
    meta = metadata(package); vacant()
    now = int(time.time())
    plan = dict(type=TYPE,attempt_id=uuid.uuid4().hex,created_at=now,expires_at=now+7*86400,
        code_manifest_sha256=boot.verify_code(trusted=False),package_sha256=package,
        contract=dict(contract),**{k:meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')})
    binding(plan,meta)
    return header(boot.canonical(plan),cfg.digest(boot.canonical(plan)))


def header(raw, approved_sha):
    require(cfg.sha(approved_sha) and cfg.digest(raw)==approved_sha)
    plan = boot.decode(raw)
    require(boot.canonical(plan)==raw and set(plan)==FIELDS and plan['type']==TYPE
        and token(plan['attempt_id']) and all(cfg.sha(plan[k]) for k in FIELDS if k.endswith('_sha256'))
        and all(type(plan[k]) is int for k in ('created_at','expires_at'))
        and 0 < plan['expires_at']-plan['created_at'] <= 7*86400
        and plan['created_at'] <= time.time() < plan['expires_at'] and isinstance(plan['contract'],dict))
    archive.validate(plan['contract'],plan['context_sha256'],plan['contract'].get('uid'))
    return plan


def control(plan, plan_sha):
    digest = archive.digest(plan['contract'])
    result = dict(type=cfg.TYPE,attempt_id=plan['attempt_id'],plan_sha256=plan_sha,
        contract_sha256=digest,credential_account=cfg.account(plan['context_sha256'],digest,plan['attempt_id']),
        **{k:plan[k] for k in ('package_sha256','context_sha256','publisher_sha256')})
    return cfg.validate(result,plan['contract'],plan['context_sha256'],plan['contract']['uid'])


def validate(raw, approved_sha, *, started=False):
    plan = header(raw,approved_sha)
    require(plan['code_manifest_sha256']==boot.verify_code())
    meta = metadata(plan['package_sha256']); binding(plan,meta)
    if started: require(publication.read_record(pending(plan['attempt_id']))==raw)
    else: vacant()
    return plan,meta


def probe_proof(proof, plan):
    # Independent stdlib validation at the root trust boundary. Do not import
    # the protocol/crypto dependency tree into Apple's bootstrap interpreter.
    fields = {'type','contract_sha256','connection_guid','attempt_id','directories','file',
        'bytes','sha256','available_bytes','required_bytes','canary_removed','backup_authenticated'}
    require(isinstance(proof,dict) and set(proof)==fields
        and proof['type']=='tianming.mac-smb-stream-preflight.v2'
        and proof['contract_sha256']==archive.digest(plan['contract'])
        and proof['attempt_id']==plan['attempt_id'] and token(proof['connection_guid'])
        and type(proof['bytes']) is int and proof['bytes']==SIZE and cfg.sha(proof['sha256'])
        and type(proof['required_bytes']) is int and proof['required_bytes']==SIZE
        and type(proof['available_bytes']) is int and SIZE<=proof['available_bytes']<2**63
        and proof['canary_removed'] is True and proof['backup_authenticated'] is False
        and isinstance(proof['directories'],list) and len(proof['directories'])==3)
    for identity in [*proof['directories'],proof['file']]:
        require(isinstance(identity,dict) and set(identity)=={'index'}
            and all(type(v) is int and 0<v<2**64 for v in identity.values()))


def expected(plan, phase, challenge):
    return dict(type=RESULT,phase=phase,challenge=challenge,
        plan_sha256=cfg.digest(boot.canonical(plan)),package_sha256=plan['package_sha256'],
        context_sha256=plan['context_sha256'],contract_sha256=archive.digest(plan['contract']))


def reply(value, plan, meta, phase, challenge):
    common = expected(plan,phase,challenge)
    common['identity'] = boot._expected(meta['context']['uid'],meta['context']['gid'])
    extra = ({'proof','credential_created','credential_readback_verified'} if phase=='provision'
             else {'credential_readback_verified'} if phase=='verify' else set())
    require(isinstance(value,dict) and set(value)==set(common)|extra
            and all(value[k]==v for k,v in common.items()))
    if phase!='identity': require(value['credential_readback_verified'] is True)
    if phase=='provision':
        require(value['credential_created'] is True)
        probe_proof(value['proof'],plan)
    return value


def verify_policy():
    result = subprocess.run(['/usr/bin/sandbox-exec','-p',cfg.PROFILE,'/usr/bin/true'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        cwd='/',env={'PATH':'/usr/bin:/bin'},close_fds=True,timeout=10)
    require(result.returncode==0)


def foreground():
    require(os.isatty(0) and os.tcgetpgrp(0)==os.getpgrp())


def worker(meta, raw, phase):
    require(phase in PHASES)
    plan = header(raw,cfg.digest(raw)); binding(plan,meta)
    challenge = uuid.uuid4().hex; connection = None
    command = ['/usr/bin/sandbox-exec','-p',cfg.PROFILE,
        str(meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(meta['tree']/ENTRY),
        '--phase',phase,'--attempt',plan['attempt_id'],'--plan-sha256',cfg.digest(raw),'--challenge',challenge]
    verify_policy()
    try:
        passed = ()
        if phase=='provision':
            foreground()
            connection = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
            connection.settimeout(10)
            endpoint = (plan['contract']['server_address'],445)
            connection.connect(endpoint)
            require(connection.getpeername()==endpoint and connection.fileno()>=3)
            passed = (connection.fileno(),); command += ['--socket-fd',str(passed[0])]
        context = meta['context']
        process = subprocess.Popen(command,stdin=None if phase=='provision' else subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd='/',user=context['uid'],group=context['gid'],
            extra_groups=[],umask=0o077,close_fds=True,pass_fds=passed,
            env={'HOME':str(cfg.BASE/'home'),'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8','ERP_HOME_REHEARSAL':'1'})
        interrupted = False
        while True:
            try: output,_ = process.communicate(); break
            except KeyboardInterrupt: interrupted = True
        require(not interrupted and process.returncode==0)
        return reply(boot.decode(output),plan,meta,phase,challenge)
    finally:
        if connection is not None: connection.close()


def apply(raw, approved_sha):
    identity,_ = boot.modules(); identity._administrator()
    validate(raw,approved_sha)
    with publication.lock():
        plan,meta = validate(raw,approved_sha)
        foreground(); verify_policy()
        boot._write(pending(plan['attempt_id']),raw,mode=0o644)
        publication.sync_directory(cfg.BASE)
        worker(meta,raw,'identity')
        validate(raw,approved_sha,started=True)
        provision = worker(meta,raw,'provision')
        validate(raw,approved_sha,started=True)
        worker(meta,raw,'verify')
        validate(raw,approved_sha,started=True)
        value = control(plan,approved_sha)
        boot._write(cfg.CONTRACT,boot.canonical(plan['contract']),mode=0o644)
        publication.sync_directory(cfg.BASE)
        validate(raw,approved_sha,started=True)
        require(publication.read_record(cfg.CONTRACT)==boot.canonical(plan['contract']))
        result = dict(type='tianming.mac-smb-stream-install.v1',plan_sha256=approved_sha,
            package_sha256=plan['package_sha256'],context_sha256=plan['context_sha256'],
            contract_sha256=value['contract_sha256'],credential_created=True,
            new_process_credential_verified=True,proof=provision['proof'],
            preferences_changed=False,erp_started=False,backup_executed=False,
            launchd_session_verified=False,logout_reboot_verified=False,factory_activated=False)
        completed = cfg.BASE/('smb-stream-install-'+plan['attempt_id']+'.verified.json')
        boot._write(completed,boot.canonical(result),mode=0o644)
        publication.sync_directory(cfg.BASE)
        require(publication.read_record(completed)==boot.canonical(result))
        validate(raw,approved_sha,started=True)
        require(publication.read_record(cfg.CONTRACT)==boot.canonical(plan['contract']))
        # Publish the enabling control last. A preserved contract/verification
        # record without this commit marker cannot pass runtime.load_binding.
        boot._write(cfg.CONTROL,boot.canonical(value),mode=0o644)
        publication.sync_directory(cfg.BASE)
        require(publication.read_record(cfg.CONTROL)==boot.canonical(value))
        return result


def authenticated(args, *, script, executable):
    from app.core import mac_service_keychain as keychain
    from . import mac_smb_stream_runtime as runtime
    require(sys.platform=='darwin' and sys.flags.isolated and sys.dont_write_bytecode
        and args.phase in PHASES and token(args.attempt) and cfg.sha(args.plan_sha256)
        and token(args.challenge) and keychain.selected())
    context = keychain.load_context()
    require(boot._actual()==boot._expected(context['uid'],context['gid']))
    raw = publication.read_record(pending(args.attempt)); plan = header(raw,args.plan_sha256)
    require(plan['attempt_id']==args.attempt)
    meta = metadata(plan['package_sha256']); binding(plan,meta)
    require(context==meta['context'] and Path(script).resolve()==meta['tree']/ENTRY
        and Path(__file__).resolve()==meta['tree']/MODULE
        and Path(executable).resolve()==meta['tree']/'runtime/bin/python3.12')
    require((args.phase=='provision' and type(args.socket_fd) is int and args.socket_fd>=3)
        or (args.phase!='provision' and args.socket_fd is None))
    runtime.denied_new_connection()
    return plan,meta,raw


def run(args, *, script, executable, prompt=getpass.getpass):
    from app.core import mac_service_keychain as keychain
    plan,meta,raw = authenticated(args,script=script,executable=executable)
    value = control(plan,args.plan_sha256)
    def check():
        current,current_meta,current_raw = authenticated(args,script=script,executable=executable)
        require(current==plan and current_meta==meta and current_raw==raw)
    result = expected(plan,args.phase,args.challenge); result['identity'] = boot._actual()
    if args.phase=='identity': return result
    store = keychain.ServiceStore(); require(store.value==meta['context'])
    password = encoded = observed = None
    try:
        if args.phase=='verify':
            observed = store.get(value['credential_account'])
            require(isinstance(observed,bytes) and 0<len(observed)<=1024 and b'\0' not in observed)
            observed.decode('utf-8','strict'); check()
        else:
            foreground()
            with keychain.unlocked(store.api,store.value) as ref:
                store.api.require_absent(ref,keychain.SERVICE,value['credential_account'])
            from .mac_smb_stream_transport import Channel
            from .mac_smb_stream_session import open_share
            from .mac_smb_stream_probe_v2 import check as check_nas
            calls = 0
            def secret():
                nonlocal password,encoded,calls
                require(calls==0); calls+=1
                check(); foreground()
                with warnings.catch_warnings():
                    warnings.simplefilter('error',getpass.GetPassWarning)
                    password = prompt('NAS现有账号密码（隐藏输入）：')
                require(isinstance(password,str))
                encoded = password.encode('utf-8','strict')
                require(0<len(encoded)<=1024 and b'\0' not in encoded); check()
                return password
            channel = Channel(args.socket_fd,(plan['contract']['server_address'],445))
            try:
                with open_share(channel,plan['contract'],context_sha=plan['context_sha256'],
                        uid=meta['context']['uid'],credential_provider=secret,timeout=600) as tree:
                    proof = check_nas(tree,plan['contract'],plan['attempt_id'],SIZE)
                probe_proof(proof,plan); require(calls==1)
            finally: channel.close()
            check()
            store.put(value['credential_account'],encoded)
            observed = store.get(value['credential_account'])
            require(hmac.compare_digest(observed,encoded)); check()
            result.update(proof=proof,credential_created=True)
        result['credential_readback_verified'] = True
        return reply(result,plan,meta,args.phase,args.challenge)
    finally:
        password = encoded = observed = None


def main(*, script, executable):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase',choices=sorted(PHASES | {UPGRADE_PHASE}),required=True)
    parser.add_argument('--attempt',required=True)
    parser.add_argument('--plan-sha256',required=True)
    parser.add_argument('--challenge',required=True)
    parser.add_argument('--socket-fd',type=int)
    args = parser.parse_args()
    operation = run_upgrade_probe if args.phase == UPGRADE_PHASE else run
    result = operation(args,script=script,executable=executable)
    sys.stdout.buffer.write(boot.canonical(result)+b'\n'); sys.stdout.buffer.flush()


# A distinct upgrade protocol. First provisioning and ordinary runtime binding
# remain unchanged; this worker can only read the existing credential and probe.
UPGRADE_PHASE = 'upgrade-probe'
UPGRADE_CAPABILITY = 'archive_smb_upgrade_v1'
UPGRADE_TYPE = 'tianming.mac-smb-stream-upgrade-plan.v1'
UPGRADE_RESULT = 'tianming.mac-smb-stream-upgrade-probe.v1'
UPGRADE_FIELDS = FIELDS | {'previous_control', 'supervisor_sha256', 'activation_sha256'}


def upgrade_pending(attempt):
    require(token(attempt))
    return cfg.BASE / ('smb-stream-upgrade-' + attempt + '.pending.json')


def upgrade_header(raw, approved_sha):
    require(cfg.sha(approved_sha) and cfg.digest(raw) == approved_sha)
    plan = boot.decode(raw)
    require(boot.canonical(plan) == raw and set(plan) == UPGRADE_FIELDS and plan['type'] == UPGRADE_TYPE)
    # Reuse the original closed fields and time/contract validation.
    base = {k: plan[k] for k in FIELDS}; base['type'] = TYPE
    header(boot.canonical(base), cfg.digest(boot.canonical(base)))
    require(cfg.sha(plan['supervisor_sha256']))
    require(plan['activation_sha256'] is None or cfg.sha(plan['activation_sha256']))
    cfg.validate(plan['previous_control'], plan['contract'], plan['context_sha256'], plan['contract']['uid'])
    require(plan['previous_control']['publisher_sha256'] == plan['publisher_sha256']
            and plan['previous_control']['package_sha256'] != plan['package_sha256'])
    return plan


def upgrade_control(plan):
    # The install attempt still identifies the existing credential. The new
    # plan retains the entire previous control, including its original plan SHA.
    value = {**plan['previous_control'], 'package_sha256': plan['package_sha256'],
             'plan_sha256': cfg.digest(boot.canonical(plan))}
    return cfg.validate(value, plan['contract'], plan['context_sha256'], plan['contract']['uid'])


def upgrade_validate(raw, approved_sha, *, started=False):
    plan = upgrade_header(raw, approved_sha)
    meta = metadata(plan['package_sha256']); binding(plan, meta)
    manifest = service._program_manifest(meta)
    require(type(manifest.get('runtime_capabilities', {}).get(UPGRADE_CAPABILITY)) is int
            and manifest['runtime_capabilities'][UPGRADE_CAPABILITY] == 1)
    require(publication.read_record(cfg.CONTROL) == boot.canonical(plan['previous_control'])
            and publication.read_record(cfg.CONTRACT) == boot.canonical(plan['contract'])
            and cfg.digest(publication.read_record(service.CONTROL)) == plan['supervisor_sha256'])
    if started:
        require(publication.read_record(upgrade_pending(plan['attempt_id'])) == raw)
    else:
        require(not os.path.lexists(upgrade_pending(plan['attempt_id'])))
    return plan, meta


def make_upgrade_plan(package):
    from . import mac_system_upgrade as supervisor
    supervisor._entry()
    meta = metadata(package)
    previous = boot.decode(publication.read_record(cfg.CONTROL))
    old = metadata(previous['package_sha256'])
    require(old['context'] == meta['context'] and old['public'] == meta['public'])
    now = int(time.time())
    plan = dict(type=UPGRADE_TYPE, attempt_id=uuid.uuid4().hex,
        created_at=now, expires_at=now+86400, code_manifest_sha256=supervisor.verify_code(),
        package_sha256=package, contract=boot.decode(publication.read_record(cfg.CONTRACT)),
        previous_control=previous, supervisor_sha256=cfg.digest(publication.read_record(service.CONTROL)),
        activation_sha256=_upgrade_activation(),
        **{k:meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')})
    raw = boot.canonical(plan)
    upgrade_validate(raw, cfg.digest(raw))
    return plan


def upgrade_reply(value, plan, meta, challenge):
    expected = dict(type=UPGRADE_RESULT, challenge=challenge,
        plan_sha256=cfg.digest(boot.canonical(plan)), package_sha256=plan['package_sha256'],
        identity=boot._expected(meta['context']['uid'],meta['context']['gid']),
        credential_changed=False, backup_authenticated=False)
    require(isinstance(value,dict) and set(value) == set(expected) | {'proof'}
            and all(value[k] == v and type(value[k]) is type(v) for k,v in expected.items()))
    probe_proof(value['proof'], plan)
    return value


def upgrade_worker(meta, raw):
    plan, current = upgrade_validate(raw, cfg.digest(raw), started=True)
    require(meta == current); verify_policy()
    challenge = uuid.uuid4().hex
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as channel:
        endpoint = (plan['contract']['server_address'],445)
        channel.settimeout(10); channel.connect(endpoint)
        require(channel.getpeername() == endpoint and channel.fileno() >= 3)
        channel.settimeout(None)
        command = ['/usr/bin/sandbox-exec','-p',cfg.PROFILE,
            str(meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(meta['tree']/ENTRY),
            '--phase',UPGRADE_PHASE,'--attempt',plan['attempt_id'],
            '--plan-sha256',cfg.digest(raw),'--challenge',challenge,'--socket-fd',str(channel.fileno())]
        process = subprocess.Popen(command,stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd='/',user=meta['context']['uid'],
            group=meta['context']['gid'],extra_groups=[],umask=0o077,close_fds=True,
            pass_fds=(channel.fileno(),),env={'HOME':str(cfg.BASE/'home'),'PATH':'/usr/bin:/bin',
                'LANG':'en_US.UTF-8','ERP_HOME_REHEARSAL':'1'})
        interrupted = False
        while True:
            try: output,_ = process.communicate(); break
            except KeyboardInterrupt: interrupted = True
        require(not interrupted and process.returncode == 0)
        return upgrade_reply(boot.decode(output),plan,meta,challenge)



def _upgrade_activation():
    """Pin the approved factory record, or explicitly require an unactivated host."""
    path = cfg.BASE / 'factory-activation.json'
    if not os.path.lexists(path):
        return None
    raw = publication.read_record(path)
    value = boot.decode(raw)
    # This upgrade supports the already completed owner handoff only. Unknown
    # activation protocols remain closed instead of being treated as home mode.
    require(value.get('type') == 'tianming.factory-owner-current-mac.activation.v1')
    gate = boot.decode(publication.read_record(cfg.BASE / 'owner-current-mac-activation-publication.json'))
    require(gate.get('ready') is True and gate.get('activation_sha256') == cfg.digest(raw)
            and not os.path.lexists(cfg.BASE / 'owner-current-mac-activation-failed.json'))
    return cfg.digest(raw)


@contextmanager
def _upgrade_paths(plan):
    # Runtime binding is independent of the supervisor's home-only upgrade.
    # Keep exact ownership, operation lock, pending-state and activation gates;
    # never replace or disable the existing factory activation or monitor.
    from . import mac_system_upgrade as supervisor
    with ExitStack() as held:
        base = held.enter_context(supervisor.Pin(cfg.BASE, mode=0o755, directory=True, mutable=True))
        root = held.enter_context(supervisor.Pin(cfg.BASE / 'instance', uid=499, gid=499,
            mode=0o700, directory=True, mutable=True, parent=base))
        control = held.enter_context(supervisor.Pin(root.path / 'control', uid=499, gid=499,
            mode=0o700, directory=True, mutable=True, parent=root))
        operation = held.enter_context(supervisor.Pin(control.path / 'operation.lock',
            uid=499, gid=499, mode=0o600, parent=control))
        require(operation.read() == b'0')
        def check():
            for pin in (base, root, control, operation): pin.check()
            require(_upgrade_activation() == plan['activation_sha256'])
            if plan['activation_sha256'] is not None:
                active = boot.decode(publication.read_record(cfg.BASE / 'factory-activation.json'))
                require(active.get('context_sha256') == plan['context_sha256'])
            supervisor._no_pending(root)
        check()
        yield dict(base=base, root=root, operation=operation, check=check)

def apply_upgrade(raw, approved_sha):
    """Root-only, maintenance-locked CAS. Does not stop ERP or touch its DB."""
    from . import mac_system_upgrade as supervisor
    supervisor._entry()
    plan = upgrade_header(raw, approved_sha)
    require(plan['code_manifest_sha256'] == supervisor.verify_code())
    with supervisor._publication(), _upgrade_paths(plan) as paths, supervisor._locked(paths['operation']):
        plan,meta = upgrade_validate(raw,approved_sha)
        previous_state,_ = supervisor._state(paths['root'],499,499)
        paths['check']()
        boot._write(upgrade_pending(plan['attempt_id']),raw,mode=0o644)
        publication.sync_directory(cfg.BASE)
        result = upgrade_worker(meta,raw)
        upgrade_validate(raw,approved_sha,started=True); paths['check']()
        require(supervisor._state(paths['root'],499,499)[0] == previous_state)
        replacement = boot.canonical(upgrade_control(plan))
        verified = dict(type='tianming.mac-smb-stream-upgrade-verification.v1',
            plan_sha256=approved_sha, previous_control_sha256=cfg.digest(boot.canonical(plan['previous_control'])),
            replacement_control_sha256=cfg.digest(replacement), probe=result,
            credential_changed=False, contract_changed=False, preferences_changed=False,
            database_changed=False, backup_authenticated=False)
        # This is precommit evidence only. Success requires the CAS and normal
        # return; a verification file by itself never means the upgrade applied.
        boot._write(cfg.BASE/('smb-stream-upgrade-'+plan['attempt_id']+'.verified.json'),
                    boot.canonical(verified),mode=0o644)
        publication.sync_directory(cfg.BASE)
        upgrade_validate(raw,approved_sha,started=True); paths['check']()
        supervisor._cas(paths['base'],cfg.CONTROL.name,boot.canonical(plan['previous_control']),
                        replacement,'smb-stream-upgrade-'+plan['attempt_id']+'.control.pending')
        require(publication.read_record(cfg.CONTROL) == replacement)
        return {**verified, 'control_committed':True}


def authenticated_upgrade(args, *, script, executable):
    from app.core import mac_service_keychain as keychain
    from . import mac_smb_stream_runtime as runtime, mac_published_runtime as published
    require(sys.platform == 'darwin' and sys.flags.isolated and sys.dont_write_bytecode
            and args.phase == UPGRADE_PHASE and token(args.attempt) and cfg.sha(args.plan_sha256)
            and token(args.challenge) and type(args.socket_fd) is int and 3 <= args.socket_fd <= 2**31-1
            and keychain.selected())
    context = keychain.load_context()
    require(boot._actual() == boot._expected(context['uid'],context['gid']))
    raw = publication.read_record(upgrade_pending(args.attempt))
    plan,meta = upgrade_validate(raw,args.plan_sha256,started=True)
    require(plan['attempt_id'] == args.attempt and context == meta['context']
            and Path(script).resolve() == meta['tree']/ENTRY
            and Path(__file__).resolve() == meta['tree']/MODULE
            and Path(executable).resolve() == meta['tree']/'runtime/bin/python3.12')
    tree,_ = published.resolve(cfg.BASE/'instance',plan['package_sha256'],meta['public'],kind='migration')
    require(tree == meta['tree']); runtime.denied_new_connection()
    return plan,meta,raw


def run_upgrade_probe(args, *, script, executable):
    from app.core import mac_service_keychain as keychain
    from .mac_smb_stream_transport import Channel
    from .mac_smb_stream_session import open_share
    from .mac_smb_stream_probe_v2 import check as check_nas
    plan,meta,raw = authenticated_upgrade(args,script=script,executable=executable)
    records = {upgrade_pending(args.attempt):raw,
        cfg.CONTROL:boot.canonical(plan['previous_control']), cfg.CONTRACT:boot.canonical(plan['contract'])}
    digests = {service.CONTROL:plan['supervisor_sha256'],
        cfg.BASE/'published.json':plan['registry_sha256'], keychain.CONTEXT:plan['context_sha256'],
        cfg.BASE/'publisher-public.pem':plan['publisher_sha256']}
    def recheck():
        # Full signed tree validation occurred before opening any secret. Root
        # holds the publication lock; recheck immutable approval bytes at each
        # boundary without hashing the 500MB package during the SMB handshake.
        require(boot._actual() == boot._expected(meta['context']['uid'],meta['context']['gid'])
                and keychain.load_context() == meta['context'])
        require(all(publication.read_record(path) == value for path,value in records.items())
                and all(cfg.digest(publication.read_record(path)) == value for path,value in digests.items()))
    recheck()
    store = keychain.ServiceStore(); require(store.value == meta['context'])
    # No put/delete/prompt path: upgrade retains the initial account verbatim.
    def secret():
        recheck()
        value = store.get(plan['previous_control']['credential_account'])
        try:
            require(isinstance(value,bytes) and 0 < len(value) <= 1024 and b'\0' not in value)
            password = value.decode('utf-8','strict'); recheck()
            return password
        finally: value = None
    channel = Channel(args.socket_fd,(plan['contract']['server_address'],445))
    try:
        with open_share(channel,plan['contract'],plan['context_sha256'],meta['context']['uid'],secret) as tree:
            proof = check_nas(tree,plan['contract'],plan['attempt_id'],SIZE)
        recheck()
    finally: channel.close()
    return upgrade_reply(dict(type=UPGRADE_RESULT,challenge=args.challenge,plan_sha256=args.plan_sha256,
        package_sha256=plan['package_sha256'],identity=boot._actual(),proof=proof,
        credential_changed=False,backup_authenticated=False),plan,meta,args.challenge)

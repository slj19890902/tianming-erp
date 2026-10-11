"""Exclusive first provisioning; root never receives the NAS password.

Root orchestration uses only the reviewed stdlib bootstrap pack. The signed
service worker imports SMB/Keychain code only after authenticating its pending
plan. No ERP control, preferences switch, backup, replacement or cleanup here.
"""
import getpass
import hmac
import os
from pathlib import Path
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


def binding(plan, meta):
    require(all(plan[key] == meta[key] for key in
        ('context_sha256','publisher_sha256','registry_sha256')))
    archive.validate(plan['contract'],plan['context_sha256'],meta['context']['uid'])
    require(plan['contract']['server_address'] not in mounts.local_addresses())


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
        and proof['type']=='tianming.mac-smb-stream-preflight.v1'
        and proof['contract_sha256']==archive.digest(plan['contract'])
        and proof['attempt_id']==plan['attempt_id'] and token(proof['connection_guid'])
        and type(proof['bytes']) is int and proof['bytes']==SIZE and cfg.sha(proof['sha256'])
        and type(proof['required_bytes']) is int and proof['required_bytes']==SIZE
        and type(proof['available_bytes']) is int and SIZE<=proof['available_bytes']<2**63
        and proof['canary_removed'] is True and proof['backup_authenticated'] is False
        and isinstance(proof['directories'],list) and len(proof['directories'])==3)
    for identity in [*proof['directories'],proof['file']]:
        require(isinstance(identity,dict) and set(identity)=={'index','created'}
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
            from .mac_smb_stream_probe import check as check_nas
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
    parser.add_argument('--phase',choices=sorted(PHASES),required=True)
    parser.add_argument('--attempt',required=True)
    parser.add_argument('--plan-sha256',required=True)
    parser.add_argument('--challenge',required=True)
    parser.add_argument('--socket-fd',type=int)
    result = run(parser.parse_args(),script=script,executable=executable)
    sys.stdout.buffer.write(boot.canonical(result)+b'\n'); sys.stdout.buffer.flush()

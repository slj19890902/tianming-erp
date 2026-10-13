"""Fixed dedicated-UID phases; no root publication, activation write or start.

update runs in the approved transport tree because the original SMB adapter
authenticates its own module. prepare/activate run in the approved execution
tree because the original full cold-backup audit authenticates its own module.
Top-level imports stay stdlib for the -I -B fixed-file entry.
"""
import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys
import time

BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
MODULE = 'desktop_assistant/current_mac_worker.py'
CONTRACT_MODULE = 'desktop_assistant/current_mac_worker_contract.py'
CAPABILITY = 'current_mac_worker_v1'
FAILURE = 'Current Mac cutover phase failed; preserve scene; no retry, start or cleanup'


def require(ok):
    if not ok:
        raise ValueError(FAILURE)


def _entry():
    require(sys.platform == 'darwin' and sys.flags.isolated and sys.dont_write_bytecode
        and (os.getuid(),os.geteuid(),os.getgid(),os.getegid()) == (499,499,499,499))
    import pwd
    require(pwd.getpwuid(499).pw_name == '_tianmingerp')


@contextmanager
def _plan(request):
    from desktop_assistant import current_mac_worker_contract as c
    from desktop_assistant.mac_service_restore import Lease, _read_complete
    path = Path(c.plan_path(request.get('attempt_id'),request.get('phase')))
    require(path == BASE/('current-mac-cutover-'+request['attempt_id'])/(request['phase']+'-plan.json'))
    with ExitStack() as stack:
        base=stack.enter_context(Lease(BASE,uid=0,gid=None,mode=0o755,directory=True,mutable=True))
        slot=stack.enter_context(Lease(path.parent,uid=0,gid=None,mode=0o755,directory=True,parent=base))
        held=[]
        def read(name,expected=None):
            require(name in {'update-plan.json','update-result.json','prepare-plan.json',
                'activate-plan.json','checkpoint-plan.json','checkpoint-result.json','network.json','preparation.json','request.json','authorization.json'})
            lease=stack.enter_context(Lease(slot.path/name,uid=0,gid=None,mode=0o644,parent=slot))
            raw=_read_complete(lease,c.LIMIT)
            if expected is not None:require(c.raw_digest(raw)==expected)
            held.append((lease,raw))
            return raw
        raw=read(path.name,request.get('plan_sha256'))
        plan=c.validate_plan(c.decode(raw),now=time.time())
        c.validate_request(request,plan,c.raw_digest(raw))
        def check():
            base.check();slot.check()
            for lease,data in held:
                lease.check();require(_read_complete(lease,c.LIMIT)==data)
            c.validate_plan(plan,now=time.time())
        check()
        yield plan,check,read
        check()


def authenticated(request,plan,*,script,executable):
    from desktop_assistant import current_mac_worker_contract as c
    from desktop_assistant import mac_keychain_install as boot,mac_system_service as system
    from desktop_assistant import mac_published_runtime as published,mac_smb_stream_runtime as stream
    from desktop_assistant import current_mac_target_audit as audit
    from app.core import mac_service_keychain as keychain
    _entry();require(not os.path.lexists(BASE/'factory-activation.json') and keychain.selected())
    c.validate_identity(boot._actual(),plan)
    context=keychain.load_context();require(context['uid']==499 and context['gid']==499)
    roles={}
    for role in ('execution','monitor','transport'):
        identity=plan[role+'_package_sha256'];meta=system.metadata(identity)
        require(meta['context']==context and all(meta[k]==plan[k] for k in
            ('context_sha256','publisher_sha256','registry_sha256')))
        tree,manifest=published.resolve(ROOT,identity,meta['public'],kind='migration')
        require(tree==meta['tree'])
        roles[role]=(meta,tree,manifest)
    require(len({roles[k][0]['public'] for k in roles})==1)
    selected='transport' if request['phase']=='update' or plan['type']==c.MOUNTED_PLAN else 'execution'
    meta,tree,manifest=roles[selected]
    caps=manifest.get('runtime_capabilities',{})
    require(type(caps.get(CAPABILITY)) is int and caps[CAPABILITY]==1
        and {MODULE,CONTRACT_MODULE,'desktop_assistant/manager.py',
             'desktop_assistant/mac_service_manage.py'}<=manifest.get('files',{}).keys()
        and Path(script).resolve()==tree/MODULE and Path(__file__).resolve()==tree/MODULE
        and Path(executable).resolve()==tree/'runtime/bin/python3.12'
        and Path(sys.executable).resolve()==tree/'runtime/bin/python3.12')
    for role in ('execution','monitor'):
        capabilities=roles[role][2].get('runtime_capabilities',{})
        require(type(capabilities.get(audit.EXECUTION_CAPABILITY)) is int
                and capabilities[audit.EXECUTION_CAPABILITY]==1)
    execution_caps=roles['execution'][2].get('runtime_capabilities',{})
    require(type(execution_caps.get(audit.CAPABILITY)) is int
            and execution_caps[audit.CAPABILITY]==1
            and audit.MODULE in roles['execution'][2].get('files',{}))
    from desktop_assistant.mac_service_restore import Lease,_read_complete
    with Lease(BASE/'home',uid=context['uid'],gid=context['gid'],mode=0o700,
               directory=True,mutable=True) as home:
        with Lease(home.path/('service-restore-'+plan['restore_attempt_id']+'.json'),
                uid=context['uid'],gid=context['gid'],mode=0o600,parent=home) as original:
            require(boot.digest(_read_complete(original,c.LIMIT))==plan['restore_receipt_sha256'])
    system.require_management(roles['monitor'][2])
    monitor=roles['monitor'][0]
    control=system.publication.read_record(system.CONTROL)
    plist=system._plist(plan['monitor_package_sha256'],plan['publisher_sha256'])
    require(control==boot.canonical(system.record_for(monitor,True))
        and boot.digest(control)==plan['system_control_sha256']
        and boot.digest(plist)==plan['system_plist_sha256'])
    for parent in (system.PLISTS,*system.PLISTS.parents):boot.protected(parent,directory=True)
    require(system.publication.read_record(system.PLIST)==plist
        and system.management_service_state()=={'registered':True,'enabled':True,'scheduled_backups':True})
    if plan['type']==c.MOUNTED_PLAN:
        from desktop_assistant import mounted_nas_proof as mounted
        require(type(caps.get(mounted.CAPABILITY)) is int and caps[mounted.CAPABILITY]==1
            and mounted.MODULE in manifest['files'])
        if request['phase']!='checkpoint':
            binding=mounted.load()
            require(binding['control']['package_sha256']==plan['transport_package_sha256']
                and binding['control']['context_sha256']==plan['context_sha256']
                and binding['control']['publisher_sha256']==plan['publisher_sha256']
                and binding['control_sha']==plan['transport_control_sha256']
                and binding['control']['contract_sha256']==plan['transport_contract_sha256']
                and binding['control']['receipt_sha256']==plan['backup_receipt_sha256']
                and binding['contract']['local_update_receipt_sha256']==plan['local_update_receipt_sha256'])
    else:
        binding=stream.load_binding();binding.check()
        require(binding.context==context and binding.control['package_sha256']==plan['transport_package_sha256']
            and binding.control_sha==plan['transport_control_sha256']
            and binding.control['contract_sha256']==plan['transport_contract_sha256'])
    return context,meta['public'],roles


def _reply(request,plan,result):
    from desktop_assistant import current_mac_worker_contract as c,mac_keychain_install as boot
    value=dict(type=c.REPLY,attempt_id=request['attempt_id'],phase=request['phase'],
        plan_sha256=request['plan_sha256'],challenge=request['challenge'],identity=boot._actual(),
        manual_stop=True,process_stopped=True,home_rehearsal=True,
        borrowed_lock=request['phase']=='activate',result=result)
    return c.validate_reply(value,request,plan,request['plan_sha256'])


def _update(request,plan,context,public,roles,recheck):
    from desktop_assistant import mac_service_manage as manage,mac_smb_stream_target as stream
    from desktop_assistant import mac_smb_stream_receipt as receipt,mac_keychain_install as boot
    from desktop_assistant.credential_store import unprotect_backup
    from desktop_assistant.manager import Manager
    with manage.instance(context) as (folders,lock,paths_check):
        root=folders['root']
        settings_raw,saved=manage.settings(root,context)
        destination=stream.approved_destination(context)
        require(saved.get('nas')==str(destination))
        result=[]
        def check():
            paths_check();recheck()
            require(manage.settings(root,context)[0]==settings_raw)
        class PlannedManager(Manager):
            @contextmanager
            def lock(self):
                # Preserve the original Manager.lock and update_stopped algorithm.
                with super().lock():
                    check()
                    before,state=manage.record(root,'state.json',context)
                    require(boot.digest(before)==plan['state_before_sha256']
                        and state.get('current')==plan['current_package_sha256'])
                    self._stopped_update_state()
                    yield
                    check()
                    after,final=manage.record(root,'state.json',context)
                    self._stopped_update_state()
                    require(final.get('current')==plan['execution_package_sha256']
                        and final.get('previous')==plan['current_package_sha256']
                        and final.get('operation')=='update'
                        and final.get('update_backup')==final.get('last_backup')
                        and receipt.verified_at(ROOT,final)==final.get('last_backup_at')
                        and final.get('last_backup_at') is not None)
                    index=final[receipt.INDEX]
                    backup=Path(index['path'])
                    require(backup.parent==ROOT/'backups' and backup==Path(final['last_backup']))
                    with receipt.copy_lease(root,context,backup.name) as retained:
                        retained.check();digest=hashlib.sha256();offset=0
                        while True:
                            chunk=os.pread(retained.fd,1024*1024,offset)
                            if not chunk:break
                            digest.update(chunk);offset+=len(chunk)
                        retained.check()
                        require(receipt.verified_at(ROOT,final)==final['last_backup_at'])
                        with receipt.receipts(root,context) as directory:
                            receipt_raw,proof=receipt.local._local_object(directory,index['receipt'],context['uid'])
                        require(boot.digest(receipt_raw)==index['receipt_sha256']
                            and proof['sha256']==digest.hexdigest() and proof['size']==offset)
                        payload=dict(state_before_sha256=boot.digest(before),state_after_sha256=boot.digest(after),
                            previous_package_sha256=state['current'],current_package_sha256=final['current'],
                            backup_sha256=digest.hexdigest(),backup_receipt_sha256=index['receipt_sha256'],
                            nas_independent_readback=True)
                        result.append(_reply(request,plan,payload))
                        check();retained.check();require(manage.record(root,'state.json',context)[0]==after)
        manager=PlannedManager(ROOT,public)
        package=roles['execution'][0]['tree'].parent/'package.zip'
        check()
        initial_raw,initial=manage.record(root,'state.json',context)
        require(boot.digest(initial_raw)==plan['state_before_sha256']
            and initial.get('current')==plan['current_package_sha256'])
        manager._stopped_update_state()
        password=unprotect_backup(saved['protected_password'])
        try:manager.update_stopped(package,password,destination)
        finally:del password
        require(len(result)==1)
        return result[0]


def _audit_arguments(plan):
    return {k:plan[k] for k in ('context_sha256','publisher_sha256','registry_sha256','restore_receipt_sha256')}


def run(request,*,script,executable,smb_broker_fd=None,mounted_fd=None):
    _entry()
    from desktop_assistant import current_mac_worker_contract as c,current_mac_cutover_contract as v3
    from desktop_assistant import current_mac_cutover as codec,current_mac_target_audit as audit
    from desktop_assistant import factory_network,mac_service_manage as manage
    from desktop_assistant.mac_system_upgrade_worker import _held_maintenance
    request=c.decode(c.canonical(request))
    with _plan(request) as (plan,plan_check,read):
        context,public,roles=authenticated(request,plan,script=script,executable=executable)
        def check():
            plan_check()
            actual=authenticated(request,plan,script=script,executable=executable)
            require(actual[:2]==(context,public))
        network_raw=read('network.json',plan['network_file_sha256'])
        network=factory_network.validate(c.decode(network_raw))
        require(v3.digest(network)==plan['network_sha256'])
        os.environ['ERP_HOME_REHEARSAL']='1'
        require(smb_broker_fd is None or request['phase']=='update')
        mounted_plan=plan['type']==c.MOUNTED_PLAN
        require((mounted_fd is not None)==(mounted_plan and request['phase']!='checkpoint'))
        audit_args=_audit_arguments(plan)
        if mounted_fd is not None:
            audit_args.update(mounted_fd=mounted_fd,audit_package_sha256=plan['transport_package_sha256'])
        if request['phase']=='update':
            if smb_broker_fd is None:
                # Internal legacy API only; real CLI below never permits this.
                result=_update(request,plan,context,public,roles,check)
            else:
                from desktop_assistant import current_mac_smb_broker as broker
                from desktop_assistant import mac_smb_stream_runtime as stream
                manifest=roles['transport'][2]
                require(type(manifest.get('runtime_capabilities',{}).get(broker.CAPABILITY)) is int
                    and manifest['runtime_capabilities'][broker.CAPABILITY]==1
                    and broker.MODULE in manifest['files'])
                bound=stream.load_binding();bound.check()
                require(bound.control_sha==plan['transport_control_sha256']
                    and bound.control['contract_sha256']==plan['transport_contract_sha256'])
                with broker.dedicated(smb_broker_fd,plan,request['plan_sha256'],request['challenge'],bound.value):
                    result=_update(request,plan,context,public,roles,check)
        elif request['phase']=='checkpoint':
            from desktop_assistant import mounted_nas_proof as mounted
            from desktop_assistant.credential_store import unprotect_backup
            from desktop_assistant.manager import Manager
            with manage.instance(context) as (folders,lock,paths_check), manage.maintenance(lock):
                settings_raw,saved=manage.settings(folders['root'],context)
                manager=Manager(ROOT,public)
                password=unprotect_backup(saved['protected_password'])
                def checkpoint_check():
                    paths_check();check()
                    require(manage.settings(folders['root'],context)[0]==settings_raw)
                try:
                    proof=mounted.build_checkpoint(manager,password,plan['state_before_sha256'],checkpoint_check)
                finally:del password
                result=_reply(request,plan,dict(checkpoint=proof))
        elif request['phase']=='prepare':
            if not mounted_plan:
                old_raw=read('update-plan.json')
                old_plan=c.validate_plan(c.decode(old_raw),now=time.time())
                previous_raw=read('update-result.json',plan['update_result_file_sha256'])
                previous=c.decode(previous_raw)
                old_request={k:previous[k] for k in ('attempt_id','phase','plan_sha256','challenge')}
                old_request.update(type=c.REQUEST,lock_fd=None)
                c.validate_update_link(previous,old_request,old_plan,c.raw_digest(old_raw),plan,
                                       update_result_raw=previous_raw)
            with audit.verified_current_mac_backup(plan['execution_package_sha256'],
                    plan['monitor_package_sha256'],plan['restore_attempt_id'],**audit_args) as (prep,audit_check):
                check()
                target=v3.make_request(prep,plan['network_sha256'],plan['publisher_sha256'],
                    request_id=plan['request_id'],now=datetime.now(timezone.utc))
                result=_reply(request,plan,dict(preparation=prep,request=target))
                require(audit_check()==prep);check()
        else:
            prep=c.decode(read('preparation.json',plan['preparation_file_sha256']))
            target=c.decode(read('request.json',plan['request_file_sha256']))
            authorization=read('authorization.json',plan['authorization_file_sha256'])
            require(v3.digest(prep)==plan['preparation_sha256'] and v3.digest(target)==plan['request_sha256'])
            body=codec.verify_authorization(authorization,public,target)
            require(body['preparation']==prep)
            codec.require_install_window(body,now=datetime.now(timezone.utc))
            with manage.instance(context,inherited_lock=request['lock_fd']) as (_,lock,paths_check), \
                    _held_maintenance(lock,borrowed=True):
                with audit.verified_current_mac_backup(plan['execution_package_sha256'],
                        plan['monitor_package_sha256'],plan['restore_attempt_id'],
                        inherited_lock=request['lock_fd'],expected_preparation=prep,
                        **audit_args) as (actual,audit_check):
                    paths_check();check();require(actual==prep)
                    now=datetime.now(timezone.utc)
                    codec.require_install_window(body,now=now)
                    record=v3.make_record(target,network,authorization,activated_at=now,
                        operator_attestation=plan['operator_attestation'])
                    result=_reply(request,plan,dict(record=record))
                    require(audit_check()==prep);paths_check();check()
                    codec.require_install_window(body,now=datetime.now(timezone.utc))
        check()
    return result


def _arguments(argv):
    from desktop_assistant import current_mac_worker_contract as c
    class SafeParser(argparse.ArgumentParser):
        def error(self,message):raise ValueError(FAILURE)
    parser=SafeParser(add_help=False,allow_abbrev=False)
    for name in ('attempt','plan-sha256','challenge','phase'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--lock-fd',type=int)
    require(len(argv) in (8,10) and len(set(argv[::2]))==len(argv[::2]))
    args=parser.parse_args(argv)
    return dict(type=c.REQUEST,attempt_id=args.attempt,plan_sha256=args.plan_sha256,
                challenge=args.challenge,phase=args.phase,lock_fd=args.lock_fd)


def main():
    _entry();os.umask(0o077)
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    from desktop_assistant import current_mac_worker_contract as c
    argv=list(sys.argv[1:]);mounted_fd=None
    if '--mounted-fd' in argv:
        require(argv.count('--mounted-fd')==1)
        index=argv.index('--mounted-fd')
        require(index%2==0 and index+1<len(argv) and argv[index+1].isascii() and argv[index+1].isdecimal())
        mounted_fd=int(argv[index+1]);require(3<=mounted_fd<=2**31-1)
        del argv[index:index+2]
    request,broker_fd=_launch_arguments(argv)
    result=run(request,script=__file__,executable=sys.executable,smb_broker_fd=broker_fd,mounted_fd=mounted_fd)
    raw=c.canonical(result)
    sys.stdout.buffer.write(raw+b'\n');sys.stdout.buffer.flush()


def _launch_arguments(argv):
    argv=list(argv);fd=None
    if '--smb-broker-fd' in argv:
        require(argv.count('--smb-broker-fd')==1)
        index=argv.index('--smb-broker-fd')
        require(index%2==0 and index+1<len(argv) and argv[index+1].isascii() and argv[index+1].isdecimal())
        fd=int(argv[index+1]);require(3<=fd<=2**31-1)
        del argv[index:index+2]
    request=_arguments(argv)
    require((request['phase']=='update')==(fd is not None))
    return request,fd


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt):raise SystemExit(FAILURE) from None

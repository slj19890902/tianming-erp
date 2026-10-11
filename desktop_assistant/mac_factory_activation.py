"""Pinned service-UID proof worker. Never publishes activation or starts ERP."""
from datetime import datetime
from pathlib import Path
import sys

from desktop_assistant import mac_keychain_install as boot, mac_system_service as system
from desktop_assistant import mac_published_runtime as published, mac_service_manage as manage
from desktop_assistant import factory_target_audit as audit, factory_activation_contract as contract
from app.core import mac_service_keychain as keychain


def run(request, *, script, executable):
    if (not isinstance(request, dict) or set(request) != {'package_sha256', 'request_id', 'input_attempt',
            'authorization_sha256', 'context_sha256', 'registry_sha256', 'challenge', 'lock_fd'}
            or not sys.flags.isolated or not sys.dont_write_bytecode or not keychain.selected()
            or type(request['lock_fd']) is not int or request['lock_fd'] < 3):
        raise ValueError('激活核验需要隔离专用身份及继承原维护锁')
    for name in ('request_id','input_attempt','challenge'): audit.protocol._id(request[name])
    for name in ('package_sha256','authorization_sha256','context_sha256','registry_sha256'): contract._hash(request[name])
    context=keychain.load_context()
    if boot._actual()!=boot._expected(context['uid'],context['gid']): raise ValueError('激活核验身份不符')
    meta=system.metadata(request['package_sha256'])
    if (meta['context']!=context or any(meta[k]!=request[k] for k in ('context_sha256','registry_sha256'))):
        raise ValueError('激活核验的root批准身份已变化')
    tree,manifest=published.resolve(manage.ROOT,request['package_sha256'],meta['public'],kind='migration')
    contract.require_protocol(manifest)
    if (Path(script).resolve()!=tree/contract.ENTRY or Path(__file__).resolve()!=tree/contract.MODULE
            or Path(executable).resolve()!=tree/'runtime/bin/python3.12'):
        raise ValueError('激活核验不是正在执行的批准程序')
    with audit.verified_restored_target(request['request_id'],request['input_attempt'],
            request['authorization_sha256'],inherited_lock=request['lock_fd']) as (evidence,recheck):
        service=system.management_service_state()
        if boot.canonical(service)!=boot.canonical({'registered':True,'enabled':True,'scheduled_backups':True}):
            raise ValueError('先完成支持系统备份的监护安装与启用')
        system.verify_keychain(context)
        from desktop_assistant.storage import read_json
        state=read_json(manage.ROOT/'state.json')
        when=manage.remote.verified_at(manage.ROOT,state)
        if not isinstance(when,str): raise ValueError('当前目标缺少已独立验证的Mac完整备份')
        when=datetime.fromisoformat(when); restored=datetime.fromisoformat(state['restored_at'])
        if when.utcoffset() is None or restored.utcoffset() is None or when<restored:
            raise ValueError('Mac完整备份早于本次最终恢复')
        request_path=manage.BASE/('factory-target-'+request['request_id']+'.json')
        network_path=manage.BASE/('factory-network-'+request['request_id']+'.json')
        request_raw=published._read(request_path);network_raw=published._read(network_path)
        authorization=(manage.BASE/'restore-inputs'/request['input_attempt']/'authorization.json').read_bytes()
        if (boot.digest(request_raw)!=evidence['request_sha256']
                or boot.digest(authorization)!=request['authorization_sha256']): raise ValueError('已核验输入变化')
        record={'type':contract.TYPE,'request':boot.decode(request_raw),'network':boot.decode(network_raw),
            'authorization':authorization.decode('utf-8'),'authorization_sha256':request['authorization_sha256'],
            'context_sha256':request['context_sha256'],'registry_sha256':request['registry_sha256'],
            'activated_at':contract._stamp(contract._now()),'target_snapshot':dict(evidence['target_snapshot']),
            'operator_attestation':dict(contract.ATTESTATION)}
        recheck()
        result={'type':'tianming.factory-activation-check.v1','challenge':request['challenge'],
            'identity':boot._actual(),'manual_stop':True,'process_stopped':True,
            'keychain_canary_verified':True,'system_ready':True,'factory_activated':False,
            'record':record,'mac_backup_verified_at':contract._stamp(when)}
        contract.reply(result,request,context)
    # The audit's final checks have completed. The administrator still owns the
    # same maintenance lock after this context and after this process exits.
    return result


def main(*, script, executable):
    request=boot.decode(sys.stdin.buffer.read(boot.LIMIT+1))
    result=run(request,script=script,executable=executable)
    sys.stdout.buffer.write(boot.canonical(result)+b'\n');sys.stdout.buffer.flush()

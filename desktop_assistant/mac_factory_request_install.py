"""Public target/network request records only; stdlib root dispatcher.

Never imports ERP/runtime dependencies or promotes configuration to active use.
The two create-only files are paired by request_id and a canonical network hash.
A retained network file without its final request is an incomplete attempt.
"""
import os
from pathlib import Path
import stat
import subprocess
import uuid

from desktop_assistant import mac_keychain_install as boot, mac_system_service as system
from desktop_assistant import mac_release_publish as publication
from desktop_assistant import factory_target_contract as contract, factory_network as network

ENTRY = 'desktop_assistant/factory_request_entry.py'
MODULE = 'desktop_assistant/mac_factory_request.py'


def require_protocol(manifest):
    if (type(manifest.get('runtime_capabilities', {}).get('factory_request_v1')) is not int
            or manifest['runtime_capabilities']['factory_request_v1'] != 1
            or type(manifest.get('reader_capabilities', {}).get('factory_cutover_v1')) is not int
            or manifest['reader_capabilities']['factory_cutover_v1'] != 1
            or not {ENTRY,MODULE,'desktop_assistant/factory_network.py',
                    'desktop_assistant/factory_target_contract.py'} <= manifest.get('files', {}).keys()):
        raise ValueError('批准程序尚未实现正式切换及目标申请协议')


def read_network(path, approved_sha):
    contract._hash(approved_sha)
    # This file is untrusted public data. Independently approved raw SHA, strict
    # fields and fixed semantics are required before starting any child process.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= boot.LIMIT:
            raise ValueError('网络提案必须是有界普通文件')
        chunks=[];remaining=boot.LIMIT+1
        while remaining:
            block=os.read(fd,remaining)
            if not block:break
            chunks.append(block);remaining-=len(block)
        raw=b''.join(chunks);after=os.fstat(fd)
        fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        if (tuple(getattr(before,k) for k in fields) != tuple(getattr(after,k) for k in fields)
                or len(raw) != before.st_size or boot.digest(raw) != approved_sha):
            raise ValueError('网络提案读取时变化或不是已审阅原字节')
        return network.validate(boot.decode(raw))
    finally:os.close(fd)


def expected_reply(value, context):
    return {'type':'tianming.factory-target-check.v1', 'challenge':value['challenge'],
            'request_sha256':contract.digest(value['request']),
            'identity':boot._expected(context['uid'],context['gid']),
            'manual_stop':True, 'process_stopped':True, 'factory_activated':False}


def prepare(package, proposed):
    identity,_ = boot.modules();identity._administrator();boot.verify_code()
    proposed = network.validate(proposed)
    with publication.lock():
        meta = system.metadata(package);require_protocol(system._program_manifest(meta))
        context=meta['context']
        target={'host_sha256':contract.host_hash(),'installation_id':context['installation_id'],
                'service_uid':context['uid'],'service_gid':context['gid'],
                'service_account':contract.SERVICE_ACCOUNT,'root':str(system.ROOT)}
        request=contract.make_request(target,package,contract.digest(proposed),meta['publisher_sha256'])
        value={'request':request,'network':proposed,'challenge':uuid.uuid4().hex,
               **{k:meta[k] for k in ('context_sha256','registry_sha256')}}
        process=subprocess.Popen([str(meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(meta['tree']/ENTRY)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,user=context['uid'],
            group=context['gid'],extra_groups=[],close_fds=True,pass_fds=(),umask=0o077,cwd='/',
            env={'HOME':str(system.BASE/'home'),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
                 'LANG':'en_US.UTF-8','ERP_HOME_REHEARSAL':'1'})
        interrupted=False;first=True
        while True:
            try: output,_=process.communicate(boot.canonical(value) if first else None);break
            except KeyboardInterrupt:interrupted=True;first=False
        if (interrupted or process.returncode != 0
                or boot.canonical(boot.decode(output)) != boot.canonical(expected_reply(value,context))):
            raise ValueError('专用目标申请核验未通过，未保存有效申请')
        latest=system.metadata(package)
        if any(latest[k] != meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')):
            raise ValueError('申请期间root批准身份变化')
        now=contract._now()
        if not contract._time(request['created_at']) <= now < contract._time(request['expires_at']):
            raise ValueError('申请检查已超时，未保存有效申请')
        network_path=system.BASE/('factory-network-'+request['request_id']+'.json')
        request_path=system.BASE/('factory-target-'+request['request_id']+'.json')
        # Never overwrite an old request/configuration, even after expiry.
        for path in (network_path,request_path):
            if path.exists() or path.is_symlink():raise ValueError('本次申请记录已存在')
        network_raw=contract.canonical(proposed);request_raw=contract.canonical(request)
        boot._write(network_path,network_raw,mode=0o644)
        publication.sync_directory(system.BASE)
        boot._write(request_path,request_raw,mode=0o644)  # Published last.
        publication.sync_directory(system.BASE)
        if publication.read_record(network_path)!=network_raw or publication.read_record(request_path)!=request_raw:
            raise ValueError('申请已写入但回读未确认，保留现场')
        return {'type':'tianming.factory-target-result.v1','request_path':str(request_path),
                'request_sha256':boot.digest(request_raw),'network_path':str(network_path),
                'network_sha256':boot.digest(network_raw),'package_sha256':package,
                'created_at':request['created_at'],'expires_at':request['expires_at'],
                'factory_activated':False,'runtime_configuration_changed':False,
                'network_reachability_verified':False,'final_backup_verified':False,
                'credentials_verified':False,'source_isolation_verified':False}

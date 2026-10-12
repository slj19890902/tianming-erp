"""Integration of fixed root collector and original stream hook.

Real child, deny-all sandbox and loopback TCP; root/UID/metadata are adapters.
No actual NAS, operational secret or authentic full business-backup claim.
"""
from copy import deepcopy
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import current_mac_dispatch as d,current_mac_worker as w
from desktop_assistant import current_mac_smb_broker as b,current_mac_worker_contract as c
from desktop_assistant import mac_smb_stream_runtime as runtime
from tests.desktop_assistant.test_current_mac_dispatch import scene,chain

REAL_POPEN=subprocess.Popen


@pytest.mark.parametrize('phase',['prepare','activate'])
def test_broker_fd_only_in_update_launch(phase):
    argv=['--attempt','a'*32,'--plan-sha256','b'*64,'--challenge','c'*32,'--phase',phase]
    with pytest.raises(ValueError):w._launch_arguments(argv+['--smb-broker-fd','8'])
    assert w._launch_arguments(argv)[1] is None


@pytest.mark.parametrize('suffix',[[],['--smb-broker-fd','2'],['--smb-broker-fd','-1'],
    ['--smb-broker-fd','8','--smb-broker-fd','9'],['--smb-broker-fd','８']])
def test_update_launch_requires_one_valid_inherited_fd(suffix):
    argv=['--attempt','a'*32,'--plan-sha256','b'*64,'--challenge','c'*32,'--phase','update']
    with pytest.raises(ValueError):w._launch_arguments(argv+suffix)
    request,fd=w._launch_arguments(argv+['--smb-broker-fd','8'])
    assert fd==8 and request['phase']=='update' and request['lock_fd'] is None


def test_stream_failure_does_not_fall_back_to_direct_socket(monkeypatch):
    binding=SimpleNamespace(check=Mock())
    monkeypatch.setattr(b,'connection',Mock(side_effect=ValueError('synthetic broker refused')))
    create=Mock(side_effect=AssertionError('direct socket fallback'))
    monkeypatch.setattr(runtime.socket,'socket',create)
    with pytest.raises(ValueError):runtime._dispatch(binding,{'action':'probe'})
    assert not create.called


@pytest.mark.skipif(sys.platform!='darwin',reason='native sandbox integration')
@pytest.mark.parametrize('fault',['none','incomplete','bad_message'])
def test_update_collector_serves_only_fixed_channels_and_waits_for_child(scene,monkeypatch,fault):
    s=scene;plan=s.plans['update'];listener=socket.socket()
    listener.bind(('127.0.0.1',0));listener.listen(3);listener.settimeout(3)
    contract=dict(type=b.archive.TYPE,context_sha256=plan['context_sha256'],uid=499,
        server_address='127.0.0.1',port=listener.getsockname()[1],username='synthetic',share='synthetic',
        relative_directory=b.archive.DIRECTORY,server_guid='a'*32,protection='smb311-session-aes-gcm')
    # Endpoint/root adapters are confined to this synthetic OS test.
    monkeypatch.setattr(b.archive,'validate',lambda value,context,uid:value)
    monkeypatch.setattr(b,'_administrator',lambda:None)
    cfg=d.stream;contract_sha=cfg.archive.digest(contract)
    control=dict(type=cfg.TYPE,attempt_id='d'*32,plan_sha256='e'*64,
        package_sha256=plan['transport_package_sha256'],context_sha256=plan['context_sha256'],
        publisher_sha256=plan['publisher_sha256'],contract_sha256=contract_sha,
        credential_account=cfg.account(plan['context_sha256'],contract_sha,'d'*32))
    for field,name,value in [('CONTROL','smb-stream-service.json',control),('CONTRACT','smb-stream-archive.json',contract)]:
        path=s.base/name;path.write_bytes(cfg.canonical(value));path.chmod(0o644);monkeypatch.setattr(cfg,field,path)
    plan.update(transport_control_sha256=c.raw_digest(cfg.canonical(control)),transport_contract_sha256=contract_sha)
    raw=c.canonical(plan);path=s.slot/'update-plan.json';path.write_bytes(raw)
    s.manifest['runtime_capabilities'][b.CAPABILITY]=1
    s.manifest['files'].update({b.MODULE:'synthetic','desktop_assistant/mac_smb_stream_runtime.py':'synthetic'})
    processes=[];errors=[]
    expected_connections=3 if fault=='none' else 0
    def echo():
        try:
            for action in b.ORDER[:expected_connections]:
                incoming,_=listener.accept()
                with incoming:
                    incoming.sendall(b'synthetic');incoming.settimeout(3)
                    assert incoming.recv(32)==action.encode()
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=echo);thread.start()
    program='''import json,os,socket,sys
sys.path.insert(0,sys.argv[1])
from desktop_assistant import current_mac_smb_broker as b
from desktop_assistant import current_mac_worker_contract as c
plan=json.loads(sys.argv[2]);contract=json.loads(sys.argv[3]);reply=json.loads(sys.argv[4])
fd=int(sys.argv[5]);challenge=sys.argv[6];fault=sys.argv[7]
channel=socket.socket(fileno=fd)
assert b._peer(channel)==(os.getuid(),os.getgid())
channel.detach()
b._peer=lambda channel:(0,0)
b.archive.validate=lambda value,context,uid:value
if fault=='incomplete':os.close(fd)
elif fault=='bad_message':
    with socket.socket(fileno=fd) as channel:channel.sendall(b'bad frame')
else:
    with b.dedicated(fd,plan,c.raw_digest(c.canonical(plan)),challenge,contract):
        binding=type('Binding',(),dict(control_sha=plan['transport_control_sha256'],
            control=dict(contract_sha256=plan['transport_contract_sha256']),value=contract))()
        for action in b.ORDER:
            with b.connection(binding,action) as connected:
                assert connected.recv(32)==b'synthetic'
                connected.sendall(action.encode())
reply['challenge']=challenge;reply['plan_sha256']=c.raw_digest(c.canonical(plan))
print(c.canonical(reply).decode())
'''
    def launch(argv,**options):
        assert argv[:3]==['/usr/bin/sandbox-exec','-p','(version 1)(allow default)(deny network-outbound)']
        assert options['user']==options['group']==499 and options['extra_groups']==[]
        fd=argv[argv.index('--smb-broker-fd')+1]
        assert options['pass_fds']==(int(fd),)
        # Actual personal-user child replaces privileged identity/executable only.
        child=REAL_POPEN(argv[:3]+[sys.executable,'-I','-B','-c',program,
            str(Path(b.__file__).resolve().parent.parent),raw.decode(),cfg.canonical(contract).decode(),
            c.canonical(s.replies['update']).decode(),fd,argv[argv.index('--challenge')+1],fault],
            stdin=options['stdin'],stdout=options['stdout'],stderr=options['stderr'],
            close_fds=True,pass_fds=options['pass_fds'],cwd='/',env=options['env'])
        processes.append(child);return child
    monkeypatch.setattr(d.subprocess,'Popen',launch)
    try:
        if fault=='none':assert d.dispatch(raw,verify_code=s.verifier)['result']==s.replies['update']['result']
        else:
            with pytest.raises(ValueError):d.dispatch(raw,verify_code=s.verifier)
        assert len(processes)==1 and processes[0].poll() is not None
        thread.join(timeout=4);assert not thread.is_alive() and not errors
        assert not (s.base/'factory-activation.json').exists()
    finally:
        listener.close()
        for child in processes:
            if child.poll() is None:child.kill();child.wait()  # Owned synthetic test only.

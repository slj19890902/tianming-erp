"""Internal root dispatch for fixed current-Mac update and audit phases.

Not an installer or executable entry point. The future root transaction must
authenticate a closed, independently SHA-approved code pack before import, and
provide its ongoing verifier. This verifier is not itself external approval.
No root record is written or ERP started. Update grants exactly the approved
NAS connections over inherited descriptors; every child denies new outbound.
"""
from contextlib import ExitStack
import errno
import fcntl
import os
from pathlib import Path
import selectors
import subprocess
import socket
import time
import uuid

from desktop_assistant import current_mac_worker_contract as c
from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_system_service as system
from desktop_assistant import mac_system_upgrade as root
from desktop_assistant.home_process import command
from desktop_assistant import current_mac_smb_broker as smb_broker
from desktop_assistant import mac_smb_stream_runtime_contract as stream

BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
WORKER = 'desktop_assistant/current_mac_worker.py'
CAPABILITY = 'current_mac_worker_v1'
ERROR = 'Current Mac audit dispatch unconfirmed; preserve scene; no retry or start'


def require(ok):
    if not ok:
        raise ValueError(ERROR)


def _collect(process,broker=None):
    """Drain both bounded pipes and await this child, even after interruption.

    The caller retains its borrowed lock throughout. A timeout is not treated
    as an exit; no signals, restart or cleanup are issued here.
    """
    chunks, size, invalid, interrupted = [], 0, False, False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            if broker is not None:selector.register(broker.channel,selectors.EVENT_READ,'broker')
            while selector.get_map():
                try:
                    for key, _ in selector.select(0.25):
                        if key.data=='broker':
                            try:
                                chunk=broker.channel.recv(smb_broker.LIMIT+4)
                                if chunk:broker.feed(chunk)
                                else:
                                    selector.unregister(key.fd);broker.channel.close()
                            except (Exception,KeyboardInterrupt):
                                invalid=True
                                selector.unregister(key.fd);broker.channel.close()
                            continue
                        data = os.read(key.fileobj.fileno(), 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        size += len(data)
                        if size > c.LIMIT or not key.data:
                            invalid = True
                        elif not invalid:
                            chunks.append(data)
                except KeyboardInterrupt:
                    interrupted = True
    finally:
        if broker is not None:broker.channel.close()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
        while True:
            try:
                code = process.wait()
                break
            except KeyboardInterrupt:
                interrupted = True
    require(code == 0 and not invalid and not interrupted)
    if broker is not None:broker.finish()
    return b''.join(chunks)


def _maintenance(stack, base, inherited_fd):
    """Prove the supplied FD is the original read-only, already-held lock."""
    require(type(inherited_fd) is int and 3 <= inherited_fd <= c.MAX_FD)
    instance = stack.enter_context(root.Pin(ROOT, uid=499, gid=499,
        mode=0o700, directory=True, mutable=True, parent=base))
    control = stack.enter_context(root.Pin(ROOT / 'control', uid=499, gid=499,
        mode=0o700, directory=True, mutable=True, parent=instance))
    pin = stack.enter_context(root.Pin(control.path / 'operation.lock', uid=499,
        gid=499, mode=0o600, parent=control))
    before = root._fingerprint(os.fstat(inherited_fd))

    def check():
        require(pin.read() == b'0'
            and root._fingerprint(os.fstat(inherited_fd)) == before
            and before == root._fingerprint(pin.check())
            and fcntl.fcntl(inherited_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
        try:
            fcntl.flock(pin.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            require(error.errno in (errno.EAGAIN, errno.EWOULDBLOCK))
        else:
            fcntl.flock(pin.fd, fcntl.LOCK_UN)
            raise ValueError(ERROR)
    check()
    return check


def dispatch(plan_raw, *, verify_code, inherited_lock=None):
    """Return a validated phase reply; keep activation's parent lock held.

    verify_code must reauthenticate the complete fixed administrator pack and
    return its raw manifest SHA. No user-selected executable, path, environment
    or secret is accepted. All files are pinned through the child's actual exit.
    """
    root._entry()
    plan = c.validate_plan(c.decode(plan_raw), now=time.time())
    require(callable(verify_code))
    require((plan['phase'] == 'activate') == (inherited_lock is not None))
    plan_sha = c.raw_digest(plan_raw)
    request = dict(type=c.REQUEST, attempt_id=plan['attempt_id'], phase=plan['phase'],
        plan_sha256=plan_sha, challenge=uuid.uuid4().hex, lock_fd=inherited_lock)
    c.validate_request(request, plan, plan_sha)
    path = Path(c.plan_path(plan['attempt_id'], plan['phase']))
    require(path == BASE / ('current-mac-cutover-' + plan['attempt_id']) /
        (plan['phase'] + '-plan.json'))
    with ExitStack() as stack:
        base = stack.enter_context(root.Pin(BASE, mode=0o755, directory=True, mutable=True))
        slot = stack.enter_context(root.Pin(path.parent, mode=0o755, directory=True, parent=base))
        saved = stack.enter_context(root.Pin(path, mode=0o644, parent=slot))
        require(saved.read() == plan_raw)
        lock_check = _maintenance(stack, base, inherited_lock) if inherited_lock is not None else lambda: None
        selected_role='transport' if plan['phase']=='update' else 'execution'
        selected = system.metadata(plan[selected_role+'_package_sha256'])
        manifest = system._program_manifest(selected)
        require(type(manifest.get('runtime_capabilities', {}).get(CAPABILITY)) is int
            and manifest['runtime_capabilities'][CAPABILITY] == 1
            and {WORKER, 'desktop_assistant/current_mac_worker_contract.py'} <= manifest.get('files', {}).keys())
        tree = selected['tree']
        extra=[];contract=None
        if plan['phase']=='update':
            require(plan_raw==c.canonical(plan)
                and type(manifest.get('runtime_capabilities',{}).get(smb_broker.CAPABILITY)) is int
                and manifest['runtime_capabilities'][smb_broker.CAPABILITY]==1
                and {smb_broker.MODULE,'desktop_assistant/mac_smb_stream_runtime.py'} <= manifest['files'].keys()
                and stream.CONTROL==BASE/'smb-stream-service.json' and stream.CONTRACT==BASE/'smb-stream-archive.json')
            for path in (stream.CONTROL,stream.CONTRACT):
                pin=stack.enter_context(root.Pin(path,mode=0o644,parent=base));extra.append((pin,pin.read()))
            control=c.decode(extra[0][1]);contract=c.decode(extra[1][1])
            stream.validate(control,contract,plan['context_sha256'],499)
            require(c.raw_digest(extra[0][1])==plan['transport_control_sha256']
                and stream.archive.digest(contract)==plan['transport_contract_sha256']
                and control['package_sha256']==plan['transport_package_sha256']
                and control['publisher_sha256']==plan['publisher_sha256'])

        def check():
            base.check(); slot.check(); lock_check()
            require(saved.read() == plan_raw and verify_code() == plan['code_manifest_sha256']
                and not os.path.lexists(BASE / 'factory-activation.json'))
            c.validate_plan(plan, now=time.time())
            for pin,data in extra:require(pin.read()==data)
            for role in ('execution', 'monitor', 'transport'):
                meta = system.metadata(plan[role + '_package_sha256'])
                require(all(meta[key] == plan[key] for key in
                    ('context_sha256', 'publisher_sha256', 'registry_sha256'))
                    and meta['context']['uid'] == 499 and meta['context']['gid'] == 499
                    and meta['public'] == selected['public'])
                if role == selected_role:
                    require(meta['tree'] == tree and system._program_manifest(meta) == manifest)
        check()
        environment = dict(HOME=str(BASE / 'home'), PATH='/usr/bin:/bin:/usr/sbin:/sbin',
            LANG='en_US.UTF-8', ERP_HOME_REHEARSAL='1')
        arguments = [str(tree / 'runtime/bin/python3.12'), '-I', '-B', str(tree / WORKER),
            '--attempt', plan['attempt_id'], '--plan-sha256', plan_sha,
            '--challenge', request['challenge'], '--phase', plan['phase']]
        if inherited_lock is not None:
            arguments += ['--lock-fd', str(inherited_lock)]
        parent=child=None;broker=None
        inherited=() if inherited_lock is None else (inherited_lock,)
        if plan['phase']=='update':
            parent,child=socket.socketpair()
            stack.callback(parent.close);stack.callback(child.close)
            broker=smb_broker.Server(parent,plan,plan_sha,request['challenge'],contract,check)
            arguments+=['--smb-broker-fd',str(child.fileno())];inherited=(child.fileno(),)
        process = subprocess.Popen(command(arguments, environment), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, user=499, group=499, extra_groups=[],
            close_fds=True, pass_fds=inherited,
            umask=0o077, cwd='/', env=environment)
        if child is not None:child.close()
        raw = _collect(process,broker)
        check()
        reply = c.validate_reply(c.decode(raw), request, plan, plan_sha)
        check()
        return reply

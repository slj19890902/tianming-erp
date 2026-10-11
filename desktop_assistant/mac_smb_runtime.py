"""One approved service-UID SMB connection, launched with NAS-only egress.

No password prompt, credential writes, remote directory creation, retries or
unmounts. An uncertain native result preserves the scene. Root provisioning
must have already installed the public binding, lock and separate NAS secret.

Candidate only: the current macOS parser rejects its exact-NAS network rule.
The parent checks that prerequisite before dispatch; build does not advertise
archive_smb_v1. Protocol tests simulate policy acceptance and are not deployment
evidence. Do not provision this worker until a real address boundary is proven.
"""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

from app.core import mac_service_keychain as keychain
from desktop_assistant import mac_keychain_install as boot, mac_service_identity as identity
from desktop_assistant import mac_system_service as service, mac_release_publish as publication
from desktop_assistant import mac_published_runtime as published, mac_mount as mounts
from desktop_assistant import mac_smb_runtime_contract as contract, mac_smb_contract as cfg
from desktop_assistant import mac_smb_client as client, mac_smb_storage as storage

LOCK = contract.BASE/'home/smb-connection.lock'
RESULT = 'tianming.mac-smb-connect-result.v1'
LIMIT = 8192


def require(ok):
    if not ok: raise ValueError(contract.ERROR)


def actual_identity(context):
    actual = boot._actual()
    require(all(actual[k] == context['uid' if k in ('uid','euid') else 'gid']
                for k in ('uid','euid','gid','egid')))
    require(isinstance(actual['kernel_groups'], list)
            and all(type(group) is int for group in actual['kernel_groups'])
            and context['gid'] in actual['kernel_groups']
            and set(actual['kernel_groups']) <= identity.permitted_groups(context['gid']))
    return actual


class Binding:
    def __init__(self, control, value, context, meta, records):
        self.control, self.value, self.context = deepcopy(control), deepcopy(value), deepcopy(context)
        self.tree = meta['tree']
        self._records = dict(records)
        self._identity = actual_identity(context)

    def check(self):
        require(self.control == boot.decode(self._records[contract.CONTROL])
                and self.value == boot.decode(self._records[contract.CONTRACT]))
        require(keychain.load_context() == self.context and actual_identity(self.context) == self._identity)
        require(all(publication.read_record(path) == raw for path, raw in self._records.items()))
        require(self.value['server_address'] not in mounts.local_addresses())

    @property
    def control_sha(self): return contract.digest(self._records[contract.CONTROL])


def load_binding():
    require(sys.platform == 'darwin' and keychain.selected())
    context = keychain.load_context(); actual_identity(context)
    records = {p:publication.read_record(p) for p in (contract.CONTROL, contract.CONTRACT, published.REGISTRY)}
    control, value = (boot.decode(records[p]) for p in (contract.CONTROL, contract.CONTRACT))
    require(contract.canonical(control) == records[contract.CONTROL]
            and contract.canonical(value) == records[contract.CONTRACT])
    meta = service.metadata(control.get('package_sha256'))
    contract.validate(control, value, meta['context_sha256'], context['uid'])
    require(context == meta['context'] and control['publisher_sha256'] == meta['publisher_sha256']
            and contract.digest(records[published.REGISTRY]) == meta['registry_sha256'])
    tree, manifest = published.resolve(contract.BASE/'instance', control['package_sha256'], meta['public'], kind='migration')
    require(tree == meta['tree'])
    contract.capability(manifest)
    result = Binding(control, value, context, meta, records)
    result.check()
    return result


@contextmanager
def connection_lock(context):
    """Existing installed lock only; pin its parent and never unlink/recreate."""
    import fcntl
    with keychain._checked_path(LOCK.parent, uid=context['uid'], directory=True, mode=0o700) as parent:
        fd = os.open(LOCK.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            original = os.fstat(fd)
            def check():
                current = os.fstat(fd); named = os.stat(LOCK.name, dir_fd=parent, follow_symlinks=False)
                require(stat.S_ISREG(current.st_mode) and stat.S_IMODE(current.st_mode) == 0o600
                        and current.st_uid == context['uid'] and current.st_gid == context['gid']
                        and current.st_nlink == 1 and current.st_size == 1
                        and (current.st_dev,current.st_ino) == (original.st_dev,original.st_ino)
                        == (named.st_dev,named.st_ino))
                keychain._no_acl(fd)
                require(os.pread(fd, 2, 0) == b'0')
            check()
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('已有专用SMB连接检查正在进行') from None
            try:
                check(); yield; check()
            finally: fcntl.flock(fd, fcntl.LOCK_UN)
        finally: os.close(fd)


def mounted(binding):
    kernel = mounts._select(cfg.MOUNT)
    if cfg.kernel_matches(binding.value, kernel): return True
    # No secret read for an existing foreign volume or autofs/personal proxy.
    require(kernel['filesystem'] == 'apfs' and kernel['flags'] & 0x1000
            and kernel['mountpoint'] != str(cfg.MOUNT))
    return False


def observed(binding):
    with storage.open_location(binding.value, context_sha=binding.control['context_sha256'],
                               uid=binding.context['uid']) as held:
        result = (held.check(), held.directory_identity)
        binding.check()
        return result


def connect(binding):
    """Called only inside the fixed sandbox-exec worker command below."""
    with connection_lock(binding.context):
        binding.check()
        secret_read = False
        if not mounted(binding):
            with client.empty_mountpoint(binding.context['uid']) as (unmounted, parents):
                binding.check(); unmounted()
                store = keychain.ServiceStore()
                require(store.value == binding.context)
                raw = password = None
                try:
                    raw = store.get(binding.control['credential_account'])
                    require(isinstance(raw, bytes) and 0 < len(raw) <= 128 and b'\0' not in raw)
                    password = raw.decode('utf-8', 'strict')
                    secret_read = True
                    binding.check(); unmounted()
                    client.mount(binding.value, password, context_sha=binding.control['context_sha256'],
                                 uid=binding.context['uid'])
                finally:
                    raw = password = None
                parents()
        mount, directories = observed(binding)
        binding.check()
        return {'mount':mount, 'directory_identity':directories, 'credential_read':secret_read}


def _request(request, binding):
    require(isinstance(request, dict) and set(request) == {'challenge','control_sha256','contract_sha256'}
            and isinstance(request['challenge'], str) and re.fullmatch('[0-9a-f]{32}',request['challenge'])
            and request['control_sha256'] == binding.control_sha
            and request['contract_sha256'] == binding.control['contract_sha256'])


def run(request, *, script, executable):
    require(sys.flags.isolated and sys.dont_write_bytecode)
    binding = load_binding(); _request(request, binding)
    require(Path(script).resolve() == binding.tree/contract.ENTRY
            and Path(__file__).resolve() == binding.tree/contract.MODULE
            and Path(executable).resolve() == binding.tree/'runtime/bin/python3.12')
    result = connect(binding)
    return {'type':RESULT, **request, 'package_sha256':binding.control['package_sha256'],
            'context_sha256':binding.control['context_sha256'], 'identity':actual_identity(binding.context), **result}


def command(binding):
    return ['/usr/bin/sandbox-exec', '-p', contract.profile(binding.value, binding.control['context_sha256'],
                binding.context['uid']), str(binding.tree/'runtime/bin/python3.12'), '-I', '-B',
            str(binding.tree/contract.ENTRY)]


def verify_network_policy(binding):
    """Fail before the secret-reading worker if this OS rejects the profile.

    Current macOS rejects literal NAS addresses in remote-ip rules. Keep that
    as a blocking deployment prerequisite; never replace it with a wildcard
    address or silently run the worker without its network restriction.
    """
    probe = subprocess.run(command(binding)[:3]+['/usr/bin/true'], cwd='/',
        env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin'}, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, close_fds=True, timeout=10)
    if probe.returncode != 0:
        raise ValueError('当前系统未接受NAS精确地址网络隔离；未启动凭据读取或连接，不可部署SMB后台入口')


def ensure_destination(path):
    binding = load_binding()
    require(Path(path) == cfg.MOUNT/cfg.RELATIVE)
    if mounted(binding):
        observed(binding)
        return
    request = {'challenge':uuid.uuid4().hex, 'control_sha256':binding.control_sha,
               'contract_sha256':binding.control['contract_sha256']}
    # An already mounted, valid volume takes no secret-reading child. An empty
    # mountpoint is checked here and independently by the locked child.
    with client.empty_mountpoint(binding.context['uid']) as (unmounted, parents):
        binding.check(); unmounted()
        verify_network_policy(binding)
        binding.check(); unmounted()
        process = subprocess.Popen(command(binding), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd='/', close_fds=True, pass_fds=(), umask=0o077,
            env={'HOME':str(contract.BASE/'home'), 'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
                 'LANG':'en_US.UTF-8', 'ERP_HOME_REHEARSAL':'1'})
        interrupted = False; first = True
        while True:
            try:
                output, _ = process.communicate(contract.canonical(request) if first else None)
                break
            except KeyboardInterrupt: interrupted = True; first = False
        require(not interrupted and process.returncode == 0 and 0 < len(output) <= LIMIT)
        result = boot.decode(output)
        expected = {'type':RESULT, **request, 'package_sha256':binding.control['package_sha256'],
                    'context_sha256':binding.control['context_sha256'], 'identity':actual_identity(binding.context)}
        require(isinstance(result, dict) and set(result) == set(expected)|{'mount','directory_identity','credential_read'}
                and all(result[k] == v for k,v in expected.items()) and type(result['credential_read']) is bool)
        cfg.require_observation(binding.value, result['mount'])
        storage.validate_directory_identity(result['directory_identity'], binding.context['uid'])
        parents(); binding.check()
        mount, directories = observed(binding)
        require(result['mount'] == mount and result['directory_identity'] == directories)


def main(*, script, executable):
    raw = sys.stdin.buffer.read(LIMIT+1)
    require(0 < len(raw) <= LIMIT)
    result = run(boot.decode(raw), script=script, executable=executable)
    output = contract.canonical(result)
    require(len(output) <= LIMIT)
    sys.stdout.buffer.write(output+b'\n'); sys.stdout.buffer.flush()

"""One-shot supervisor upgrade by a separately SHA-approved stdlib root pack.

An external fixed-SHA loader must authenticate this pack before importing it.
Self-checking is not authorization. This module is deliberately not wired into
the old bootstrap CLI, build capabilities, or an existing system-service action.
Root reads only installation/program/control metadata, never a business DB or
operational secret. The separately signed UID worker owns ERP stop and canary.
Success requires apply's normal return and no failed record for that attempt;
verified.json alone is not completion if a subsequent final check failed. All
records are retained. EXIT/START timeouts bound observation stages, not workers
or the complete transaction: a started worker is awaited without force-killing.
"""
from contextlib import contextmanager, ExitStack
import ctypes
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import uuid

from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_release_publish as publication
from desktop_assistant import mac_system_service as system
from desktop_assistant import mac_system_upgrade_contract as contract

BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
DATA = Path('/System/Volumes/Data')
MODULE = 'desktop_assistant/mac_system_upgrade.py'
CONTRACT_MODULE = 'desktop_assistant/mac_system_upgrade_contract.py'
WORKER = 'desktop_assistant/mac_system_upgrade_worker.py'
CAPABILITY = 'system_upgrade_worker_v1'
CODE_FILES = frozenset((*boot.FILES, MODULE, CONTRACT_MODULE))
STATE_LIMIT = 16 * 1024**2
EXIT_TIMEOUT = 250
START_TIMEOUT = 180
ERROR = '系统监护升级未确认完成；保留现场和未决记录，不重试、回退启用或清理'


def require(value):
    if not value:
        raise ValueError(ERROR)


class UpgradeError(ValueError):
    def __init__(self, result):
        super().__init__(ERROR)
        self.result = result  # Public booleans only; never exception text/state.


def _entry():
    require(sys.platform == 'darwin' and sys.flags.isolated and sys.flags.no_site
            and sys.dont_write_bytecode
            and (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (0, 0, 0, 0))
    boot.verify_python(current=True)


def _code_root():
    return Path(__file__).absolute().parent.parent


def verify_code():
    """Separate closed allowlist, without altering boot.FILES/verify_code."""
    root = _code_root()
    for directory in (root, *root.parents):
        boot.protected(directory, directory=True, readable=True)
    for module, name in ((boot, 'desktop_assistant/mac_keychain_install.py'),
                         (publication, 'desktop_assistant/mac_release_publish.py'),
                         (system, 'desktop_assistant/mac_system_service.py'),
                         (contract, CONTRACT_MODULE)):
        require(Path(module.__file__).absolute() == root / name)
    raw = boot.protected(root / 'install-manifest.json', readable=True)
    value = contract.decode(raw)
    require(set(value) == {'type', 'source', 'files'} and value['type'] == 'tianming.keychain-code.v1'
            and type(value['source']) is str and re.fullmatch('[0-9a-f]{40}', value['source'])
            and type(value['files']) is dict and set(value['files']) == CODE_FILES
            and all(contract.sha(v) for v in value['files'].values()))
    expected_directories = {str(parent) for name in CODE_FILES for parent in Path(name).parents
                            if str(parent) != '.'}
    actual, pending = set(), [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                require(not entry.is_symlink())
                if entry.is_dir(follow_symlinks=False):
                    require(path.relative_to(root).as_posix() in expected_directories)
                    boot.protected(path, directory=True, readable=True); pending.append(path)
                else:
                    require(entry.is_file(follow_symlinks=False))
                    actual.add(path.relative_to(root).as_posix())
    require(actual == CODE_FILES | {'install-manifest.json'})
    for name in CODE_FILES:
        require(boot.digest(boot.protected(root / name, readable=True)) == value['files'][name])
    return boot.digest(raw)


def _owned(info, uid, gid):
    return info.st_uid == uid and (gid is None or info.st_gid == gid)


def _fingerprint(info):
    return tuple(getattr(info, k) for k in ('st_dev', 'st_ino', 'st_uid', 'st_gid',
        'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


class Pin:
    """No-follow local FD; mutable contents still pin inode/owner/mode/ACL."""
    def __init__(self, path, *, uid=0, gid=None, mode=0o644, parent=None,
                 directory=False, mutable=False):
        self.path, self.parent = Path(path), parent
        self.uid, self.gid, self.mode = uid, gid, mode
        self.directory, self.mutable = directory, mutable
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0)
        self.fd = os.open(self.path.name if parent else self.path, flags,
                          dir_fd=parent.fd if parent else None)
        try:
            self.initial = _fingerprint(os.fstat(self.fd)); self.check()
        except BaseException:
            os.close(self.fd); raise

    def check(self):
        if self.parent: self.parent.check()
        info = os.fstat(self.fd)
        named = os.stat(self.path.name if self.parent else self.path,
                        dir_fd=self.parent.fd if self.parent else None, follow_symlinks=False)
        require((stat.S_ISDIR if self.directory else stat.S_ISREG)(info.st_mode)
                and _owned(info, self.uid, self.gid) and stat.S_IMODE(info.st_mode) == self.mode
                and info.st_dev == DATA.stat().st_dev
                and (self.directory or info.st_nlink == 1)
                and _fingerprint(info) == _fingerprint(named))
        # Contents may grow; path identity and protection may never change.
        require(_fingerprint(info)[:5] == self.initial[:5])
        if not self.mutable: require(_fingerprint(info) == self.initial)
        boot.no_acl(self.fd)
        return info

    def read(self, limit=contract.LIMIT):
        info = self.check(); require(not self.directory and 0 < info.st_size <= limit)
        before = _fingerprint(info); os.lseek(self.fd, 0, os.SEEK_SET)
        parts, remaining = [], info.st_size + 1
        while remaining:
            block = os.read(self.fd, min(remaining, 1024 * 1024))
            if not block: break
            parts.append(block); remaining -= len(block)
        raw = b''.join(parts)
        require(len(raw) == info.st_size and _fingerprint(self.check()) == before)
        return raw

    def __enter__(self): return self

    def __exit__(self, *_): os.close(self.fd)


def decode_state(raw):
    """Same bounded finite-number semantics as the service manage state codec."""
    require(type(raw) is bytes and 0 < len(raw) <= STATE_LIMIT)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result); result[key] = value
        return result
    def invalid(_): raise ValueError(ERROR)
    def number(value):
        result = float(value); require(math.isfinite(result)); return result
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=invalid, parse_float=number)
        require(type(value) is dict)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        require(len(boot.canonical(value)) <= STATE_LIMIT)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(ERROR) from None
    return value


def _state(root, uid, gid):
    with Pin(root.path / 'state.json', uid=uid, gid=gid, mode=0o600, parent=root) as item:
        raw = item.read(STATE_LIMIT)
    return raw, decode_state(raw)


def _no_pending(root):
    with os.scandir(root.fd) as entries:
        require(not any(entry.name == 'state.json.pending' or
                        re.fullmatch(r'state-[0-9a-f]{32}\.pending\.json', entry.name) for entry in entries))


def _new(parent, name, raw, *, mode=0o600):
    require(type(raw) is bytes and 0 < len(raw) <= contract.LIMIT and Path(name).name == name)
    parent.check()
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode, dir_fd=parent.fd)
    try:
        os.fchmod(fd, mode)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view); require(count > 0); view = view[count:]
        os.fsync(fd)
    finally: os.close(fd)
    os.fsync(parent.fd); parent.check()
    with Pin(parent.path / name, mode=mode, parent=parent) as item:
        require(item.read() == raw)


def _cas(parent, name, previous, replacement, temporary):
    """Root record CAS under publication lock; never overwrite an unknown file."""
    with Pin(parent.path / name, parent=parent) as old:
        require(old.read() == previous)
        _new(parent, temporary, replacement, mode=0o644)
        with Pin(parent.path / temporary, parent=parent) as new:
            require(new.read() == replacement); require(old.read() == previous)
            parent.check()
            os.replace(temporary, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
            os.fsync(parent.fd)
            require(_fingerprint(os.fstat(new.fd)) ==
                    _fingerprint(os.stat(name, dir_fd=parent.fd, follow_symlinks=False)))
    with Pin(parent.path / name, parent=parent) as final:
        require(final.read() == replacement)


@contextmanager
def _publication():
    # Refuse to initialize a missing lock in an allegedly installed system.
    with Pin(BASE / 'publication.lock', mode=0o600) as pin:
        require(pin.read() == b'publication lock\n')
        with publication.lock():
            pin.check(); yield pin


@contextmanager
def _paths(*, factory=False):
    with ExitStack() as held:
        base = held.enter_context(Pin(BASE, mode=0o755, directory=True, mutable=True))
        root = held.enter_context(Pin(ROOT, uid=499, gid=499, mode=0o700,
                                      directory=True, mutable=True, parent=base))
        control = held.enter_context(Pin(ROOT / 'control', uid=499, gid=499, mode=0o700,
                                         directory=True, mutable=True, parent=root))
        plists = held.enter_context(Pin(system.PLISTS, mode=0o755, directory=True, mutable=True))
        for parent in system.PLISTS.parents: boot.protected(parent, directory=True)
        locks = [held.enter_context(Pin(control.path / name, uid=499, gid=499, mode=0o600, parent=control))
                 for name in ('operation.lock', 'service-supervisor.lock')]
        require(all(item.read() == b'0' for item in locks))
        logs = [held.enter_context(Pin(BASE / name, gid=499, mode=0o660, mutable=True, parent=base))
                for name in ('system-service.stdout.log', 'system-service.stderr.log')]
        pins = [base, root, control, plists, *locks, *logs]
        authority = None
        if factory:
            active, gate = [held.enter_context(Pin(BASE / name, parent=base)) for name in
                           (contract.ACTIVATION_NAME, contract.OWNER_PUBLICATION_NAME)]
            original_active, original_gate = active.read(), gate.read()
            authority = contract.activation_binding(original_active, original_gate)
            pins.extend((active, gate))
        def check():
            for item in pins: item.check()
            if factory:
                require(not os.path.lexists(BASE / contract.OWNER_FAILED_NAME)
                        and active.read() == original_active and gate.read() == original_gate)
            else:
                require(not os.path.lexists(BASE / contract.ACTIVATION_NAME))
            _no_pending(root)
        check()
        yield dict(base=base, root=root, control=control, plists=plists,
                   operation=locks[0], monitor=locks[1], logs=logs, check=check, authority=authority)


def _metadata(plan):
    require(verify_code() == plan['code_manifest_sha256'])
    contract.validate_window(plan, time.time())
    require(plan['uid'] == plan['gid'] == 499)
    old, new = (system.metadata(plan[prefix + '_package_sha256']) for prefix in ('old', 'new'))
    require(old['context'] == new['context'] and old['public'] == new['public'])
    for meta in (old, new):
        require(all(meta[k] == plan[k] for k in ('context_sha256', 'publisher_sha256', 'registry_sha256')))
        require(meta['context']['uid'] == meta['context']['gid'] == 499)
    identity, keychain = boot.modules()
    keychain._identity(old['context'], administrator=True)
    require(plan['current_package_sha256'] in boot.decode(publication.read_record(BASE / 'published.json'))['packages'])
    for prefix in ('old', 'new'):
        require(boot.digest(system._plist(plan[prefix + '_package_sha256'], plan['publisher_sha256']))
                == plan[prefix + '_plist_sha256'])
    manifest = system._program_manifest(new)
    caps = manifest.get('runtime_capabilities', {})
    require(type(caps.get(CAPABILITY)) is int and caps[CAPABILITY] == 1
            and {WORKER, CONTRACT_MODULE, 'desktop_assistant/manager.py',
                 'desktop_assistant/mac_service_manage.py'} <= manifest.get('files', {}).keys())
    if plan['type'] == contract.FACTORY_PLAN:
        require(type(caps.get(contract.FACTORY_CAPABILITY)) is int
                and caps[contract.FACTORY_CAPABILITY] == 1
                and 'desktop_assistant/mac_factory_runtime.py' in manifest.get('files', {}))
    return old, new


def _initial(plan, paths):
    old, new = _metadata(plan); paths['check']()
    expected = ({key: plan[key] for key in ('activation_sha256', 'owner_publication_sha256', 'context_sha256')}
                if plan['type'] == contract.FACTORY_PLAN else None)
    require(paths['authority'] == expected)
    raw, _ = system.checked_control(old)
    require(boot.digest(raw) == plan['old_control_sha256'])
    plist = system._installed_plist(old)
    state_raw, state = _state(paths['root'], 499, 499)
    require(state.get('current') == plan['current_package_sha256']
            and boot.digest(state_raw) == plan['state_before_sha256'])
    stopped = boot.canonical({**state, 'manual_stop': True}); decode_state(stopped)
    require(boot.digest(stopped) == plan['state_stopped_sha256'])
    return old, new, raw, plist, stopped


def make_plan(old_package, new_package):
    """Read installed metadata/state only. This does not publish or stop."""
    _entry(); code = verify_code()
    require(contract.sha(old_package) and contract.sha(new_package) and old_package != new_package)
    with _publication() as publication_pin, _paths(factory=os.path.lexists(BASE / contract.ACTIVATION_NAME)) as paths:
        old = system.metadata(old_package)
        control, _ = system.checked_control(old); plist = system._installed_plist(old)
        raw, state = _state(paths['root'], 499, 499)
        require(contract.sha(state.get('current')))
        stopped = boot.canonical({**state, 'manual_stop': True}); decode_state(stopped)
        now = int(time.time())
        plan = {'type': contract.PLAN, 'attempt_id': uuid.uuid4().hex,
            'created_at': now, 'expires_at': now + contract.MAX_AGE, 'uid': 499, 'gid': 499,
            'code_manifest_sha256': code, 'old_package_sha256': old_package, 'new_package_sha256': new_package,
            'current_package_sha256': state['current'], **{k: old[k] for k in
                ('context_sha256', 'publisher_sha256', 'registry_sha256')},
            'old_control_sha256': boot.digest(control), 'old_plist_sha256': boot.digest(plist),
            'new_plist_sha256': boot.digest(system._plist(new_package, old['publisher_sha256'])),
            'state_before_sha256': boot.digest(raw), 'state_stopped_sha256': boot.digest(stopped)}
        if paths['authority'] is not None:
            plan.update(type=contract.FACTORY_PLAN, **{key: paths['authority'][key] for key in
                        ('activation_sha256', 'owner_publication_sha256')})
        contract.validate_plan(plan, now=time.time()); _initial(plan, paths); publication_pin.check()
        return plan


def _header(raw, approved_sha):
    require(contract.sha(approved_sha) and contract.digest(raw) == approved_sha)
    plan = contract.validate_plan(contract.decode(raw), now=time.time())
    require(verify_code() == plan['code_manifest_sha256'])
    return plan


def prepare(raw, approved_sha):
    """Publish only the separately approved fixed public plan, exclusively."""
    _entry(); plan = _header(raw, approved_sha)
    with _publication() as publication_pin, _paths(factory=plan['type'] == contract.FACTORY_PLAN) as paths:
        _initial(plan, paths)
        name = 'system-upgrade-' + plan['attempt_id']
        os.mkdir(name, mode=0o755, dir_fd=paths['base'].fd)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=paths['base'].fd)
        try: os.fchmod(fd, 0o755); os.fsync(fd)
        finally: os.close(fd)
        os.fsync(paths['base'].fd)
        with Pin(BASE / name, mode=0o755, directory=True, mutable=True, parent=paths['base']) as slot:
            _new(slot, 'plan.json', raw, mode=0o644)
            _initial(plan, paths)
            _new(slot, 'prepared.json', contract.canonical({'plan_sha256': approved_sha,
                 'type': 'tianming.system-upgrade-prepared.v1'}))
            paths['check'](); publication_pin.check()
        return {'plan_sha256': approved_sha, 'attempt_id': plan['attempt_id'], 'service_changed': False}


class _BSDInfo(ctypes.Structure):
    # Apple SDK sys/proc_info.h: sizeof=136, start seconds/useconds at 120/128.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('birth_sec', ctypes.c_uint64), ('birth_usec', ctypes.c_uint64)]


def _libproc():
    require(sys.platform == 'darwin' and ctypes.sizeof(_BSDInfo) == 136
            and _BSDInfo.birth_sec.offset == 120 and _BSDInfo.birth_usec.offset == 128)
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    lib.proc_pidpath.restype = ctypes.c_int
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    lib.proc_listpids.restype = ctypes.c_int
    return lib


def process_identity(pid):
    """Kernel birth and executable sandwich, never PID existence alone."""
    require(type(pid) is int and 1 < pid < 2**31)
    lib = _libproc()
    def sample():
        value = _BSDInfo(); ctypes.set_errno(0)
        require(lib.proc_pidinfo(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value)) == ctypes.sizeof(value))
        # SDK SZOMB=5 / PROC_FLAG_INEXIT=4. Run/sleep transitions are not identity.
        require(value.status != 5 and not value.flags & 4)
        result = {k: int(getattr(value, k)) for k in ('pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'birth_sec', 'birth_usec')}
        require(result['pid'] == pid and result['birth_sec'] > 0 and 0 <= result['birth_usec'] < 1000000)
        return result
    before = sample(); buffer = ctypes.create_string_buffer(4096); ctypes.set_errno(0)
    length = lib.proc_pidpath(pid, buffer, len(buffer)); require(0 < length < len(buffer))
    path = buffer.value.decode('utf-8', 'strict'); require(path.startswith('/') and '\x00' not in path)
    require(sample() == before)
    return {**before, 'exe': path}


def _service_pids():
    """Bounded effective/real UID union, never a whole-system claim."""
    lib = _libproc()
    def listing(kind):
        needed = lib.proc_listpids(kind, 499, None, 0)
        require(0 <= needed <= 65536 * 4 and needed % 4 == 0)
        capacity = max(64, needed // 4 + 64)
        buffer = (ctypes.c_int * capacity)()
        size = lib.proc_listpids(kind, 499, buffer, ctypes.sizeof(buffer))
        require(0 <= size < ctypes.sizeof(buffer) and size % 4 == 0)
        values = list(buffer)[:size // 4]
        require(all(1 < pid < 2**31 for pid in values) and len(set(values)) == len(values))
        return frozenset(values)
    for _ in range(3):
        before = (listing(4), listing(5))
        if before == (listing(4), listing(5)):
            return before[0] | before[1]
    raise ValueError(ERROR)


def _no_old_monitor(meta):
    """No residual old interpreter in the strictly sampled service UID set."""
    expected = str((meta['tree'] / 'runtime/bin/python3.12').resolve(strict=True))
    pids = _service_pids(); identities = {pid: process_identity(pid) for pid in pids}
    require(_service_pids() == pids)
    for pid, value in identities.items():
        require(value == process_identity(pid)
                and value['uid'] == value['ruid'] == value['gid'] == value['rgid'] == 499
                and value['exe'] != expected)
    require(_service_pids() == pids)
    return len(pids)


class ExitWatch:
    """The registered kernel proc must emit NOTE_EXIT. Reuse is not proof."""
    def __init__(self, identity):
        import select  # kqueue symbols resolved only on the native path.
        require(sys.platform == 'darwin')
        self.identity, self.seen, self.api = dict(identity), False, select
        require(process_identity(identity['pid']) == identity)
        self.queue = select.kqueue()
        try:
            change = select.kevent(identity['pid'], filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
            self._events(self.queue.control([change], 1, 0))
            require(process_identity(identity['pid']) == identity)
        except BaseException:
            self.queue.close(); raise

    def _events(self, events):
        for event in events:
            require(event.ident == self.identity['pid'] and event.filter == self.api.KQ_FILTER_PROC
                    and not event.flags & self.api.KQ_EV_ERROR and event.fflags == self.api.KQ_NOTE_EXIT)
            self.seen = True

    def poll(self, timeout=0):
        self._events(self.queue.control([], 1, timeout)); return self.seen

    def __enter__(self): return self

    def __exit__(self, *_): self.queue.close()


def _job():
    code, output, error = system.launch(['print', 'system/' + system.LABEL])
    require(type(output) is bytes and type(error) is bytes and len(output) + len(error) <= 1024**2)
    if code != 0:
        require(code == 113 and b'Could not find service "cn.tianming.erp" in domain for system' in error)
        return None
    text = output.decode('utf-8', 'strict')
    states = re.findall(r'^\s*state = (\S+)[ \t]*$', text, re.M)
    pids = re.findall(r'^\s*pid = ([0-9]+)[ \t]*$', text, re.M)
    require(len(states) == 1 and len(pids) <= 1)
    return {'state': states[0], 'pid': int(pids[0]) if pids else None}


def _monitor(meta):
    job = _job(); require(job and job['state'] == 'running' and job['pid'] is not None)
    result = process_identity(job['pid'])
    expected = str((meta['tree'] / 'runtime/bin/python3.12').resolve(strict=True))
    require(result['ppid'] == 1 and result['uid'] == result['ruid'] == result['gid'] == result['rgid'] == 499
            and result['exe'] == expected)
    require(_job() == job and process_identity(job['pid']) == result)
    return result


def _launch(action):
    arguments = {'disable': ['disable', 'system/' + system.LABEL],
                 'enable': ['enable', 'system/' + system.LABEL],
                 'bootout': ['bootout', 'system', str(system.PLIST)],
                 'bootstrap': ['bootstrap', 'system', str(system.PLIST)]}[action]
    require(system.launch(arguments)[0] == 0)


def _collect(process):
    """Drain bounded public output; always observe our own child's exit."""
    parts, size, overflow, interrupted = [], 0, False, False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                try:
                    for key, _ in selector.select(0.25):
                        block = os.read(key.fileobj.fileno(), 8192)
                        if not block:
                            selector.unregister(key.fileobj); key.fileobj.close(); continue
                        if key.data:
                            size += len(block)
                            if size <= contract.LIMIT: parts.append(block)
                            else: overflow = True
                except KeyboardInterrupt: interrupted = True
    finally:
        # Even a collector error must not release the parent's borrowed lock
        # while its child still executes. Close pipes, then wait without signals.
        for stream in (process.stdout, process.stderr):
            if not stream.closed: stream.close()
        while True:
            try: code = process.wait(); break
            except KeyboardInterrupt: interrupted = True
    require(code == 0 and not interrupted and not overflow)
    return b''.join(parts)


def _worker(plan, plan_sha, meta, phase, fd=None):
    from desktop_assistant.home_process import command
    request = {'type': contract.REQUEST, 'attempt_id': plan['attempt_id'], 'plan_sha256': plan_sha,
               'phase': phase, 'challenge': uuid.uuid4().hex, 'lock_fd': fd}
    contract.validate_request(request, plan, plan_sha)
    environment = {'HOME': str(BASE / 'home'), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                   'LANG': 'en_US.UTF-8', 'ERP_HOME_REHEARSAL': '1'}
    argv = [str(meta['tree'] / 'runtime/bin/python3.12'), '-I', '-B', str(meta['tree'] / WORKER),
            '--attempt', plan['attempt_id'], '--plan-sha256', plan_sha,
            '--challenge', request['challenge'], '--phase', phase]
    if fd is not None: argv += ['--lock-fd', str(fd)]
    process = subprocess.Popen(command(argv, environment), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, user=499, group=499, extra_groups=[],
        close_fds=True, pass_fds=() if fd is None else (fd,), umask=0o077, cwd='/', env=environment)
    return contract.validate_reply(contract.decode(_collect(process)), request, plan, plan_sha)


@contextmanager
def _locked(pin):
    import fcntl
    pin.check(); require(pin.read() == b'0')
    require(fcntl.fcntl(pin.fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
    fcntl.flock(pin.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try: pin.check(); yield pin.fd; pin.check()
    finally: fcntl.flock(pin.fd, fcntl.LOCK_UN)


def _wait_exit(watch, paths, check):
    deadline = time.monotonic() + EXIT_TIMEOUT
    while not watch.poll(0.25):
        check(); require(time.monotonic() < deadline)
    # The event is mandatory even if the PID disappeared or was reused.
    require(_job() is None); paths['check']()


def _status(paths, identity, started):
    with Pin(paths['control'].path / 'service-status.json', uid=499, gid=499,
             mode=0o600, parent=paths['control']) as item:
        raw = item.read()
    value = boot.decode(raw)
    require(set(value) == {'status', 'pid', 'configured_mode', 'home_rehearsal', 'updated_at',
                          'system_identity', 'requires_user_login', 'keychain_canary_verified'})
    if value['pid'] != identity['pid'] or value['status'] in {'maintenance-busy', 'starting', 'stopped'}:
        return None
    factory = paths['authority'] is not None
    require(value['status'] == 'manually-stopped' and value['configured_mode'] == ('factory' if factory else 'home')
            and value['home_rehearsal'] is (not factory) and value['requires_user_login'] is False
            and value['keychain_canary_verified'] is True)
    actual = value['system_identity']; require(type(actual) is dict and set(actual) == contract.IDENTITY_FIELDS)
    require(all(type(actual[k]) is int and actual[k] == 499 for k in ('uid', 'euid', 'gid', 'egid')))
    groups = actual['kernel_groups']; installer, _ = boot.modules()
    require(type(groups) is list and 499 in groups and all(type(v) is int for v in groups)
            and len(set(groups)) == len(groups) and set(groups) <= set(installer.permitted_groups(499)))
    at = datetime.fromisoformat(value['updated_at'])
    require(at.utcoffset() is not None and started <= at.timestamp() <= time.time())
    return value


def _wait_new(meta, old, started, paths, check):
    deadline = time.monotonic() + START_TIMEOUT
    while True:
        check(); require(time.monotonic() < deadline)
        job = _job()
        if job and job['state'] == 'running' and job['pid'] is not None:
            identity = _monitor(meta)
            require((identity['pid'], identity['birth_sec'], identity['birth_usec']) !=
                    (old['pid'], old['birth_sec'], old['birth_usec'])
                    and identity['birth_sec'] + identity['birth_usec'] / 1000000 >= started)
            try: status = _status(paths, identity, started)
            except FileNotFoundError: status = None
            if status is not None:
                check(); require(_monitor(meta) == identity)
                return identity, status
        time.sleep(0.1)


def _fail_closed(plan, paths, original):
    result = {'gate_disabled_confirmed': False, 'label_disabled_confirmed': False}
    known = [original, contract.disabled_control_bytes(plan, 'stop'), contract.disabled_control_bytes(plan, 'verify')]
    enabled = contract.disabled_control(plan, 'verify'); enabled['enabled'] = True
    known.append(contract.canonical(enabled))
    try:
        with Pin(system.CONTROL, parent=paths['base']) as current: raw = current.read()
        require(raw in known)
        value = boot.decode(raw); value['enabled'] = False; disabled = boot.canonical(value)
        if raw != disabled:
            _cas(paths['base'], system.CONTROL.name, raw, disabled,
                 'system-upgrade-failed-' + uuid.uuid4().hex + '.json')
        with Pin(system.CONTROL, parent=paths['base']) as current:
            result['gate_disabled_confirmed'] = current.read() == disabled
    except Exception: pass
    try: _launch('disable'); result['label_disabled_confirmed'] = True
    except Exception: pass
    return result


def apply(attempt_id, approved_sha):
    """Run a prepared attempt once. No arbitrary plan path or resume branch."""
    _entry(); require(contract.token(attempt_id) and contract.sha(approved_sha)); verify_code()
    require(Path(contract.plan_path(attempt_id)) == BASE / ('system-upgrade-' + attempt_id) / 'plan.json')
    with _publication() as publication_pin, \
            _paths(factory=os.path.lexists(BASE / contract.ACTIVATION_NAME)) as paths, ExitStack() as held:
        slot = held.enter_context(Pin(BASE / ('system-upgrade-' + attempt_id), mode=0o755,
                                      directory=True, mutable=True, parent=paths['base']))
        plan_file = held.enter_context(Pin(slot.path / 'plan.json', parent=slot))
        raw = plan_file.read(); plan = _header(raw, approved_sha); require(plan['attempt_id'] == attempt_id)
        with Pin(slot.path / 'prepared.json', mode=0o600, parent=slot) as prepared:
            require(contract.decode(prepared.read()) == {'type': 'tianming.system-upgrade-prepared.v1',
                                                        'plan_sha256': approved_sha})
        require(set(os.listdir(slot.fd)) == {'plan.json', 'prepared.json'})
        old, new, control_raw, plist_raw, stopped = _initial(plan, paths)
        old_identity = _monitor(old)
        current_control, current_plist = control_raw, plist_raw
        expected_state = plan['state_before_sha256']

        def check():
            publication_pin.check(); paths['check'](); plan_file.check(); require(plan_file.read() == raw); _metadata(plan)
            with Pin(system.CONTROL, parent=paths['base']) as item: require(item.read() == current_control)
            with Pin(system.PLIST, parent=paths['plists']) as item: require(item.read() == current_plist)
            state_raw, state = _state(paths['root'], 499, 499)
            require(boot.digest(state_raw) == expected_state and state['current'] == plan['current_package_sha256'])
            if expected_state == plan['state_stopped_sha256']: require(state_raw == stopped and state['manual_stop'] is True)

        with ExitWatch(old_identity) as watch:
            check()
            pending = {'type': 'tianming.system-upgrade-attempt.v1', 'plan_sha256': approved_sha,
                'old_monitor': old_identity, 'logs': {item.path.name: list(item.initial[:6]) for item in paths['logs']},
                'state_before_sha256': plan['state_before_sha256'], 'state_stopped_sha256': plan['state_stopped_sha256']}
            _new(slot, 'pending.json', contract.canonical(pending))
            _new(slot, 'old-control.json', control_raw); _new(slot, 'old.plist', plist_raw)
            needs_close = False
            try:
                check(); require(_monitor(old) == old_identity)
                needs_close = True  # CAS can succeed before a later readback error.
                disabled = contract.disabled_control_bytes(plan, 'stop')
                _cas(paths['base'], system.CONTROL.name, current_control, disabled,
                     'system-upgrade-' + attempt_id + '-disable-old.json')
                current_control = disabled; _launch('disable'); check()
                reply = _worker(plan, approved_sha, new, 'stop')
                expected_state = plan['state_stopped_sha256']; check()
                _new(slot, 'stop.json', contract.canonical(reply))
                job = _job()
                require(job is not None)
                if job['pid'] is None:
                    require(watch.poll())
                else:
                    require(job['pid'] == old_identity['pid'] and not watch.poll())
                    require(process_identity(old_identity['pid']) == old_identity and _job() == job)
                _launch('bootout'); _wait_exit(watch, paths, check)
                _new(slot, 'old-exited.json', contract.canonical({'old_monitor': old_identity,
                     'kernel_exit_observed': True, 'label_absent': True}))
                with _locked(paths['operation']) as fd:
                    check()
                    with _locked(paths['monitor']):
                        require(_job() is None)
                        scanned = _no_old_monitor(old)
                        _new(slot, 'old-service-processes.json', contract.canonical({
                            'old_interpreter_absent_for_service_uid': True, 'sampled_process_count': scanned}))
                        new_plist = system._plist(plan['new_package_sha256'], plan['publisher_sha256'])
                        _cas(paths['plists'], system.PLIST.name, current_plist, new_plist,
                             'system-upgrade-' + attempt_id + '.plist')
                        current_plist = new_plist
                        disabled = contract.disabled_control_bytes(plan, 'verify')
                        _cas(paths['base'], system.CONTROL.name, current_control, disabled,
                             'system-upgrade-' + attempt_id + '-select-new.json')
                        current_control = disabled; check()
                        reply = _worker(plan, approved_sha, new, 'verify', fd)
                        check(); _new(slot, 'verify.json', contract.canonical(reply))
                    enabled = contract.disabled_control(plan, 'verify'); enabled['enabled'] = True
                    enabled = contract.canonical(enabled)
                    _cas(paths['base'], system.CONTROL.name, current_control, enabled,
                         'system-upgrade-' + attempt_id + '-enable-new.json')
                    current_control = enabled; check(); require(_job() is None); _no_old_monitor(old)
                    _launch('enable'); started = time.time(); _launch('bootstrap')
                # The new tick needs operation.lock to report manually-stopped.
                identity, status = _wait_new(new, old_identity, started, paths, check)
                check(); require(_monitor(new) == identity)
                result = {'type': 'tianming.system-upgrade-result.v1', 'attempt_id': attempt_id,
                    'plan_sha256': approved_sha, 'old_package_sha256': plan['old_package_sha256'],
                    'new_package_sha256': plan['new_package_sha256'], 'current_package_sha256': plan['current_package_sha256'],
                    'state_sha256': plan['state_stopped_sha256'], 'old_monitor': old_identity,
                    'new_monitor': identity, 'system_identity': status['system_identity'],
                    'old_kernel_exit_verified': True, 'system_keychain_canary_verified': True,
                    'manual_stop': True, 'erp_started': False, 'root_gate_enabled': True,
                    'requires_user_login': False, 'logout_reboot_verified': False, 'factory_activated': False}
                if plan['type'] == contract.FACTORY_PLAN:
                    result.update(configured_mode='factory', factory_activated=True,
                        activation_performed=False, activation_preserved=True,
                        activation_sha256=plan['activation_sha256'],
                        owner_publication_sha256=plan['owner_publication_sha256'])
                _new(slot, 'verified.json', contract.canonical(result)); check()
                require(_monitor(new) == identity)
                return result
            except (Exception, KeyboardInterrupt) as exc:
                closure = _fail_closed(plan, paths, control_raw) if needs_close else {
                    'gate_disabled_confirmed': False, 'label_disabled_confirmed': False}
                failure = {'type': 'tianming.system-upgrade-failure.v1', 'plan_sha256': approved_sha,
                    'attempt_id': attempt_id, 'preserve_scene': True, 'do_not_retry': True,
                    'error_type': type(exc).__name__, **closure}
                try: _new(slot, 'failed.json', contract.canonical(failure))
                except Exception: pass
                raise UpgradeError(failure) from None

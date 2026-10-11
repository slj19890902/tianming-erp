"""Pin an already approved SMB destination; no mounts, secrets or directory creation.

The writer holds every path component until completion. A fresh readback worker
opens the same directory identities again. Neither a local fallback nor a new
SMB session is accepted as the original storage location.
"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import os
from pathlib import Path
import stat

from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_mount as mounts, mac_smb_contract as cfg, mac_smb_mount as observer
from desktop_assistant.mac_smb_client import _parent_permissions


def require(ok):
    if not ok: raise ValueError(cfg.ERROR)


def _dedicated(uid):
    require((os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (uid, uid, uid, uid))
    require(80 not in boot._actual()['kernel_groups'])


def _stamp(info):
    return {key: getattr(info, 'st_' + key) for key in ('dev', 'ino', 'uid', 'gid', 'mode')}


def _local_parent(path, info):
    require(stat.S_ISDIR(info.st_mode) and _parent_permissions(path, info))


def _directory(fd, uid, *, gid=None):
    info = os.fstat(fd)
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
            and info.st_gid == (uid if gid is None else gid) and stat.S_IMODE(info.st_mode) == 0o700)
    boot.no_acl(fd)
    return _stamp(info)


def validate_directory_identity(value, uid):
    require(isinstance(value, list) and len(value) == len(Path(cfg.RELATIVE).parts) + 1)
    for item in value:
        require(isinstance(item, dict) and set(item) == {'dev','ino','uid','gid','mode'}
                and all(type(v) is int and v >= 0 for v in item.values())
                and item['ino'] > 0 and item['uid'] == item['gid'] == uid
                and item['mode'] == stat.S_IFDIR | 0o700)
    require(len({item['dev'] for item in value}) == 1)
    return value


class Location:
    def __init__(self, fd, observation, identities, check):
        self.destination_fd = fd
        self._observation = deepcopy(observation)
        self._identities = deepcopy(identities)
        self._check = check

    @property
    def directory_identity(self): return deepcopy(self._identities)

    def check(self):
        self._check()
        return deepcopy(self._observation)


@contextmanager
def open_location(value, *, context_sha, uid):
    cfg.validate(value, context_sha, uid)
    _dedicated(uid)
    require(value['server_address'] not in mounts.local_addresses())
    original = deepcopy(value)
    with ExitStack() as stack:
        parents = []
        fd = None
        # The physical Data-volume path has no /Library firmlink traversal.
        for path in (Path('/'), *reversed(cfg.MOUNT.parents[:-1])):
            parent = fd
            fd = os.open(path if parent is None else path.name,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            _local_parent(path, info); boot.no_acl(fd)
            parents.append((path, fd, parent, _stamp(info)))
        mount_parent = fd
        # Query cached kernel metadata before any open can fall back locally.
        require(cfg.kernel_matches(value, mounts._select(cfg.MOUNT)))
        mount_fd = os.open(cfg.MOUNT.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=mount_parent)
        stack.callback(os.close, mount_fd)
        observed = observer.observe(value, mount_fd, context_sha=context_sha, uid=uid)
        cfg.require_observation(value, observed)
        nodes = [(cfg.MOUNT.name, mount_fd, mount_parent, _directory(mount_fd, uid))]
        fd = mount_fd
        for name in Path(cfg.RELATIVE).parts:
            parent = fd
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stack.callback(os.close, fd)
            nodes.append((name, fd, parent, _directory(fd, uid)))
        identities = [item[3] for item in nodes]
        validate_directory_identity(identities, uid)

        def check():
            require(value == original)
            _dedicated(uid)
            for path, pfd, parent, stamp in parents:
                current = os.fstat(pfd)
                _local_parent(path, current); boot.no_acl(pfd)
                named = os.stat(path if parent is None else path.name, dir_fd=parent, follow_symlinks=False)
                require(_stamp(current) == stamp == _stamp(named))
            for name, nfd, parent, stamp in nodes:
                require(_directory(nfd, uid) == stamp
                        and mounts.fd_identity(nfd) == observed['kernel'])
                # The namespace must still name the held FD, not a replacement.
                require(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == _stamp(os.fstat(nfd)))
            require(observer.observe(value, mount_fd, context_sha=context_sha, uid=uid) == observed)
            cfg.require_observation(value, observed)

        held = Location(fd, observed, identities, check)
        held.check()
        yield held
        held.check()

"""Apple-signed Python bridge for a root-only publishing key.

The private bytes travel only through inherited pipes, never stdout or argv.
This entry is installed in the independently approved, root-owned admin pack.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

BASE = Path('/Library/TianmingERP')


def require(ok):
    if not ok:
        raise ValueError('Mac发布身份钥匙串检查未通过')


def protected(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        item = os.fstat(fd)
        require(stat.S_ISREG(item.st_mode) and item.st_uid == 0
                and item.st_nlink == 1 and stat.S_IMODE(item.st_mode) == 0o644
                and 0 < item.st_size <= 65536)
        value = os.read(fd, 65537)
        require(len(value) == item.st_size
                and (path.lstat().st_dev, path.lstat().st_ino) == (item.st_dev, item.st_ino))
        return value
    finally:
        os.close(fd)


def main():
    require(sys.platform == 'darwin' and sys.flags.isolated and sys.dont_write_bytecode
            and os.getuid() == os.geteuid() == 0 and len(sys.argv) in (3, 4))
    action, expected_context = sys.argv[1:3]
    require(action in {'create', 'read', 'denial-probe'} and len(expected_context) == 64)
    for parent in (Path(__file__).parent, Path(__file__).parent.parent, BASE):
        require(not parent.is_symlink() and parent.lstat().st_uid == 0
                and stat.S_IMODE(parent.lstat().st_mode) == 0o755)
    context = protected(BASE/'keychain-context.json')
    require(hashlib.sha256(context).hexdigest() == expected_context)
    account = 'mac-publisher-v1:' + expected_context
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from app.core.mac_service_keychain import Security, BOOTSTRAP_SERVICE
    api = Security()
    if action == 'create':
        require(len(sys.argv) == 3)
        raw = sys.stdin.buffer.read(33)
        require(len(raw) == 32)
        with api.system(writable=True) as keychain:
            api.require_absent(keychain, BOOTSTRAP_SERVICE, account)
            api.put(keychain, BOOTSTRAP_SERVICE, account, raw, 0, 0)
        del raw
    else:
        require(len(sys.argv) == 4)
        secret_fd = int(sys.argv[3])
        require(secret_fd > 2 and stat.S_ISFIFO(os.fstat(secret_fd).st_mode))
        if action == 'denial-probe':
            # Drop kernel identity before the actual Security.framework read.
            os.setgroups([]); os.setgid(499); os.setuid(499)
            require(os.getuid() == os.geteuid() == 499 and os.getgid() == os.getegid() == 499
                    and set(os.getgroups()) <= {499})
        with api.system() as keychain:
            try:
                raw = api.get(keychain, BOOTSTRAP_SERVICE, account)
            except Exception as error:
                if action == 'denial-probe' and getattr(error, 'os_status', None) in {-25293, -25308}:
                    print(json.dumps({'publishing_key_denied':True, 'uid':499, 'gid':499,
                                      'os_status':error.os_status}), flush=True)
                    return
                raise
        require(action != 'denial-probe')
        require(len(raw) == 32)
        require(os.write(secret_fd, raw) == 32)
        del raw
    print('{"keychain_operation_confirmed":true}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never disclose Security.framework queries or secret values.
        print('{"keychain_operation_confirmed":false,"error_type":"' + type(error).__name__ + '"}', flush=True)
        raise SystemExit(1)

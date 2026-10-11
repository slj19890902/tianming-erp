"""Check remote NAS before stopping ERP. Never creates a missing NAS directory."""
from pathlib import Path
import os
import subprocess
import sys
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def probe(path):
    from desktop_assistant.mac_backup_storage import before_write
    before_write(path)
    return _write_probe(path)


def _write_probe(path):
    root = Path(path)
    if not root.is_absolute() or not root.is_dir():
        return 2
    target = root / ('tianming-connectivity-' + uuid.uuid4().hex + '.tmp')
    payload = os.urandom(16384)
    try:
        with target.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if target.read_bytes() != payload:
            return 3
        return 0
    finally:
        target.unlink(missing_ok=True)


def check_before_stop(path, *, manager=None):
    from desktop_assistant import mac_smb_stream_target as stream
    if stream.selected(path):
        return stream.preflight(path, manager=manager)
    from desktop_assistant.mac_backup_storage import prepare_destination, required, BASE
    prepare_destination(path)
    environment = None
    if required():
        command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), str(path)]
        environment = {'HOME': str(BASE/'home'), 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
    elif getattr(sys, 'frozen', False):
        command = [sys.executable, '--nas-probe-path', str(path)]
    else:
        command = [sys.executable, '-m', 'desktop_assistant.nas_probe', str(path)]
    try:
        result = subprocess.run(command, timeout=20, capture_output=True, env=environment,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ValueError('NAS远程读写超时，ERP保持原运行状态。请检查家中NAS网络；本次未开始备份或更新。') from None
    if result.returncode:
        raise ValueError('NAS无法完成读写验证，ERP保持原运行状态。请检查极空间登录和备份目录。')


if __name__ == '__main__':
    try:
        sys.exit(probe(sys.argv[1]))
    except Exception:
        sys.exit(2)

"""Bound console logs inside the server process, independent of its launcher.

Only server.log and its numbered rotations are touched. Business audit records
and backup/release logs do not belong to this policy.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
import threading


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise ValueError('runtime log ownership or type changed')


class BoundedLog:
    def __init__(self, control, *, max_bytes=8 * 1024**2, backups=3):
        self.path = Path(control) / 'server.log'
        self.max_bytes, self.backups = max_bytes, backups
        info = self.path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or not 1 <= backups <= 10 or max_bytes < 128):
            raise ValueError('runtime log policy or private control directory invalid')
        self.fd = None
        self._open()

    def _open(self):
        if self.path.exists() or self.path.is_symlink(): _regular(self.path)
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _regular(self.path)
            if os.fstat(fd).st_ino != self.path.lstat().st_ino:
                raise ValueError('runtime log path changed')
        except BaseException:
            os.close(fd); raise
        self.fd = fd
        # A previous release may have left a large active console file. Keep
        # its most recent bytes in the same owned inode; merely renaming it
        # would leave the old launcher descriptor holding all of its space.
        size = os.fstat(fd).st_size
        if size > self.max_bytes:
            recent = os.pread(fd, self.max_bytes, size - self.max_bytes)
            os.ftruncate(fd, 0)
            view = memoryview(recent)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            os.fsync(fd)

    def _rotate(self):
        paths = [self.path, *(self.path.with_name(f'server.log.{i}') for i in range(1, self.backups + 1))]
        for p in paths:
            if p.exists() or p.is_symlink(): _regular(p)
        self.close()
        if paths[-1].exists(): paths[-1].unlink()
        for i in range(len(paths) - 1, 0, -1):
            if paths[i - 1].exists(): os.replace(paths[i - 1], paths[i])
        self._open()

    def write(self, data):
        view = memoryview(data)
        while view:
            remaining = self.max_bytes - os.fstat(self.fd).st_size
            if remaining <= 0:
                self._rotate(); remaining = self.max_bytes
            count = os.write(self.fd, view[:remaining])
            view = view[count:]

    def close(self):
        if self.fd is not None:
            os.close(self.fd); self.fd = None


@contextmanager
def capture_console(control, *, max_bytes=8 * 1024**2, backups=3):
    """Drain stdout/stderr to rotating files from this very same server process.

    Native writes are captured too. The chat/maintenance launcher owns no pipe.
    A storage failure is recorded once and drained, never a blocked ERP thread.
    """
    sink = BoundedLog(control, max_bytes=max_bytes, backups=backups)
    saved = (os.dup(1), os.dup(2))
    reader, writer = os.pipe()
    def drain():
        failed = False
        try:
            while data := os.read(reader, 65536):
                if failed: continue
                try: sink.write(data)
                except (OSError, ValueError):
                    failed = True
                    try:
                        from desktop_assistant.storage import write_json
                        write_json(Path(control) / 'runtime-log-status.json',
                            {'status': 'attention', 'reason': 'runtime log storage failed; console output drained'})
                    except Exception: pass
        finally:
            sink.close(); os.close(reader)
    thread = threading.Thread(target=drain, name='erp-runtime-log', daemon=True)
    try:
        sys.stdout.flush(); sys.stderr.flush()
        thread.start()
        os.dup2(writer, 1); os.dup2(writer, 2); os.close(writer)
        yield
    finally:
        sys.stdout.flush(); sys.stderr.flush()
        os.dup2(saved[0], 1); os.dup2(saved[1], 2)
        os.close(saved[0]); os.close(saved[1])
        thread.join(timeout=5)

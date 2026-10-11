"""Single-host maintenance exclusion using the host's kernel file locks.

The lock file must remain in place: unlinking it would let another process lock
a different inode while an existing maintenance operation is still running.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import sys


BUSY = '另一个更新、恢复或备份正在进行'


@contextmanager
def operation_lock(path: Path, *, existing_only=False):
    # Source audit may lock existing metadata, but must never repair a missing
    # or uninitialized source. r+b closes the check/open creation race.
    with path.open('r+b' if existing_only else 'a+b') as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            if existing_only:
                raise ValueError('审计要求已有非空维护锁，不创建或初始化')
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError(BUSY) from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise ValueError(BUSY) from None
                raise
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

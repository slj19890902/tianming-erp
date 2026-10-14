"""Read-only, bounded collector for the exact 2026-10-13 published map bytes.

collect(directory) returns (small_manifest, matching_bytes_or_None). No output
file is written. Only that directory's immediate children are examined. Root
wrappers should retain an external process timeout as well as this POSIX alarm.
The fixed formal CLI default is for the authorized caller, never for tests.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import stat
import threading
import time


EXPECTED_SHA256 = 'e79feaba1ab0443a1ef7a337a10e7d3615a8536d21298a0af20161a390da521b'
FORMAL_DIRECTORY = Path('/Library/TianmingERP/instance/shared/data/layout_backups')
ALLOWED_DATES = frozenset(('20261013', '20261014'))
MAX_ENTRIES = 4096
MAX_FILES = 8
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
ENUMERATION_SECONDS = 3.0
TOTAL_SECONDS = 20.0
CHUNK_BYTES = 64 * 1024
NAME = re.compile(r'twin_layout_v1\.before_(\d{8})_(\d{6})_(\d{6})_([0-9a-f]{12})\.json\Z')


class _Stop(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def _identity(value):
    return dict(device=value.st_dev, inode=value.st_ino, size=value.st_size,
                mtime_ns=value.st_mtime_ns, ctime_ns=value.st_ctime_ns,
                file_type=stat.S_IFMT(value.st_mode))


def _directory(path):
    """Open each component relative to a pinned FD, without resolving symlinks."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.anchor != '/':
        raise _Stop('rejected', 'directory_must_be_absolute_without_parent_traversal')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open('/', flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class _Budget:
    def __init__(self):
        self.started = time.monotonic()
        self.deadline = self.started + TOTAL_SECONDS
        self.phase_deadline = self.started + min(ENUMERATION_SECONDS, TOTAL_SECONDS)
        self.reason = 'enumeration_seconds'

    def expired(self):
        now = time.monotonic()
        if now >= self.deadline:
            raise _Stop('limit_reached', 'total_seconds')
        if now >= self.phase_deadline:
            raise _Stop('limit_reached', self.reason)

    def alarm(self, _signal, _frame):
        raise _Stop('limit_reached', self.reason)

    def arm(self):
        self.expired()
        remaining = min(self.phase_deadline, self.deadline) - time.monotonic()
        if remaining <= 0:
            raise _Stop('limit_reached', self.reason)
        signal.setitimer(signal.ITIMER_REAL, remaining)

    def reading(self):
        self.phase_deadline = self.deadline
        self.reason = 'total_seconds'
        self.arm()


@contextmanager
def _deadline(budget):
    if threading.current_thread() is not threading.main_thread():
        raise _Stop('rejected', 'main_thread_required_for_hard_deadline')
    if any(signal.getitimer(signal.ITIMER_REAL)):
        raise _Stop('rejected', 'existing_alarm_not_modified')
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, budget.alarm)
    try:
        budget.arm()
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _enumerate(root_fd, budget, manifest):
    candidates = []
    ignored = Counter()
    manifest['ignored_counts'] = ignored
    with os.scandir(root_fd) as entries:
        for _ in range(MAX_ENTRIES):
            budget.expired()
            try:
                entry = next(entries)
            except StopIteration:
                break
            manifest['entries_seen'] += 1
            match = NAME.fullmatch(entry.name)
            if not match or match[1] not in ALLOWED_DATES:
                ignored['outside_filename_or_date_scope'] += 1
                continue
            try:
                timestamp = datetime.strptime(''.join(match.group(1, 2, 3)), '%Y%m%d%H%M%S%f')
            except ValueError:
                ignored['invalid_filename_timestamp'] += 1
                continue
            before = os.stat(entry.name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                ignored['symlink_or_nonregular'] += 1
                continue
            candidates.append((entry.name, _identity(before), match[4], timestamp))
            manifest['candidates_in_scope'] = len(candidates)
        else:
            # Reaching the cap is conservatively incomplete; do not fetch 4097.
            raise _Stop('limit_reached', 'directory_entries')
    manifest['ignored_counts'] = dict(ignored)
    manifest['candidates_in_scope'] = len(candidates)
    manifest['enumeration_complete'] = True
    # Filename prefix is only an ordering hint; full raw SHA is mandatory.
    return sorted(candidates, key=lambda c: (c[2] != EXPECTED_SHA256[:12], -int(c[3].strftime('%Y%m%d%H%M%S%f')), c[0]))


def _read_candidate(root_fd, name, expected_stat, budget, manifest):
    budget.expired()
    if manifest['files_opened'] >= MAX_FILES:
        raise _Stop('limit_reached', 'files')
    if expected_stat['size'] > MAX_FILE_BYTES:
        raise _Stop('limit_reached', 'file_bytes')
    if manifest['bytes_read'] + expected_stat['size'] > MAX_TOTAL_BYTES:
        raise _Stop('limit_reached', 'total_bytes')
    current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    if _identity(current) != expected_stat:
        raise _Stop('rejected', 'candidate_changed_before_open')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=root_fd)
    manifest['files_opened'] += 1
    try:
        if _identity(os.fstat(descriptor)) != expected_stat:
            raise _Stop('rejected', 'candidate_changed_at_open')
        chunks = []
        length = 0
        hasher = sha256()
        while length < expected_stat['size']:
            budget.expired()
            block = os.read(descriptor, min(CHUNK_BYTES, expected_stat['size'] - length))
            manifest['bytes_read'] += len(block)
            budget.expired()
            if not block:
                raise _Stop('rejected', 'candidate_truncated_during_read')
            chunks.append(block); hasher.update(block); length += len(block)
        after = _identity(os.fstat(descriptor))
        named_after = _identity(os.stat(name, dir_fd=root_fd, follow_symlinks=False))
        if after != expected_stat or named_after != expected_stat:
            raise _Stop('rejected', 'candidate_changed_during_read')
        budget.expired()
        actual_sha = hasher.hexdigest()
        manifest['checked'].append(dict(name=name, bytes=length, sha256=actual_sha,
                                       identity_before=expected_stat, identity_after=after))
        if actual_sha == EXPECTED_SHA256:
            return b''.join(chunks)
        return None
    finally:
        os.close(descriptor)


def collect(directory):
    """Return a manifest and at most one exact raw source; write nothing.

    found means only exact bytes collected. Map schema, other-floor metadata,
    business bindings and eligibility are checked later by the separate preview.
    A limited, unsafe or changing read returns no bytes, even after a hash match.
    """
    budget = _Budget()
    manifest = dict(schema='tianming.map-binding-source-collection.v1', directory=os.fspath(directory),
        expected_sha256=EXPECTED_SHA256, allowed_dates=sorted(ALLOWED_DATES),
        status='rejected', reason=None, read_only=True, files_written=False,
        limits=dict(directory_entries=MAX_ENTRIES, files=MAX_FILES, file_bytes=MAX_FILE_BYTES,
                    total_bytes=MAX_TOTAL_BYTES, enumeration_seconds=ENUMERATION_SECONDS, total_seconds=TOTAL_SECONDS),
        entries_seen=0, enumeration_complete=False, candidates_in_scope=0, ignored_counts={},
        files_opened=0, bytes_read=0, checked=[], match=None)
    matched = None
    root_fd = None
    try:
        with _deadline(budget):
            root_fd = _directory(directory)
            directory_before = _identity(os.fstat(root_fd))
            candidates = _enumerate(root_fd, budget, manifest)
            budget.reading()
            for name, identity, _prefix, _timestamp in candidates:
                manifest['pending_candidate'] = name
                matched = _read_candidate(root_fd, name, identity, budget, manifest)
                if matched is not None:
                    manifest['match'] = dict(name=name, sha256=EXPECTED_SHA256, bytes=len(matched))
                    break
            budget.expired()
            # A replaced directory path must not authenticate an old detached FD.
            verify_fd = _directory(directory)
            try:
                if (_identity(os.fstat(root_fd)) != directory_before
                        or _identity(os.fstat(verify_fd)) != directory_before):
                    raise _Stop('rejected', 'directory_changed_during_collection')
            finally:
                os.close(verify_fd)
            budget.expired()
            manifest.update(status='found' if matched is not None else 'not_found_within_scope',
                            directory_identity_unchanged=True)
    except _Stop as error:
        manifest.update(status=error.status, reason=error.reason, match=None)
        matched = None
    except OSError as error:
        manifest.update(status='rejected', reason='filesystem_error', errno=error.errno, match=None)
        matched = None
    finally:
        if root_fd is not None:
            os.close(root_fd)
        manifest['elapsed_seconds'] = round(time.monotonic() - budget.started, 6)
        manifest['enumeration_incomplete'] = not manifest['enumeration_complete']
        if manifest['status'] in ('found', 'not_found_within_scope'):
            manifest.pop('pending_candidate', None)
    return manifest, matched


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=FORMAL_DIRECTORY)
    parser.add_argument('--include-bytes', action='store_true', help='Include exact matching bytes as base64 on stdout; never write files')
    args = parser.parse_args()
    manifest, payload = collect(args.directory)
    result = dict(manifest=manifest)
    if args.include_bytes:
        result['matching_bytes_base64'] = base64.b64encode(payload).decode('ascii') if payload is not None else None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return {'found':0, 'not_found_within_scope':2, 'limit_reached':3, 'rejected':4}[manifest['status']]


if __name__ == '__main__':
    raise SystemExit(main())

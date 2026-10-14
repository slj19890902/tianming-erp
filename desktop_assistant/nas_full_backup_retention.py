"""Global two-version retention, after existing remote and recovery verification.

This does not generate backups, verify passwords, stop ERP or infer success from
names/mtime. There is deliberately no JSON-to-apply or production authority here.
A trusted integration must verify existing reports and fixed storage contract,
enumerate every relevant reference, and exclude ALL writers for freeze/recheck.
The inventory is exact, including staging files; removal is files only.
apply_plan is an OFFLINE historical cleanup candidate: it hashes each deletion
file twice while writers are excluded. Never call it on a normal backup's
synchronous/stopped path. Routine rotation needs the existing receipt-based
incremental index and only the one expired package/dependencies as its worklist.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import ContextManager, Protocol
import unicodedata

ROLES = frozenset({'current', 'previous', 'schema_authority', 'migration_target',
                   'recovery_sources', 'fixed_backup_contract', 'active_backups',
                   'formal_materials', 'drawings', 'knowledge', 'keys'})
COVERAGE = frozenset({'database', 'attachments', 'maps', 'program', 'credentials'})
DISPOSABLE = frozenset({'retired_complete_backup', 'stale_pending', 'stale_partial',
                        'completed_backup_work', 'historical_package', 'historical_rehearsal'})
KEEP = frozenset({'protected', 'awaiting_verification'})
SCOPES = frozenset({'backups', 'mac-server-backups', 'archives',
                    'development-archives', 'desktop-assistant'})


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _relative(value: str) -> str:
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or '\x00' in value or PurePosixPath(value).is_absolute()
            or str(PurePosixPath(value)) != value
            or any(p in {'.', '..'} or p.endswith((' ', '.')) for p in value.split('/'))):
        raise ValueError('invalid relative path')
    return value


def _comparison_key(path: str) -> str:
    # Mac/SMB can alias case and NFC/NFD spellings. Conservatively protect all
    # aliases, even on case-sensitive stores; never rewrite the execution path.
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', path).casefold())


def _under(path: str, prefix: str) -> bool:
    path, prefix = _comparison_key(path), _comparison_key(prefix)
    return path == prefix or path.startswith(prefix + '/')


@dataclass(frozen=True)
class Fingerprint:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    nlink: int

    @classmethod
    def of(cls, info):
        return cls(*(getattr(info, 'st_' + key) for key in cls.__dataclass_fields__))


@dataclass(frozen=True)
class FileRecord:
    path: str
    identity: Fingerprint
    sha256: str
    category: str
    # Verified job ownership/closure and classification report, never suffix inference.
    classification_report_sha256: str


@dataclass(frozen=True)
class VerifiedBackup:
    """Trusted verifier output, bound to existing readback and restore reports.

    The adapter must check reports' origin, actual success, byte identity, complete
    recovery coverage, source business point and dependencies. Hash-looking text
    from a user-supplied manifest cannot manufacture this authority.
    """
    path: str
    sha256: str
    source_time: str
    remote_report_sha256: str
    recovery_report_sha256: str
    coverage: tuple[str, ...]
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedSnapshot:
    root: str
    contract_sha256: str
    # Digest of authenticated contract/ref/upload/ownership verification evidence.
    authority_sha256: str
    inventory_complete: bool
    files: tuple[FileRecord, ...]
    backups: tuple[VerifiedBackup, ...]
    # All roles explicitly present. Empty roles require adapter-verified absence.
    references: tuple[tuple[str, tuple[str, ...]], ...]
    active_uploads: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    snapshot_sha256: str
    keep_backups: tuple[str, ...]
    delete: tuple[FileRecord, ...]
    retained: tuple[tuple[str, str], ...]
    blockers: tuple[str, ...]

    @property
    def delete_bytes(self):
        return sum(r.identity.size for r in self.delete)


def plan(snapshot: VerifiedSnapshot) -> Plan:
    """Pure planning: fail closed, globally latest + previous authenticated point."""
    blockers = []
    root = Path(snapshot.root)
    if (not root.is_absolute() or str(root) != snapshot.root or '..' in root.parts
            or root == Path(root.anchor) or not _sha(snapshot.contract_sha256)
            or not _sha(snapshot.authority_sha256)):
        blockers.append('fixed storage contract or authority missing')
    if snapshot.inventory_complete is not True:
        blockers.append('exact inventory incomplete')
    if snapshot.active_uploads:
        blockers.append('active or unconfirmed upload')
    references = dict(snapshot.references)
    if len(references) != len(snapshot.references) or set(references) != ROLES:
        blockers.append('runtime/backup/business reference coverage incomplete')
    if not references.get('current') or not references.get('fixed_backup_contract'):
        blockers.append('current runtime or fixed backup contract reference missing')
    protected = []
    for paths in references.values():
        for path in paths:
            try: protected.append(_relative(path))
            except ValueError: blockers.append('invalid reference path')
    indexed = {}
    folded = set()
    for record in snapshot.files:
        try: _relative(record.path)
        except ValueError: blockers.append('invalid inventory path'); continue
        if _comparison_key(record.path) in folded:
            blockers.append('duplicate inventory path')
        folded.add(_comparison_key(record.path))
        indexed[record.path] = record
        info = record.identity
        if (any(type(v) is not int or v < 0 for v in asdict(info).values())
                or not stat.S_ISREG(info.mode) or info.nlink != 1 or info.ino <= 0
                or not _sha(record.sha256)):
            blockers.append('nonregular, linked or unbound file: ' + record.path)
        if record.category not in DISPOSABLE | KEEP or not _sha(record.classification_report_sha256):
            blockers.append('unknown file or ownership: ' + record.path)
    authenticated = []
    for backup in snapshot.backups:
        try:
            _relative(backup.path)
            when = datetime.fromisoformat(backup.source_time)
            if (when.utcoffset() is None or when > datetime.now(timezone.utc)
                    or set(backup.coverage) != COVERAGE or len(backup.coverage) != len(COVERAGE)
                    or not all(_sha(x) for x in (backup.sha256, backup.remote_report_sha256,
                                                 backup.recovery_report_sha256))
                    or backup.remote_report_sha256 == backup.recovery_report_sha256
                    or backup.path not in indexed or indexed[backup.path].sha256 != backup.sha256
                    or not backup.path.endswith('.tmbackup')):
                raise ValueError('incomplete verification')
            for dependency in backup.dependencies:
                _relative(dependency)
                if dependency not in indexed: raise ValueError('missing dependency')
            authenticated.append((when, backup))
        except (ValueError, TypeError):
            blockers.append('backup or dependency verification incomplete: ' + backup.path)
    # Same encrypted bytes at two paths are a duplicate, not two recovery versions.
    unique = {}
    for when, backup in sorted(authenticated, key=lambda x: (x[0], x[1].path), reverse=True):
        unique.setdefault(backup.sha256, (when, backup))
    pair = list(unique.values())[:2]
    if len(pair) != 2 or pair[0][0] == pair[1][0]:
        blockers.append('two distinct authenticated business recovery points required')
    chosen = tuple(b.path for _, b in pair)
    for _, backup in pair:
        protected.extend((backup.path, *backup.dependencies))
    if any(r.path not in chosen and r.category == 'retired_complete_backup'
           and any(_under(r.path, p) or _under(p, r.path) for p in protected)
           for r in snapshot.files):
        blockers.append('extra complete version still referenced; update fixed references first')
    deletions, retained = [], []
    for record in snapshot.files:
        reason = None
        if any(_under(record.path, p) or _under(p, record.path) for p in protected):
            reason = 'protected recovery/runtime/business reference'
        elif record.path.split('/')[0] not in SCOPES:
            reason = 'outside disposable scope'
        elif record.category in KEEP:
            reason = record.category
        elif record.category in DISPOSABLE:
            deletions.append(record)
        else:
            reason = 'unknown'
        if reason: retained.append((record.path, reason))
    if blockers:
        retained.extend((r.path, 'blocked: no deletion authorized') for r in deletions)
        deletions = []
    return Plan(digest(asdict(snapshot)), chosen, tuple(sorted(deletions, key=lambda r: r.path)),
                tuple(sorted(retained)), tuple(sorted(set(blockers))))


class Authority(Protocol):
    """Production adapter must hold existing locks plus exclusive NAS writer lease.

    freeze authenticates storage, complete inventory, all ownership/references and
    both independent recovery results; recheck refreshes mount, upload and active
    references before each unlink. Lease must exclude name/content replacement.
    A cached manifest or a `verified: true` JSON flag does not meet this contract.
    """
    def freeze(self) -> ContextManager[VerifiedSnapshot]: ...
    def recheck(self, snapshot: VerifiedSnapshot) -> None: ...


def _directory(parent, name, held):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    held.callback(os.close, fd)
    return fd


def _root_fd(root, held):
    # Open from / component by component; O_NOFOLLOW also protects ancestor paths.
    fd = _directory(None, '/', held)
    for component in Path(root).parts[1:]: fd = _directory(fd, component, held)
    return fd


def _open_record(root_fd, record, held):
    parent = root_fd
    for part in record.path.split('/')[:-1]: parent = _directory(parent, part, held)
    name = record.path.split('/')[-1]
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    held.callback(os.close, fd)
    if Fingerprint.of(os.fstat(fd)) != record.identity:
        raise ValueError('file identity changed: ' + record.path)
    result = hashlib.sha256()
    while block := os.read(fd, 1024 * 1024): result.update(block)
    if (result.hexdigest() != record.sha256 or Fingerprint.of(os.fstat(fd)) != record.identity
            or Fingerprint.of(os.stat(name, dir_fd=parent, follow_symlinks=False)) != record.identity):
        raise ValueError('file content or name changed: ' + record.path)
    return parent, name, fd


def apply_plan(reviewed: Plan, authority: Authority) -> dict:
    """Offline historical cleanup only; no CLI or production adapter is supplied.

    All deletion files are preflighted before the first unlink. Stops at first
    failure and returns exact partial progress; never recursive removal. Native
    POSIX dir-FD operations required; no unsafe Windows/path fallback.
    """
    if (os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW')
            or os.open not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd):
        raise ValueError('safe file execution unavailable')
    if reviewed.blockers: raise ValueError('blocked retention plan')
    removed = []
    with authority.freeze() as snapshot:
        current = plan(snapshot)
        if current != reviewed or current.blockers:
            raise ValueError('reviewed plan or trusted evidence changed')
        with ExitStack() as held:
            root_fd = _root_fd(snapshot.root, held)
            # Avoid holding thousands of file descriptors across a large manifest.
            for record in current.delete:
                with ExitStack() as check: _open_record(root_fd, record, check)
            authority.recheck(snapshot)
            try:
                for record in current.delete:
                    authority.recheck(snapshot)
                    with ExitStack() as check:
                        parent, name, fd = _open_record(root_fd, record, check)
                        # Recheck authority after hashing, while the exclusive writer
                        # lease covers the unavoidable stat/unlink name race.
                        authority.recheck(snapshot)
                        if Fingerprint.of(os.stat(name, dir_fd=parent, follow_symlinks=False)) != record.identity:
                            raise ValueError('file replaced before deletion: ' + record.path)
                        os.unlink(name, dir_fd=parent)
                        removed.append(record)
            except (OSError, ValueError) as error:
                return {'status': 'stopped', 'removed': [r.path for r in removed],
                        'removed_bytes': sum(r.identity.size for r in removed), 'error': str(error)}
    return {'status': 'complete', 'removed': [r.path for r in removed],
            'removed_bytes': sum(r.identity.size for r in removed)}

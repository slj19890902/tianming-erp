"""Deletion primitives for explicitly owned, disposable local directories."""
from __future__ import annotations

import os
import re
from pathlib import Path
import shutil
import stat

import psutil


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def local_path(path: Path) -> Path:
    """Reject links in every component before resolving a deletion target."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            if is_link(part):
                raise ValueError('清理路径包含链接或重解析点')
        except FileNotFoundError:
            pass
    # Windows may expand ADMINI~1 and other 8.3 names. Resolve after rejecting
    # every reparse component; callers compare canonical parent boundaries.
    return path.resolve()


def is_application_process(name: str) -> bool:
    name = name.casefold()
    # "enterprise" in a VPN/system process is not an ERP executable. Keep
    # Python/Tianming names and ERP-prefixed words (ERP.exe, erp-worker, etc.).
    return ('python' in name or 'tianming' in name or '天明' in name
            or re.search(r'(^|[^a-z0-9])erp', name) is not None)


def assert_idle(root: Path, *, allow_unrelated_inaccessible_file: bool = False) -> None:
    """An inaccessible application process cannot be treated as idle."""
    root = Path(root)
    narrow_file = allow_unrelated_inaccessible_file and root.is_file() and not is_link(root)
    prefix = str(root).casefold()
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.pid == os.getpid():
            continue
        name = (proc.info.get('name') or '').casefold()
        application = is_application_process(name)
        values = []
        inaccessible = False
        for read in (proc.exe, proc.cwd, proc.cmdline):
            try:
                value = read()
                values.extend(value if isinstance(value, (list, tuple)) else [value])
            except psutil.NoSuchProcess:
                values = []
                break
            except psutil.AccessDenied:
                inaccessible = True
        if any(prefix in str(value).replace('/', os.sep).casefold() for value in values if value):
            raise ValueError('目录仍被运行进程引用')
        if application:
            if inaccessible and not narrow_file:
                raise ValueError('无法确认 ERP/测试进程已退出') from None
            try:
                values.extend(value for key, value in proc.environ().items()
                              if key.startswith(('ERP_', 'TM_ERP_'))
                              and key.endswith(('_PATH', '_DIR', '_ROOT', '_CONTROL')))
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                inaccessible = True
            if any(prefix in str(value).replace('/', os.sep).casefold() for value in values if value):
                raise ValueError('目录仍被运行进程引用')
            # Immutable backup ciphers have already passed ownership, identity,
            # maintenance-state and active-reference checks. A privileged parent
            # Python process may hide cwd/argv/env from the service UID while its
            # executable remains visible. Do not let that unrelated parent make
            # retention impossible; directory cleanup and wholly opaque processes
            # keep the original conservative refusal.
            if inaccessible and not (narrow_file and values):
                raise ValueError('无法确认 ERP/测试进程已退出') from None


def inspect_tree(root: Path) -> list[Path]:
    root = local_path(root)
    entries = []
    for folder, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(folder) / name
            if name == '.git' or is_link(path):
                raise ValueError('清理范围包含 Git 工作树或链接')
            entries.append(path)
    return entries


def remove_owned_tree(target: Path, parent: Path) -> int:
    """Only a direct child of the caller's validated owner can be removed."""
    parent = local_path(parent)
    target = local_path(target)
    if target.parent != parent or target == Path(target.anchor):
        raise ValueError('清理目标越过所属目录')
    if any((p / '.git').exists() for p in (target, *target.parents)):
        raise ValueError('不自动删除 Git 工作区中的任务文件')
    if not target.exists():
        return 0
    entries = inspect_tree(target)
    assert_idle(target)
    size = sum(p.stat().st_size for p in entries if p.is_file())
    # Windows refuses files still held by an application. Do not force handles
    # closed, change permissions, follow junctions, or invoke another shell.
    shutil.rmtree(target)
    return size

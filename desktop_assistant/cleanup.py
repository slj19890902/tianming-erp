"""Deletion primitives for explicitly owned, disposable local directories."""
from __future__ import annotations

import os
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


def assert_idle(root: Path) -> None:
    """An inaccessible application process cannot be treated as idle."""
    prefix = str(root).casefold()
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.pid == os.getpid():
            continue
        try:
            values = [proc.exe(), proc.cwd(), *proc.cmdline()]
            name = (proc.info.get('name') or '').casefold()
            if any(word in name for word in ('python', 'tianming', 'erp')):
                values.extend(value for key, value in proc.environ().items()
                              if key.startswith(('ERP_', 'TM_ERP_'))
                              and key.endswith(('_PATH', '_DIR', '_ROOT', '_CONTROL')))
            if any(prefix in str(value).replace('/', os.sep).casefold() for value in values if value):
                raise ValueError('目录仍被运行进程引用')
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            name = (proc.info.get('name') or '').casefold()
            if any(word in name for word in ('python', 'tianming', 'erp')):
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

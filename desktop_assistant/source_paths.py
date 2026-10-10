"""Interpret archived paths using source semantics, never the destination OS."""
from pathlib import Path, PurePosixPath, PureWindowsPath


def _normalized(path):
    parts = []
    for part in path.parts[1:]:
        if part == '..':
            if not parts:
                raise ValueError('源路径越过根目录')
            parts.pop()
        elif part != '.':
            if isinstance(path, PureWindowsPath) and ':' in part:
                raise ValueError('源路径包含Windows备用数据流')
            parts.append(part)
    return type(path)(path.anchor, *parts)


def relative_source_path(reference: str, source_root: str | Path) -> Path:
    """Return a portable relative path, rejecting other roots and ambiguity.

    Windows drive letters/UNC shares compare with Windows case semantics. No
    resolve/stat call is made against the archived machine or a network share.
    The caller must also check containment of the actual copied destination.
    """
    root_text = str(source_root)
    windows = bool(PureWindowsPath(root_text).drive) or '\\' in root_text
    path_type = PureWindowsPath if windows else PurePosixPath
    root = path_type(root_text)
    if not root.is_absolute():
        raise ValueError('归档源根目录必须为绝对路径')
    root = _normalized(root)
    if not reference or '\0' in reference:
        raise ValueError('源路径为空或无效')
    if not windows and PureWindowsPath(reference).drive:
        raise ValueError('源路径与归档平台不一致')
    source = path_type(reference)
    if not source.is_absolute():
        if source.anchor:
            raise ValueError('源路径缺少明确盘符或根目录')
        source = root / source
    source = _normalized(source)
    try:
        relative = source.relative_to(root)
    except ValueError:
        raise ValueError('源路径不在完整备份目录内') from None
    if not relative.parts:
        raise ValueError('附件路径不能指向根目录')
    return Path(*relative.parts)

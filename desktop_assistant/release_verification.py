"""Verify the complete code tree against an already authenticated manifest."""
import os
from pathlib import Path
import re
import stat

from desktop_assistant.storage import read_json, safe_name, sha

MANAGED = {'data': 'data', 'static/uploads': 'legacy_uploads',
           'factory_twin/data': 'factory_twin_data', 'logs': 'logs'}


def immutable(path, *, directory=False, link=False):
    from app.core.mac_service_keychain import _checked_path
    if link:
        # Its immutable, ACL-checked parent protects the symlink itself. Do not
        # follow it to demand root ownership of mutable business data.
        info = path.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise ValueError('已发布数据链接不受root保护')
        return
    mode = 0o755 if directory or os.stat(path, follow_symlinks=False).st_mode & 0o111 else 0o644
    with _checked_path(path, uid=0, directory=directory, mode=mode):
        pass


def verify_tree(root, release, manifest, *, root_owned=False, managed_links=True):
    root, release = Path(root), Path(release)
    expected = manifest.get('files')
    if not isinstance(expected, dict) or not expected:
        raise ValueError('签名发布文件清单无效')
    for name, digest in expected.items():
        safe_name(name)
        if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                or name in {'manifest.json', 'manifest.sig', '.env'}
                or any(name == p or name.startswith(p + '/') for p in MANAGED)):
            raise ValueError('签名程序文件清单含保留路径或无效摘要')
    if len({name.casefold() for name in expected}) != len(expected):
        raise ValueError('签名程序文件存在大小写路径冲突')
    if release.is_symlink() or release.is_junction() or not release.is_dir():
        raise ValueError('执行副本必须是完整的真实目录')
    if root_owned:
        immutable(release, directory=True)
    actual, links = set(), set()
    pending = [release]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                name = path.relative_to(release).as_posix()
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    if not managed_links or name not in MANAGED:
                        raise ValueError('发布目录含非托管链接')
                    target = root / 'shared' / MANAGED[name]
                    if (not target.is_dir() or target.resolve() != target.absolute()
                            or path.resolve() != target.absolute()):
                        raise ValueError('发布目录含非托管链接')
                    if root_owned:
                        immutable(path, link=True)
                    links.add(name)
                elif stat.S_ISDIR(info.st_mode):
                    if root_owned:
                        immutable(path, directory=True)
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    if root_owned:
                        immutable(path)
                    actual.add(name)
                else:
                    raise ValueError('发布目录含特殊文件，不能核验完整执行副本')
    if actual != set(expected) | {'manifest.json'}:
        raise ValueError('发布目录含未签名文件或缺少签名文件')
    if root_owned and managed_links and links != set(MANAGED):
        raise ValueError('root发布副本缺少预建托管数据链接')
    if read_json(release / 'manifest.json') != manifest:
        raise ValueError('后台服务缓存清单与签名原件不一致')
    for name, digest in expected.items():
        path = release / name
        if path.resolve() != release.absolute() / name or sha(path) != digest:
            raise ValueError('后台服务发布文件校验失败')
    return release

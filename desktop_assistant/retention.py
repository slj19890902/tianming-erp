"""Best-effort retention after a complete successful update, under its lock."""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.request import Request, build_opener, ProxyHandler

from desktop_assistant.cleanup import assert_idle, is_link, local_path, remove_owned_tree
from desktop_assistant.storage import archive_path, read_json, sha, signed_release_manifest, write_json

IDENTITY = re.compile(r'[a-f0-9]{64}')
STATE_KEYS = ('current', 'previous', 'schema_authority', 'migration_target')
LINKS = {'data': 'data', 'static/uploads': 'legacy_uploads',
         'factory_twin/data': 'factory_twin_data', 'logs': 'logs'}


def archive_synced(path: Path) -> bool:
    """Do not confuse a buffered ZSpace write with a completed NAS upload.

    Native network shares are server-backed. FUSE-rclone needs the local VFS
    upload queue to be empty; unavailable/unknown providers fail closed.
    This probe never purges cache or changes the mount configuration.
    """
    if os.name != 'nt':
        return False
    kernel = ctypes.windll.kernel32
    fs = ctypes.create_unicode_buffer(256)
    if not kernel.GetVolumeInformationW(str(path.anchor), None, 0, None, None, None, fs, len(fs)):
        return False
    if fs.value.casefold() == 'fuse-rclone':
        # This is the configured local ZSpace mount, not a remote admin API.
        if path.drive.casefold() != 'z:':
            return False
        opener = build_opener(ProxyHandler({}))
        try:
            request = Request('http://127.0.0.1:5572/vfs/stats', data=b'{}',
                              headers={'Content-Type': 'application/json'})
            with opener.open(request, timeout=2) as response:
                result = json.load(response)
            cache = result['diskCache']
            return (result['fs'] == 'uzmount:' and cache['uploadsQueued'] == 0
                    and cache['uploadsInProgress'] == 0 and cache['erroredFiles'] == 0)
        except (OSError, ValueError, KeyError):
            return False
    return kernel.GetDriveTypeW(str(path.anchor)) == 4


def _release_files(root: Path, release: Path, manifest: dict) -> list[Path]:
    expected = manifest['files']
    actual = set()
    links = []
    for folder, dirs, names in os.walk(release, followlinks=False):
        for name in list(dirs):
            path = Path(folder) / name
            relative = path.relative_to(release).as_posix()
            if is_link(path):
                if relative not in LINKS or path.resolve() != (root / 'shared' / LINKS[relative]).resolve():
                    raise ValueError('旧版本含非托管链接')
                links.append(path)
                dirs.remove(name)
            elif name == '.git':
                raise ValueError('旧版本含 Git 工作区')
        for name in names:
            path = Path(folder) / name
            relative = path.relative_to(release).as_posix()
            if is_link(path):
                raise ValueError('旧版本含链接文件')
            actual.add(relative)
            if relative in expected:
                if sha(path) != expected[relative]:
                    raise ValueError('旧版本文件已修改')
            elif relative != 'manifest.json' and not relative.endswith(('.pyc', '.pyo')):
                raise ValueError('旧版本含未登记文件')
    if set(expected) - actual or read_json(release / 'manifest.json') != manifest:
        raise ValueError('旧版本文件或版本清单不完整')
    return links


def cleanup_releases(manager, archive_dir: Path) -> dict:
    root = local_path(manager.root)
    state = manager.state
    if state.get('operation') not in ('update', 'rollback', 'migration_recovered'):
        raise ValueError('仅成功更新或恢复后执行版本保留')
    keep = {value for key in STATE_KEYS if (value := state.get(key))}
    if not state.get('current') or any(not isinstance(x, str) or not IDENTITY.fullmatch(x) for x in keep):
        raise ValueError('缺少可信的受保护版本状态')
    # Retain the signed compatibility fallback of protected versions as well.
    for identity in tuple(keep):
        package = local_path(root / 'packages' / (identity + '.zip'))
        if sha(package) != identity:
            raise ValueError('受保护版本安装包校验失败')
        manifest = signed_release_manifest(package, manager.public_key)
        fallback = (manifest.get('migration') or {}).get('rollback_package_sha256')
        if fallback:
            if not isinstance(fallback, str) or not IDENTITY.fullmatch(fallback):
                raise ValueError('兼容回退版本身份异常')
            keep.add(fallback)
    report = {'at': datetime.now(timezone.utc).isoformat(), 'protected': sorted(keep),
              'removed_releases': [], 'removed_packages': [], 'retained': {}}
    releases = local_path(root / 'releases')
    packages = local_path(root / 'packages')
    candidates = {p.name for p in releases.iterdir() if IDENTITY.fullmatch(p.name)}
    candidates.update(p.stem for p in packages.glob('*.zip') if IDENTITY.fullmatch(p.stem))
    for identity in sorted(candidates - keep):
        try:
            if any(manager.state.get(k) != state.get(k) for k in (*STATE_KEYS, 'operation')):
                raise ValueError('更新状态变化，停止清理')
            package = local_path(packages / (identity + '.zip'))
            if sha(package) != identity:
                raise ValueError('旧安装包缺失或哈希不匹配')
            manifest = signed_release_manifest(package, manager.public_key)
            release = local_path(releases / identity)
            if release.exists():
                assert_idle(release)
                links = _release_files(root, release, manifest)
                # Only unlink authenticated junctions; never traverse shared.
                for link in links:
                    if not is_link(link) or link.resolve() != (root / 'shared' / LINKS[link.relative_to(release).as_posix()]).resolve():
                        raise ValueError('托管链接发生变化')
                    link.unlink() if link.is_symlink() else link.rmdir()
                remove_owned_tree(release, releases)
                report['removed_releases'].append(identity)
            remote = archive_path(archive_dir / (identity + '.zip'))
            if not remote.is_file() or not archive_synced(remote):
                raise ValueError('NAS归档未完成或不可确认；保留可重建的本地安装包')
            if sha(remote) != identity or not archive_synced(remote):
                raise ValueError('NAS归档校验或同步状态变化；保留本地安装包')
            assert_idle(package)
            package.unlink()
            report['removed_packages'].append(identity)
        except (OSError, ValueError, KeyError) as error:
            report['retained'][identity] = str(error)
    return report


def after_update(manager, archive_dir: Path) -> None:
    """Never turn a successful activation into rollback because cleanup failed."""
    try:
        report = cleanup_releases(manager, archive_dir)
    except Exception as error:
        report = {'error': str(error), 'removed_releases': [], 'removed_packages': []}
    try:
        write_json(manager.root / 'control' / 'retention-latest.json', report)
    except OSError:
        pass

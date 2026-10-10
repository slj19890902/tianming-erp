"""Explicit release runtime selection; no system-Python fallback."""
import os
from pathlib import Path
import platform
import subprocess
import sys

from desktop_assistant.storage import sha


RUNTIMES = {
    'windows': 'runtime/python.exe',
    'macos-arm64': 'runtime/bin/python3.12',
}


def runtime_relative(manifest):
    kind = manifest.get('runtime_platform', 'windows')
    if not isinstance(kind, str) or kind not in RUNTIMES:
        raise ValueError('发布包运行平台不受支持')
    return RUNTIMES[kind]


def runtime_python(release, manifest, *, runnable=False):
    relative = runtime_relative(manifest)
    python = Path(release) / relative
    # The caller supplies the authenticated release manifest. Do not follow a
    # substituted executable or silently borrow an interpreter from the host.
    if python.is_symlink() or not python.is_file():
        raise ValueError('发布包运行环境缺失或被链接替换')
    if python.resolve() != (Path(release).resolve() / relative):
        raise ValueError('发布包运行环境目录含链接')
    expected = manifest.get('files', {}).get(relative)
    if not expected or sha(python) != expected:
        raise ValueError('发布包运行环境哈希不一致')
    if runnable:
        kind = manifest.get('runtime_platform', 'windows')
        if kind == 'windows' and sys.platform != 'win32':
            raise ValueError('Windows运行器仅可离线保全，不能在此平台启动')
        if kind == 'macos-arm64' and (sys.platform != 'darwin' or platform.machine() != 'arm64'):
            raise ValueError('此发布包要求原生Apple Silicon Mac')
        if kind == 'macos-arm64' and not os.access(python, os.X_OK):
            raise ValueError('Mac运行器缺少执行权限，须重新校验安装')
    return python


def prepare_runtime(release, manifest):
    python = runtime_python(release, manifest)
    # ZIP mode bits are not trusted. Grant only the known, hash-verified Python
    # entry point ordinary execute permission, never setuid/setgid permissions.
    if manifest.get('runtime_platform') == 'macos-arm64' and os.name != 'nt':
        python.chmod(0o755)
    return python


def process_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def link_managed_directory(link, target):
    link, target = Path(link), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        if link.resolve() != target.resolve():
            raise ValueError('程序目录存在非托管数据，拒绝覆盖')
        return
    if os.name == 'nt':
        quoted_link = str(link).replace("'", "''")
        quoted_target = str(target).replace("'", "''")
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                        f"New-Item -ItemType Junction -Path '{quoted_link}' -Target '{quoted_target}' | Out-Null"],
                       check=True, **process_options())
    else:
        link.symlink_to(target.resolve(), target_is_directory=True)

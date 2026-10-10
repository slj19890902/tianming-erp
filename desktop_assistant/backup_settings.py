"""Persist local backup settings without opening a Windows shell folder dialog."""
from pathlib import Path
import os
import sys
import uuid

from desktop_assistant.storage import read_json, write_json
from desktop_assistant.credential_store import protect_backup as protect, unprotect_backup as unprotect


def register_nightly(manager, executable):
    if sys.platform == 'darwin':
        identity = manager.state.get('current')
        if not identity:
            return {'registered':False,'reason':'native-runtime-pending'}
        from desktop_assistant.storage import sha, signed_release_manifest
        package = manager.root/'packages'/(identity+'.zip')
        if sha(package) != identity:
            raise ValueError('原备份运行包身份不一致')
        manifest = signed_release_manifest(package, manager.public_key)
        if manifest.get('runtime_platform', 'windows') == 'windows':
            # Imported Windows data needs a Mac NAS/password configuration in
            # order to back up before installing the native release. Persist the
            # configuration, but never claim an unusable scheduler was created.
            return {'registered':False,'reason':'native-runtime-pending'}
        from desktop_assistant.mac_nightly import register_locked
        return register_locked(manager)
    from desktop_assistant.windows import register_nightly as register_windows
    return register_windows(manager.root, executable)


def load_preferences(root):
    path = root / 'preferences.json'
    if not path.exists():
        return {}
    settings = read_json(path)
    if not isinstance(settings, dict):
        raise ValueError('原备份设置无法读取，未覆盖，请联系维护人员检查')
    return settings


def validate_backup_directory(directory, installation_root):
    nas = Path(directory.strip())
    if not nas.is_absolute() or not nas.is_dir():
        raise ValueError('备份文件夹不可用，请检查NAS连接及完整路径')
    nas = Path(os.path.abspath(nas))
    root = installation_root.resolve()
    if nas.is_relative_to(root):
        raise ValueError('备份不能放在助手安装目录内，请选择独立NAS文件夹')
    try:
        resolved = nas.resolve(strict=True)
    except OSError as error:
        # Some mounted NAS volumes support file IO/stat but not Windows final
        # volume-name resolution (1005). A distinct, known device proves the
        # destination cannot alias the local installation. Never blanket-ignore
        # resolution errors or allow an unknown/same-volume destination.
        if getattr(error, 'winerror', None) != 1005:
            raise
        destination_device, root_device = nas.stat().st_dev, root.stat().st_dev
        if not destination_device or not root_device or destination_device == root_device:
            raise ValueError('无法核实备份目录与助手目录的隔离，请选择独立NAS文件夹') from None
    else:
        if resolved.is_relative_to(root):
            raise ValueError('备份不能放在助手安装目录内，请选择独立NAS文件夹')
    return nas


def save_backup_settings(manager, directory, password, confirmation, executable):
    # Called only after Save, on a worker thread: a disconnected NAS must not
    # prevent the settings form from opening or stop the ERP service.
    with manager.lock():
        saved = load_preferences(manager.root)
        if password or confirmation:
            if password != confirmation or len(password) < 12:
                raise ValueError('两次恢复密码必须相同，且至少12个字符')
            protected = protect(password)
        elif saved.get('protected_password'):
            password = unprotect(saved['protected_password'])
            if len(password) < 12:
                raise ValueError('请重新设置至少12个字符的恢复密码')
            protected = saved['protected_password']
        else:
            raise ValueError('请填写恢复密码并再次输入确认，至少12个字符')
        if not directory.strip():
            raise ValueError('请填写NAS备份文件夹')
        nas = validate_backup_directory(directory, manager.root)
        probe = nas / ('assistant-write-check-' + uuid.uuid4().hex + '.tmp')
        try:
            with probe.open('xb') as stream:
                stream.write(b'ERP backup write check')
            probe.unlink()
        except OSError:
            raise ValueError('备份文件夹无法写入，请检查NAS连接和该文件夹的写入权限') from None
        # Do not overwrite working preferences when task registration fails.
        try:
            registration = register_nightly(manager, executable)
        except Exception:
            raise ValueError('自动备份任务未设置成功，原备份设置保留；请重试或联系维护人员') from None
        saved.update(nas=str(nas), protected_password=protected)
        if isinstance(registration, dict):
            saved['nightly_schedule'] = {'registered':bool(registration.get('registered')),
                                         'requires_user_login':True}
        write_json(manager.root / 'preferences.json', saved)
    if isinstance(registration, dict) and not registration.get('registered'):
        return '备份路径和凭据已安全保存；Mac原生运行包核验启用后须登记自动备份，当前尚未安排定时任务。'
    if not manager.state.get('current'):
        return '自动备份已设置。请返回工厂电脑页，点“首次接入并启用”。'
    return '已设置每天晚上11点自动备份。请返回工厂电脑页，点“现在备份一次”核对。'

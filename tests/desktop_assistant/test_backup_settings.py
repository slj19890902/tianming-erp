from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
import pytest
from desktop_assistant.gui import App


def test_backup_button_opens_inline_form_without_calling_native_chooser(tmp_path, monkeypatch):
    root = tmp_path / 'assistant'
    (root / 'control').mkdir(parents=True)
    manager = SimpleNamespace(root=root, state={'current': None, 'previous': None})
    window = tk.Tk()
    window.withdraw()
    monkeypatch.setattr('desktop_assistant.gui.filedialog.askdirectory', lambda **kw: pytest.fail('native chooser was called'))
    try:
        app = App(window, manager)
        button = next(b for key, b in app.action_buttons if key == 'configure')
        button.invoke()
        window.update()
        assert app.backup_panel.winfo_manager() == 'pack'
        assert not app.tabs.winfo_manager()
        assert not (root / 'preferences.json').exists()
        assert window.winfo_reqheight() <= 740
        app.close_backup_settings()
        assert app.tabs.winfo_manager() == 'pack'
        assert not (root / 'preferences.json').exists()
    finally:
        window.destroy()


@pytest.fixture
def settings_case(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from desktop_assistant import backup_settings as settings
    root = tmp_path / 'assistant'
    (root / 'control').mkdir(parents=True)
    nas = tmp_path / 'nas'
    nas.mkdir()
    manager = SimpleNamespace(root=root, state={'current': None}, lock=nullcontext)
    calls = []
    monkeypatch.setattr(settings, 'protect', lambda text: 'encrypted-fixture')
    monkeypatch.setattr(settings, 'unprotect', lambda text: 'existing-fixture-passphrase')
    monkeypatch.setattr(settings, 'register_nightly', lambda *args: calls.append(args))
    return manager, nas, calls


def test_success_encrypts_password_preserves_feed_and_explains_first_setup(settings_case):
    from desktop_assistant.backup_settings import save_backup_settings
    from desktop_assistant.storage import write_json, read_json
    manager, nas, calls = settings_case
    write_json(manager.root / 'preferences.json', {'release_feed': 'existing-release-feed'})
    result = save_backup_settings(manager, str(nas), 'fixture-passphrase', 'fixture-passphrase', Path('fixture.exe'))
    saved = read_json(manager.root / 'preferences.json')
    assert saved == {'release_feed': 'existing-release-feed', 'nas': str(nas), 'protected_password': 'encrypted-fixture'}
    assert len(calls) == 1 and list(nas.iterdir()) == []
    assert '首次接入' in result


@pytest.mark.parametrize(('password', 'confirmation', 'reason'), [
    ('', '', '请填写恢复密码'), ('short', 'short', '至少12'), ('fixture-passphrase', 'different', '相同')])
def test_invalid_password_never_creates_task_or_settings(settings_case, password, confirmation, reason):
    from desktop_assistant.backup_settings import save_backup_settings
    manager, nas, calls = settings_case
    with pytest.raises(ValueError, match=reason):
        save_backup_settings(manager, str(nas), password, confirmation, Path('fixture.exe'))
    assert calls == [] and not (manager.root / 'preferences.json').exists()


@pytest.mark.parametrize('directory', ['missing', 'inside', 'relative'])
def test_unavailable_or_unsafe_directory_never_registers_task(settings_case, directory):
    from desktop_assistant.backup_settings import save_backup_settings
    manager, nas, calls = settings_case
    path = {'missing': nas / 'unavailable', 'inside': manager.root, 'relative': Path('relative-backup')}[directory]
    with pytest.raises(ValueError, match='文件夹|安装目录'):
        save_backup_settings(manager, str(path), 'fixture-passphrase', 'fixture-passphrase', Path('fixture.exe'))
    assert calls == [] and not (manager.root / 'preferences.json').exists()


def test_task_registration_failure_keeps_original_preferences(settings_case, monkeypatch):
    from desktop_assistant import backup_settings as settings
    from desktop_assistant.storage import write_json
    manager, nas, calls = settings_case
    path = manager.root / 'preferences.json'
    write_json(path, {'nas': 'old-nas', 'protected_password': 'old-encrypted', 'release_feed': 'old-feed'})
    before = path.read_bytes()
    def fail(*args):
        raise OSError('task rejected')
    monkeypatch.setattr(settings, 'register_nightly', fail)
    with pytest.raises(ValueError, match='原备份设置保留'):
        settings.save_backup_settings(manager, str(nas), '', '', Path('fixture.exe'))
    assert path.read_bytes() == before


def test_empty_password_fields_keep_existing_recovery_password(settings_case):
    from desktop_assistant.backup_settings import save_backup_settings
    from desktop_assistant.storage import write_json, read_json
    manager, nas, calls = settings_case
    write_json(manager.root / 'preferences.json', {'protected_password': 'old-encrypted'})
    save_backup_settings(manager, str(nas), '', '', Path('fixture.exe'))
    assert read_json(manager.root / 'preferences.json')['protected_password'] == 'old-encrypted'
    assert len(calls) == 1


def test_callback_errors_are_visible_without_recording_sensitive_message(tmp_path):
    import sys
    root = tmp_path / 'assistant'
    (root / 'control').mkdir(parents=True)
    manager = SimpleNamespace(root=root, state={'current': None})
    window = tk.Tk()
    window.withdraw()
    try:
        app = App(window, manager)
        try:
            raise RuntimeError('private-password-fixture')
        except RuntimeError:
            app.report_callback_exception(*sys.exc_info())
        assert '界面操作未完成' in app.log.get()
        assert 'private-password-fixture' not in app.log.get()
        assert 'private-password-fixture' not in (root / 'control/last-ui-error.json').read_text()
    finally:
        window.destroy()


@pytest.mark.parametrize('succeed', [True, False])
def test_form_save_finishes_on_ui_thread_and_reports_result(tmp_path, monkeypatch, succeed):
    root = tmp_path / 'assistant'
    (root / 'control').mkdir(parents=True)
    manager = SimpleNamespace(root=root, state={'current': None})
    window = tk.Tk()
    window.withdraw()
    class Thread:
        def __init__(self, target, **kw): self.target = target
        def start(self): self.target()
    monkeypatch.setattr('desktop_assistant.gui.threading.Thread', Thread)
    monkeypatch.setattr('desktop_assistant.gui.sys.frozen', True, raising=False)
    def save(*args):
        if not succeed:
            raise ValueError('备份文件夹不可用')
        return '自动备份已设置'
    monkeypatch.setattr('desktop_assistant.backup_settings.save_backup_settings', save)
    try:
        app = App(window, manager)
        next(b for key, b in app.action_buttons if key == 'configure').invoke()
        app.backup_directory.set(str(tmp_path))
        app.backup_password.set('fixture-passphrase')
        app.backup_confirmation.set('fixture-passphrase')
        app.backup_save_button.invoke()
        assert app.busy and app.backup_cancel_button.instate(['disabled'])
        app.poll()
        assert not app.busy
        if succeed:
            assert app.backup_panel is None
            assert '完成：自动备份已设置' in app.log.get()
        else:
            assert app.backup_panel.winfo_manager() == 'pack'
            assert not app.backup_save_button.instate(['disabled'])
            assert '未完成：备份文件夹不可用' in app.log.get()
    finally:
        window.destroy()


def test_mounted_nas_volume_can_save_when_windows_cannot_resolve_volume(settings_case, monkeypatch):
    import stat
    from desktop_assistant import backup_settings as settings
    manager, nas, calls = settings_case
    original_resolve, original_stat = Path.resolve, Path.stat
    def resolve(path, *args, **kwargs):
        if path == nas:
            error = OSError('volume name resolution not supported')
            error.winerror = 1005
            raise error
        return original_resolve(path, *args, **kwargs)
    def file_stat(path, *args, **kwargs):
        if path == nas:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=123456789)
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    monkeypatch.setattr(Path, 'stat', file_stat)
    settings.save_backup_settings(manager, str(nas), 'fixture-passphrase', 'fixture-passphrase', Path('fixture.exe'))
    assert len(calls) == 1


@pytest.mark.parametrize(('code', 'device'), [(1005, 'same'), (1005, 0), (5, 123456789)])
def test_volume_resolution_fallback_never_bypasses_unknown_same_or_other_error(settings_case, monkeypatch, code, device):
    import stat
    from desktop_assistant import backup_settings as settings
    manager, nas, calls = settings_case
    original_resolve, original_stat = Path.resolve, Path.stat
    if device == 'same':
        device = manager.root.stat().st_dev
    def resolve(path, *args, **kwargs):
        if path == nas:
            error = OSError('fixture')
            error.winerror = code
            raise error
        return original_resolve(path, *args, **kwargs)
    def file_stat(path, *args, **kwargs):
        if path == nas:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=device)
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    monkeypatch.setattr(Path, 'stat', file_stat)
    with pytest.raises((ValueError, OSError)):
        settings.save_backup_settings(manager, str(nas), 'fixture-passphrase', 'fixture-passphrase', Path('fixture.exe'))
    assert calls == [] and not (manager.root / 'preferences.json').exists()

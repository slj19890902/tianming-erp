from pathlib import Path
from unittest.mock import patch, Mock
import sys

from desktop_assistant import gui


def test_double_click_uses_installed_exe_directory(tmp_path):
    executable = tmp_path / 'installed/TianmingERP-Assistant.exe'
    with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(executable)):
        assert gui.startup_root() == executable.parent


def test_explicit_shortcut_or_task_root_has_priority(tmp_path):
    with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(tmp_path / 'other/assistant.exe')):
        assert gui.startup_root(tmp_path / 'chosen') == tmp_path / 'chosen'


def test_development_default_is_not_python_runtime_directory(tmp_path):
    with patch.object(sys, 'frozen', False, create=True), patch.dict('os.environ', {'LOCALAPPDATA': str(tmp_path)}):
        assert gui.startup_root() == tmp_path / 'TianmingERP'


def test_missing_package_does_not_ask_for_source_or_start_import(tmp_path):
    app = Mock()
    app.manager.root = tmp_path
    with patch.object(gui.messagebox, 'showerror') as error, patch.object(gui.filedialog, 'askdirectory') as directory:
        gui.App.import_old(app)
    error.assert_called_once()
    assert '完整安装器' in error.call_args.args[1]
    assert str(tmp_path) in error.call_args.args[1]
    directory.assert_not_called()
    app.settings.assert_not_called()
    app.run.assert_not_called()

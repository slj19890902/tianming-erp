from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_SCRIPTS = PROJECT_ROOT / "scripts" / "windows"


def test_windows_start_launcher_scripts_exist() -> None:
    for name in (
        "start_erp.ps1",
        "start_erp.bat",
        "stop_erp.ps1",
        "stop_erp.bat",
        "install_desktop_shortcut.ps1",
        "install_startup_task.ps1",
    ):
        assert (WINDOWS_SCRIPTS / name).is_file()


def test_start_launcher_uses_project_venv_and_alembic_upgrade() -> None:
    source = (WINDOWS_SCRIPTS / "start_erp.ps1").read_text(encoding="utf-8")

    for marker in (
        '$ProjectRoot = "D:\\纸箱厂erp软件搭建"',
        r".venv\Scripts\python.exe",
        "-m alembic upgrade head",
        "-m alembic current",
        "app.main:app",
        '"--host", "127.0.0.1"',
        '"--port", "8000"',
        "http://127.0.0.1:8000/api/health",
        "http://127.0.0.1:8000/",
        "erp_startup.log",
        "ERP 已经在运行",
        "ERP 启动失败",
    ):
        assert marker in source


def test_stop_launcher_confirms_before_killing_erp_port() -> None:
    source = (WINDOWS_SCRIPTS / "stop_erp.ps1").read_text(encoding="utf-8")

    for marker in (
        "Get-NetTCPConnection",
        "Read-Host",
        "Stop-Process",
        "ERP 服务已关闭",
        "ERP 当前没有运行",
    ):
        assert marker in source


def test_desktop_shortcut_and_startup_task_installers_point_to_launcher() -> None:
    shortcut = (WINDOWS_SCRIPTS / "install_desktop_shortcut.ps1").read_text(
        encoding="utf-8"
    )
    startup = (WINDOWS_SCRIPTS / "install_startup_task.ps1").read_text(
        encoding="utf-8"
    )

    assert "打开天明ERP.lnk" in shortcut
    assert "scripts\\windows\\start_erp.bat" in shortcut
    assert "Tianming ERP Auto Start" in startup
    assert "AtLogOn" in startup


def test_user_startup_guide_is_plain_language() -> None:
    guide = (PROJECT_ROOT / "docs" / "USER_STARTUP_GUIDE.md").read_text(
        encoding="utf-8"
    )

    for marker in (
        "双击桌面上的【打开天明ERP】",
        "ERP 启动失败",
        "开机登录后自动启动 ERP",
        "ERP 数据库文件不要手动删除",
    ):
        assert marker in guide

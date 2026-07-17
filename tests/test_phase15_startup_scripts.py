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
        '$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path',
        r".venv\Scripts\python.exe",
        'Invoke-PythonCommand -Label "alembic_upgrade"',
        'Invoke-PythonCommand -Label "alembic_current"',
        '"-m", "alembic", "upgrade", "head"',
        '"-m", "alembic", "current"',
        "app.main:app",
        '"--host", $BindHost',
        '"--port", $ErpPort.ToString()',
        "from app.core.config import load_settings",
        "ERP_HEALTH_URL",
        "ERP_BROWSER_URL",
        "生产环境必须配置 ERP_HEALTH_URL",
        "生产环境必须配置 ERP_BROWSER_URL",
        "erp_startup.log",
        "ERP already running",
        "ERP startup failed",
    ):
        assert marker in source


def test_stop_launcher_confirms_before_killing_erp_port() -> None:
    source = (WINDOWS_SCRIPTS / "stop_erp.ps1").read_text(encoding="utf-8")

    for marker in (
        "Get-NetTCPConnection",
        "Read-Host",
        "Stop-Process",
        "ERP is not running",
        "Possible ERP processes:",
    ):
        assert marker in source


def test_desktop_shortcut_and_startup_task_installers_point_to_launcher() -> None:
    shortcut = (WINDOWS_SCRIPTS / "install_desktop_shortcut.ps1").read_text(
        encoding="utf-8"
    )
    startup = (WINDOWS_SCRIPTS / "install_startup_task.ps1").read_text(
        encoding="utf-8"
    )

    assert "Open Tianming ERP.lnk" in shortcut
    assert "scripts\\windows\\start_erp.bat" in shortcut
    assert "Tianming ERP Auto Start" in startup
    assert "AtLogOn" in startup


def test_user_startup_guide_is_plain_language() -> None:
    guide = (PROJECT_ROOT / "docs" / "USER_STARTUP_GUIDE.md").read_text(
        encoding="utf-8"
    )

    for marker in (
        "平时怎么打开 ERP",
        "为什么浏览器不能自己启动 ERP",
        "logs\\erp_startup.log",
        "Open Tianming ERP.lnk",
        "开机自启是可选的",
        ".venv",
        "data\\carton_erp.sqlite3",
    ):
        assert marker in guide

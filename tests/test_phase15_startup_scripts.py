from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


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
        "ensure_erp_running.ps1",
    ):
        assert (WINDOWS_SCRIPTS / name).is_file()


def test_start_launcher_uses_project_venv_and_read_only_revision_gate() -> None:
    source = (WINDOWS_SCRIPTS / "start_erp.ps1").read_text(encoding="utf-8")

    for marker in (
        '$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path',
        "Set-Location -LiteralPath $ProjectRoot",
        r".venv\Scripts\python.exe",
        'Invoke-PythonCommand `',
        '"startup_revision_check"',
        '"check-startup"',
        '"Factory launcher requires ERP_ENVIRONMENT=production',
        "app.main:app",
        '"--host", $BindHost',
        '"--port", $ErpPort.ToString()',
        '"--app-dir", $ProjectRoot',
        '"--workers", "1"',
        "ERP must run with exactly one worker",
        "from app.core.config import load_settings",
        "base64.b64encode",
        "FromBase64String",
        "ERP_HEALTH_URL",
        "ERP_BROWSER_URL",
        "ERP_PRODUCTION_TRANSPORT",
        'ProductionTransport -eq "https_proxy"',
        'ProductionTransport -eq "lan_http"',
        "https_proxy requires ERP_HEALTH_URL",
        "lan_http requires ERP_HEALTH_URL and ERP_BROWSER_URL",
        '$LocalHealthUrl = "http://127.0.0.1:$ErpPort/api/health"',
        "Test-LocalErpRunning",
        "Confirm-ProductionExternalHealth",
        "-MaximumRedirection 0",
        "erp_startup.log",
        "ERP already running",
        "ERP startup failed",
    ):
        assert marker in source
    assert "alembic upgrade head" not in source
    assert '"-m", "alembic", "upgrade"' not in source


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


def test_desktop_shortcut_and_startup_task_installs_health_guard() -> None:
    shortcut = (WINDOWS_SCRIPTS / "install_desktop_shortcut.ps1").read_text(
        encoding="utf-8"
    )
    startup = (WINDOWS_SCRIPTS / "install_startup_task.ps1").read_text(
        encoding="utf-8"
    )

    assert "Open Tianming ERP.lnk" in shortcut
    assert "scripts\\windows\\start_erp.bat" in shortcut
    guard = (WINDOWS_SCRIPTS / "ensure_erp_running.ps1").read_text(
        encoding="utf-8"
    )

    assert 'TaskName = "TianmingERP"' in startup
    assert "ensure_erp_running.ps1" in startup
    assert "AtStartup" in startup
    assert "AtLogOn" in startup
    assert "SYSTEM" in startup
    assert "RestartCount 999" in startup
    assert "erp_maintenance.lock" in guard
    assert "Global\\TianmingErpHealthGuard" in guard
    assert "start_erp.ps1" in guard
    assert "alembic" not in guard.lower()


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
        "健康守护",
        "erp_health_guard.log",
        ".venv",
        "data\\carton_erp.sqlite3",
    ):
        assert marker in guide


def test_legacy_background_launcher_cannot_bypass_hardened_runtime_config() -> None:
    legacy = (
        PROJECT_ROOT / "scripts" / "admin" / "start_erp_background.ps1"
    ).read_text(encoding="utf-8")
    hardened = (WINDOWS_SCRIPTS / "start_erp.ps1").read_text(encoding="utf-8")
    updater = (
        PROJECT_ROOT / "scripts" / "admin" / "release_erp.ps1"
    ).read_text(encoding="utf-8")

    assert "scripts\\windows\\start_erp.ps1" in legacy
    assert "-NoBrowser" in legacy
    assert "0.0.0.0" not in legacy
    assert "http://127.0.0.1" not in legacy
    assert "ERP_HEALTH_URL" in hardened
    assert "ERP_BROWSER_URL" in hardened
    assert "Stop-Process -Id $process.Id" in hardened
    assert "$process -and -not $process.HasExited" in hardened
    assert "base64.b64encode" in updater
    assert "FromBase64String" in updater
    assert "from app.core.config import load_settings" in updater
    assert "-LocalPort $ErpPort" in updater
    assert "https_proxy 正式发布要求 HTTPS ERP_HEALTH_URL 与 ERP_BROWSER_URL" in updater
    assert "lan_http 正式发布要求 HTTP ERP_HEALTH_URL 与 ERP_BROWSER_URL" in updater


def test_startup_readiness_is_loopback_first_and_external_check_is_advisory() -> None:
    source = (WINDOWS_SCRIPTS / "start_erp.ps1").read_text(encoding="utf-8")
    local_probe = source.split("function Test-LocalErpRunning", 1)[1].split(
        "function Test-ExternalErpHealth",
        1,
    )[0]

    assert "$LocalHealthUrl" in local_probe
    assert "$ExternalHealthUrl" not in local_probe
    ready_gate = source.index('if (-not $ready)')
    external_after_ready = source.index(
        "Confirm-ProductionExternalHealth",
        ready_gate,
    )
    success = source.index('Write-Log "ERP startup succeeded."', ready_gate)
    assert ready_gate < external_after_ready < success
    assert "the local ERP process remains running" in source


def test_update_script_validates_listener_identity_before_stop_process() -> None:
    source = (
        PROJECT_ROOT / "scripts" / "admin" / "release_erp.ps1"
    ).read_text(encoding="utf-8")

    for marker in (
        "Get-CimInstance",
        "Win32_Process",
        "ExecutablePath",
        '$expectedErpPython',
        '$expectedBasePython',
        ".venv\\Scripts\\python.exe",
        '"uvicorn"',
        '"app.main:app"',
        "Test-ErpCommandLineIdentity",
        "--app-dir",
        "--workers",
        "Get-ValidatedErpProcess -ProcessId $processId",
    ):
        assert marker in source
    validation = source.index("Get-ValidatedErpProcess -ProcessId $processId")
    stop = source.index("Stop-Process -Id $processId", validation)
    assert validation < stop
    assert "Find-Python" not in source
    assert "Get-Command python" not in source
    assert "$env:LOCALAPPDATA" not in source
    assert "禁止回退到全局 Python" in source


def test_update_script_command_identity_rejects_decoy_argument_sequence() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is not available")

    script = PROJECT_ROOT / "scripts" / "admin" / "release_erp.ps1"
    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    quoted_script = str(script).replace("'", "''")
    quoted_root = str(PROJECT_ROOT).replace("'", "''")
    valid = (
        f'"{python}" -X utf8 -m uvicorn app.main:app '
        f'--app-dir "{PROJECT_ROOT}" --host 127.0.0.1 --port 8000 --workers 1'
    ).replace("'", "''")
    malicious = (
        f'"{python}" -m uvicorn other:app --root-path app.main:app '
        f'--app-dir "{PROJECT_ROOT}" --workers 1'
    ).replace("'", "''")
    command = (
        f". '{quoted_script}' -LibraryOnly; "
        f"$valid = Test-ErpCommandLineIdentity -CommandLine '{valid}' "
        f"-ExpectedAppDir '{quoted_root}'; "
        f"$malicious = Test-ErpCommandLineIdentity -CommandLine '{malicious}' "
        f"-ExpectedAppDir '{quoted_root}'; "
        'Write-Output ("{0},{1}" -f $valid, $malicious)'
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "True,False"

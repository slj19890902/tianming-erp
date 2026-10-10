from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "scripts" / "windows" / "run_email_order_intake.ps1").read_text(
    encoding="utf-8"
)
INSTALLER = (
    ROOT / "scripts" / "windows" / "install_email_order_intake_task.ps1"
).read_text(encoding="utf-8")
CONFIGURATOR = (
    ROOT / "scripts" / "windows" / "configure_email_order_intake.ps1"
).read_text(encoding="utf-8")


def test_scheduler_defaults_to_thirty_minutes_without_putting_secrets_in_task() -> None:
    assert "[int]$IntervalMinutes = 30" in INSTALLER
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in INSTALLER
    assert "-MultipleInstances IgnoreNew" in INSTALLER
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 20)" in INSTALLER
    assert "ERP_EMAIL_IMAP_PASSWORD" not in INSTALLER
    assert "password_file" not in INSTALLER.lower()


def test_scheduler_runs_one_shot_service_and_handles_busy_tick_safely() -> None:
    assert "scripts\\run_email_order_intake.py" in RUNNER
    assert "[string]$PythonExecutable" in RUNNER
    assert "& $ResolvedPython -X utf8 $Runner" in RUNNER
    assert "& py " not in RUNNER
    assert "$exitCode -eq 3" in RUNNER
    assert "email_order_intake_scheduler.log" in RUNNER


def test_scheduler_pins_exact_project_python_and_scripts_are_powershell_51_safe() -> None:
    assert "-PythonExecutable `\"$escapedPython`\"" in INSTALLER
    assert "app.main.__file__" in INSTALLER
    assert "TM_ERP_APP_MAIN=" in INSTALLER
    assert ".venv\\Scripts\\python.exe" in INSTALLER
    assert "[System.Text.UTF8Encoding]::new" not in CONFIGURATOR
    assert "New-Object System.Text.UTF8Encoding" in CONFIGURATOR
    for script in (RUNNER, INSTALLER, CONFIGURATOR):
        assert "??" not in script


def test_installer_states_that_formal_order_is_never_automatic() -> None:
    assert "Never creates formal orders automatically" in INSTALLER
    assert "正式订单仍需人工确认" in INSTALLER
    assert "需要保持该 Windows 用户登录" in INSTALLER


def test_configurator_uses_hidden_password_and_restricted_external_secret_file() -> None:
    assert "Read-Host \"请输入邮箱授权密码（输入内容不会显示）\" -AsSecureString" in CONFIGURATOR
    assert "email_imap_password.txt" in CONFIGURATOR
    assert "icacls.exe" in CONFIGURATOR
    assert CONFIGURATOR.index("icacls.exe $secretDir") < CONFIGURATOR.index(
        "[System.IO.File]::WriteAllText"
    )
    assert "icacls.exe $passwordFile" in CONFIGURATOR
    assert "ERP_EMAIL_IMAP_PASSWORD_FILE" in CONFIGURATOR
    assert "ERP_EMAIL_INTAKE_ENABLED" in CONFIGURATOR
    assert ".env" not in CONFIGURATOR


def test_one_shot_runner_turns_invalid_configuration_into_readable_json() -> None:
    script = (ROOT / "scripts" / "run_email_order_intake.py").read_text(
        encoding="utf-8"
    )
    assert "邮箱收单配置无效" in script
    assert "except (OSError, RuntimeError, ValueError)" in script

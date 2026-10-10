import os
from pathlib import Path

import pytest

from desktop_assistant.onboarding import stop_original


def fixture_script(source, release, *, reject=False):
    trace = str(source / 'trace.txt').replace("'", "''")
    script = f"""param([switch]$LibraryOnly)
function Initialize-ReleaseRuntime {{ $script:ErpPort=18000; Set-Content -LiteralPath '{trace}' -Value 'init' }}
function Get-NetTCPConnection {{ [pscustomobject]@{{OwningProcess=1234}} }}
function Get-ValidatedErpProcess {{ param([int]$ProcessId)
    {'throw "identity-rejected"' if reject else f"Add-Content -LiteralPath '{trace}' -Value 'validated'"}
}}
function Set-ErpMaintenanceLock {{ Add-Content -LiteralPath '{trace}' -Value 'lock' }}
function Stop-ErpService {{ Add-Content -LiteralPath '{trace}' -Value 'stop' }}
function Assert-ErpStopped {{ Add-Content -LiteralPath '{trace}' -Value 'stopped' }}
"""
    for root in (source, release):
        path = root / 'scripts/admin/release_erp.ps1'
        path.parent.mkdir(parents=True)
        path.write_text(script, encoding='utf-8-sig')
    return source / 'trace.txt'


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell integration')
def test_verified_script_runs_in_real_windows_child_with_chinese_path(tmp_path):
    source, release = tmp_path / '原ERP', tmp_path / 'signed'
    trace = fixture_script(source, release)
    stop_original(source, release)
    assert trace.read_text().splitlines() == ['init', 'validated', 'lock', 'stop', 'stopped']


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell integration')
def test_identity_rejection_still_prevents_lock_and_stop(tmp_path):
    source, release = tmp_path / '原ERP', tmp_path / 'signed'
    trace = fixture_script(source, release, reject=True)
    with pytest.raises(ValueError, match='停服检查'):
        stop_original(source, release)
    assert trace.read_text().splitlines() == ['init']


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell integration')
def test_readonly_check_does_not_enter_stop_phase(tmp_path):
    from desktop_assistant.onboarding import check_original_runtime
    source, release = tmp_path / 'source', tmp_path / 'signed'
    trace = fixture_script(source, release)
    check_original_runtime(source, release)
    assert trace.read_text().splitlines() == ['init', 'validated']


def test_child_uses_only_windows_modules_without_changing_parent(tmp_path, monkeypatch):
    from desktop_assistant.windows import run_maintenance_powershell
    from unittest.mock import patch
    monkeypatch.setenv('PSModulePath', 'foreign-powershell-7-modules')
    monkeypatch.setenv('SystemRoot', str(tmp_path/'synthetic-windows'))
    monkeypatch.setattr('subprocess.CREATE_NO_WINDOW', 0x08000000, raising=False)
    with patch('desktop_assistant.windows.subprocess.run') as run:
        run_maintenance_powershell('Write-Output fixture', tmp_path)
    args, options = run.call_args
    assert args[0][args[0].index('-ExecutionPolicy')+1] == 'RemoteSigned'
    assert options['env']['PSModulePath'] == str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/Modules')
    assert os.environ['PSModulePath'] == 'foreign-powershell-7-modules'


def test_script_policy_failure_is_actionable_without_echoing_stderr(tmp_path):
    from unittest.mock import patch
    from types import SimpleNamespace
    source, release = tmp_path / 'source', tmp_path / 'signed'
    fixture_script(source, release)
    with patch('desktop_assistant.onboarding.run_maintenance_powershell', return_value=SimpleNamespace(returncode=1, stderr=b'UnauthorizedAccess secret-fixture')):
        with pytest.raises(ValueError, match='Windows阻止') as error:
            stop_original(source, release)
    assert 'secret-fixture' not in str(error.value)


def test_first_setup_never_downgrades_source_program(tmp_path):
    from desktop_assistant.onboarding import check_source_version
    (tmp_path/'app').mkdir()
    (tmp_path/'app/version.py').write_text("APP_VERSION = 'v0.22.377'\n")
    with pytest.raises(ValueError, match='比原ERP.*旧'):
        check_source_version(tmp_path, {'version':'v0.22.376'})
    check_source_version(tmp_path, {'version':'v0.22.377'})
    check_source_version(tmp_path, {'version':'v0.22.378'})
    (tmp_path/'app/version.py').unlink()
    with pytest.raises(ValueError, match='无法核对'):
        check_source_version(tmp_path, {'version':'v0.22.378'})

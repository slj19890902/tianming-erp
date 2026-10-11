param(
    [ValidateSet('Check','Install','Run')][string]$Action = 'Check',
    [Parameter(Mandatory=$true)][string]$TaskParent,
    [Parameter(Mandatory=$true)][string]$ExpectedSourceCommit,
    [Parameter(Mandatory=$true)][string]$ExpectedHarnessCommit,
    [string]$BasePython = 'C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe',
    [string]$Wheelhouse,
    [switch]$DependencyInstallationApproved
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Kit = $PSScriptRoot
function Assert-NoReparse([string]$Path) {
    $Item = Get-Item -LiteralPath $Path -Force
    while ($null -ne $Item) {
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Directory links are not allowed for the kit, interpreter or task parent'
        }
        if ($Item -is [IO.DirectoryInfo]) { $Item = $Item.Parent } else { $Item = $Item.Directory }
    }
}
Assert-NoReparse $Kit
Assert-NoReparse $TaskParent
Assert-NoReparse $BasePython
if (-not (Test-Path -LiteralPath $TaskParent -PathType Container)) { throw 'Approved task parent missing' }
if ($TaskParent -notmatch '^[A-Za-z]:\\') { throw 'Use an approved local fixed-disk parent' }
if ($ExpectedSourceCommit -notmatch '^[a-f0-9]{40}$' -or $ExpectedHarnessCommit -notmatch '^[a-f0-9]{40}$') {
    throw 'Supply both exact reviewed commits'
}
$Manifest = Get-Content -LiteralPath (Join-Path $Kit 'manifest.json') -Raw | ConvertFrom-Json
if ($Manifest.source_commit -ne $ExpectedSourceCommit -or $Manifest.harness_commit -ne $ExpectedHarnessCommit) {
    throw 'Kit commits differ from the independent delivery receipt'
}
foreach ($Entry in $Manifest.files.PSObject.Properties) {
    $File = Join-Path $Kit $Entry.Name
    Assert-NoReparse $File
    if ((Get-Item -LiteralPath $File).Length -ne $Entry.Value.bytes -or
        (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Entry.Value.sha256) {
        throw 'Kit content changed'
    }
}
& $BasePython -I -B -c 'import sys; assert sys.version_info[:2]==(3,12); print(sys.version)'
if ($LASTEXITCODE -ne 0) { throw 'Approved Python 3.12 is required; do not install a replacement automatically' }
$CanonicalParentJson = & $BasePython -I -B -c 'from pathlib import Path; import json,sys; parent=Path(sys.argv[1]).resolve(strict=True); kit=Path(sys.argv[2]).resolve(strict=True); assert not parent.is_relative_to(kit); print(json.dumps(str(parent),ensure_ascii=True))' $TaskParent $Kit
if ($LASTEXITCODE -ne 0) { throw 'Keep the task parent outside the immutable kit' }
$CanonicalParent = $CanonicalParentJson | ConvertFrom-Json
if ($CanonicalParent -isnot [string] -or [string]::IsNullOrWhiteSpace($CanonicalParent)) { throw 'Invalid canonical task parent' }
$TaskParent = $CanonicalParent
$Task = Join-Path $TaskParent ('factory-synthetic-' + $ExpectedHarnessCommit.Substring(0,8))
$Venv = Join-Path $Task 'venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
function Assert-PrivateVenv {
    Assert-NoReparse $Task
    Assert-NoReparse $Python
    & $Python -I -B -c 'from pathlib import Path; import sys; assert Path(sys.prefix).resolve()==Path(sys.argv[1]).resolve(strict=True); assert sys.prefix!=sys.base_prefix; assert sys.version_info[:2]==(3,12)' $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Interpreter is not the approved task Python 3.12 venv' }
}
if ($Action -eq 'Check') {
    Write-Output ('Read-only checks passed. Task location: ' + $Task)
    Write-Output 'No environment created or dependency installed. Install requires separate approval.'
    exit 0
}
if ($Action -eq 'Install') {
    if (-not $DependencyInstallationApproved) { throw 'Dependency installation has not been approved' }
    if (Test-Path -LiteralPath $Task) { throw 'Task exists; preserve evidence and do not recreate it' }
    if (-not $Wheelhouse) { throw 'Provide the independently verified offline wheel directory' }
    Assert-NoReparse $Wheelhouse
    $Audit = Get-Content -LiteralPath (Join-Path $Kit 'windows-wheel-audit.json') -Raw | ConvertFrom-Json
    foreach ($Wheel in $Audit.wheels) {
        $File = Join-Path $Wheelhouse $Wheel.filename
        Assert-NoReparse $File
        if ((Get-Item -LiteralPath $File).Length -ne $Wheel.bytes -or
            (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Wheel.sha256) {
            throw 'Offline dependency does not match the reviewed PyPI wheel'
        }
    }
    # Changes are confined to this newly approved directory and this child shell.
    New-Item -ItemType Directory -Path $Task | Out-Null
    $SetupTemp = Join-Path $Task 'setup-temp'
    New-Item -ItemType Directory -Path $SetupTemp | Out-Null
    $env:TEMP = $SetupTemp
    $env:TMP = $SetupTemp
    $env:PYTHONDONTWRITEBYTECODE = '1'
    # --isolated still reads global/site pip.ini. Disable all inherited config,
    # including target/prefix/log, without modifying any external config file.
    Get-ChildItem Env:PIP_* | ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }
    $env:PIP_CONFIG_FILE = 'nul'
    & $BasePython -I -B -m venv $Venv *> (Join-Path $Task 'venv-create.log')
    if ($LASTEXITCODE -ne 0) { throw 'Private venv creation failed; preserve the new task' }
    Assert-PrivateVenv
    & $Python -I -B -c 'import os,sys; from pip._internal.configuration import Configuration; assert os.environ.get(sys.argv[1])==os.devnull; config=Configuration(isolated=True); config.load(); assert not list(config.items())' PIP_CONFIG_FILE *> (Join-Path $Task 'pip-config-check.log')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot prove pip configuration is empty; preserve the task without installing dependencies' }
    & $Python -I -B -m pip --isolated --require-virtualenv --disable-pip-version-check install --no-index --find-links $Wheelhouse --require-hashes --only-binary=:all: --no-cache-dir --no-input -r (Join-Path $Kit 'requirements.txt') *> (Join-Path $Task 'dependency-install.log')
    if ($LASTEXITCODE -ne 0) { throw 'Offline install failed; preserve the task, do not change the base interpreter' }
    & $Python -I -B -m pip --isolated --require-virtualenv --disable-pip-version-check check *> (Join-Path $Task 'dependency-check.log')
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency check failed' }
    Write-Output 'Private test venv prepared. No tests or production actions have run.'
    exit 0
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Approved private test venv is not ready' }
Assert-NoReparse $Task
Assert-NoReparse $Python
Assert-PrivateVenv
$Run = Join-Path $Task 'execution'
& $Python -I -B (Join-Path $Kit 'run_tests.py') --work-root $Run --expected-source-commit $ExpectedSourceCommit --expected-harness-commit $ExpectedHarnessCommit
exit $LASTEXITCODE

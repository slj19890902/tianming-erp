param(
    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 30,
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Runner = Join-Path $ProjectRoot "scripts\windows\run_email_order_intake.ps1"
$TaskName = "Tianming ERP Email Order Intake"

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "未找到邮箱收单运行脚本：$Runner。请确认正在 ERP 项目目录中安装。"
}

# Resolve one concrete interpreter at install time.  The scheduled runtime is
# then pinned to that absolute path and never depends on whichever `py` or
# `python` happens to be first on PATH later.
if (-not $PythonExecutable) {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $PythonExecutable = $venvPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            $PythonExecutable = $pythonCommand.Source
        } else {
            $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
            if (-not $launcher) {
                throw "未找到项目 Python。请使用 -PythonExecutable 指定 python.exe 的绝对路径。"
            }
            $PythonExecutable = (& $launcher.Source -3 -c "import sys; print(sys.executable)").Trim()
            if ($LASTEXITCODE -ne 0 -or -not $PythonExecutable) {
                throw "无法从 Python 启动器解析项目解释器，请使用 -PythonExecutable 显式指定。"
            }
        }
    }
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "指定的 Python 解释器不存在：$PythonExecutable"
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path

$previousProjectRoot = $env:TM_ERP_PROJECT_ROOT
try {
    $env:TM_ERP_PROJECT_ROOT = $ProjectRoot
    $probeOutput = @(& $PythonExecutable -X utf8 -c "import os,pathlib,sys; root=os.environ['TM_ERP_PROJECT_ROOT']; sys.path.insert(0,root); import app.main; print('TM_ERP_APP_MAIN=' + str(pathlib.Path(app.main.__file__).resolve()))" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "指定 Python 无法导入当前 ERP 项目。"
    }
    $mainMarker = $probeOutput | Where-Object { [string]$_ -like "TM_ERP_APP_MAIN=*" } | Select-Object -Last 1
    if (-not $mainMarker) {
        throw "指定 Python 未返回当前 ERP 项目路径。"
    }
    $importedMain = ([string]$mainMarker).Substring("TM_ERP_APP_MAIN=".Length).Trim()
} finally {
    $env:TM_ERP_PROJECT_ROOT = $previousProjectRoot
}
$expectedMain = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot "app\main.py")).Path
if (-not [string]::Equals($importedMain, $expectedMain, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Python 导入了错误项目：$importedMain；预期：$expectedMain"
}

# Credentials are intentionally not passed in the task command line. The ERP_EMAIL_*
# settings and the password-file path must be configured in the service account's
# environment before this task is installed.
$escapedRunner = $Runner.Replace('"', '""')
$escapedPython = $PythonExecutable.Replace('"', '""')
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$escapedRunner`" -PythonExecutable `"$escapedPython`""

$startAt = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $startAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel LeastPrivilege

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Every $IntervalMinutes minutes, fetch allowlisted order attachments into ERP review drafts. Never creates formal orders automatically." `
    -Force | Out-Null

Write-Host "邮箱订单收件任务已创建：$TaskName"
Write-Host "执行间隔：每 $IntervalMinutes 分钟"
Write-Host "首次执行：$startAt"
Write-Host "固定 Python：$PythonExecutable"
Write-Host "当前采用登录用户任务：这台 ERP 电脑需要保持该 Windows 用户登录。"
Write-Host "任务最长执行 20 分钟，重复触发会自动跳过，不会并行收件。"
Write-Host "任务只生成待核对草稿，正式订单仍需人工确认。"

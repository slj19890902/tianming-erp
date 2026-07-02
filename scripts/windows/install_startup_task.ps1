$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$StartBat = Join-Path $ProjectRoot "scripts\windows\start_erp.bat"
$TaskName = "Tianming ERP Auto Start"

if (-not (Test-Path -LiteralPath $StartBat)) {
    throw "Launcher not found: $StartBat"
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$StartBat`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel LeastPrivilege

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Tianming ERP auto start on logon" `
        -Force | Out-Null
    Write-Host "Auto start task created: Tianming ERP Auto Start"
} catch {
    throw "Failed to create auto start task."
}

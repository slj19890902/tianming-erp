$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\纸箱厂erp软件搭建"
$StartBat = Join-Path $ProjectRoot "scripts\windows\start_erp.bat"
$TaskName = "Tianming ERP Auto Start"

if (-not (Test-Path -LiteralPath $StartBat)) {
    throw "没有找到启动脚本：$StartBat"
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
        -Description "天明 ERP 登录后自动启动" `
        -Force | Out-Null
    Write-Host "已设置开机登录后自动启动 ERP。以后电脑开机后，稍等一会儿再打开网页即可。"
} catch {
    throw "开机自启设置失败，请把这个窗口截图发给管理员。"
}

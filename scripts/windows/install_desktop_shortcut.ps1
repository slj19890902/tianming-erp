$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Target = Join-Path $ProjectRoot "scripts\windows\start_erp.bat"
$FaultCheckTarget = Join-Path $ProjectRoot "scripts\windows\erp_fault_check.bat"
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "Open Tianming ERP.lnk"
$FaultCheckShortcutPath = Join-Path $Desktop "天明ERP故障检查.lnk"

if (-not (Test-Path -LiteralPath $Target)) {
    throw "Launcher not found: $Target"
}
if (-not (Test-Path -LiteralPath $FaultCheckTarget)) {
    throw "Fault check launcher not found: $FaultCheckTarget"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $Target
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.WindowStyle = 1
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll, 0"
$shortcut.Save()

$faultShortcut = $shell.CreateShortcut($FaultCheckShortcutPath)
$faultShortcut.TargetPath = $FaultCheckTarget
$faultShortcut.WorkingDirectory = $ProjectRoot
$faultShortcut.WindowStyle = 1
$faultShortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll, 78"
$faultShortcut.Save()

Write-Host "Desktop shortcuts created:"
Write-Host "  Open Tianming ERP.lnk"
Write-Host "  天明ERP故障检查.lnk"

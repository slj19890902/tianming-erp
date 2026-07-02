$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Target = Join-Path $ProjectRoot "scripts\windows\start_erp.bat"
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "Open Tianming ERP.lnk"

if (-not (Test-Path -LiteralPath $Target)) {
    throw "Launcher not found: $Target"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $Target
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.WindowStyle = 1
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll, 0"
$shortcut.Save()

Write-Host "Desktop shortcut created: Open Tianming ERP.lnk"

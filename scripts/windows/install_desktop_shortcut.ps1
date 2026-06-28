$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\纸箱厂erp软件搭建"
$Target = Join-Path $ProjectRoot "scripts\windows\start_erp.bat"
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "打开天明ERP.lnk"

if (-not (Test-Path -LiteralPath $Target)) {
    throw "没有找到启动脚本：$Target"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $Target
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.WindowStyle = 1
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll, 0"
$shortcut.Save()

Write-Host "桌面快捷方式已创建，以后双击【打开天明ERP】即可进入系统。"

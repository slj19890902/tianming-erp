param(
    [string]$ImapHost,
    [ValidateRange(1, 65535)]
    [int]$ImapPort = 993,
    [string]$MailboxUsername,
    [switch]$UseStartTls
)

$ErrorActionPreference = "Stop"

if (-not $ImapHost) {
    $ImapHost = Read-Host "请输入邮箱 IMAP 服务器（例如 imap.example.com）"
}
if (-not $MailboxUsername) {
    $MailboxUsername = Read-Host "请输入专用收单邮箱地址"
}
if (-not $ImapHost.Trim()) { throw "IMAP 服务器不能为空" }
if ($MailboxUsername -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
    throw "请输入完整、有效的邮箱地址"
}

$securePassword = Read-Host "请输入邮箱授权密码（输入内容不会显示）" -AsSecureString
$passwordPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPtr)
    if (-not $plainPassword) { throw "邮箱授权密码不能为空" }

    $secretDir = Join-Path $env:LOCALAPPDATA "TianmingERP\secrets"
    $storageDir = Join-Path $env:LOCALAPPDATA "TianmingERP\email_order_intake"
    $passwordFile = Join-Path $secretDir "email_imap_password.txt"
    New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
    New-Item -ItemType Directory -Force -Path $storageDir | Out-Null

    # Lock the directory before the secret is written. Re-apply the ACL to the
    # file itself so a legacy inherited ACL cannot survive reconfiguration.
    & icacls.exe $secretDir /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "邮箱密码目录权限设置失败" }
    # PowerShell 5.1 does not support every newer .NET constructor shorthand.
    $utf8NoBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($passwordFile, $plainPassword, $utf8NoBom)
    & icacls.exe $passwordFile /inheritance:r /grant:r "${env:USERNAME}:F" "SYSTEM:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "邮箱密码文件权限设置失败" }

    $scope = [EnvironmentVariableTarget]::User
    [Environment]::SetEnvironmentVariable("ERP_EMAIL_INTAKE_ENABLED", "true", $scope)
    [Environment]::SetEnvironmentVariable("ERP_EMAIL_IMAP_HOST", $ImapHost.Trim(), $scope)
    [Environment]::SetEnvironmentVariable("ERP_EMAIL_IMAP_PORT", [string]$ImapPort, $scope)
    [Environment]::SetEnvironmentVariable("ERP_EMAIL_IMAP_USERNAME", $MailboxUsername.Trim().ToLowerInvariant(), $scope)
    [Environment]::SetEnvironmentVariable("ERP_EMAIL_IMAP_PASSWORD_FILE", $passwordFile, $scope)
    [Environment]::SetEnvironmentVariable("ERP_EMAIL_IMAP_USE_SSL", $(if ($UseStartTls) { "false" } else { "true" }), $scope)
    [Environment]::SetEnvironmentVariable("ERP_EMAIL_IMAP_STARTTLS", $(if ($UseStartTls) { "true" } else { "false" }), $scope)
    [Environment]::SetEnvironmentVariable("ERP_EMAIL_INTAKE_STORAGE_DIR", $storageDir, $scope)
} finally {
    if ($passwordPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPtr)
    }
    $plainPassword = $null
    $securePassword = $null
}

Write-Host "邮箱收单配置已保存到当前 Windows 用户环境。"
Write-Host "密码文件已限制为当前用户和 SYSTEM 可访问：$passwordFile"
Write-Host "下一步：重启 ERP，再运行 install_email_order_intake_task.ps1 安装每 30 分钟收件任务。"
Write-Host "ERP 只会生成待人工核对草稿，不会自动创建正式订单。"

#Requires -Version 5.1
param(
    [string]$DatabasePath,
    [string]$OutputRoot = "D:\tm-weekly-uat-exports",
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$database = if ($DatabasePath) {
    [System.IO.Path]::GetFullPath($DatabasePath)
} else {
    Join-Path $projectRoot "data\carton_erp.sqlite3"
}
$python = if ($PythonPath) {
    [System.IO.Path]::GetFullPath($PythonPath)
} else {
    Join-Path $projectRoot ".venv\Scripts\python.exe"
}
$helper = Join-Path $projectRoot "scripts\admin\weekly_home_uat.py"

try {
    if ($env:COMPUTERNAME -ine "PC-20250926DZYH") {
        throw "正式每周导出入口只允许在工厂主机 PC-20250926DZYH 运行。"
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "项目 Python 不存在：$python"
    }
    if (-not (Test-Path -LiteralPath $database -PathType Leaf)) {
        throw "正式数据库不存在：$database"
    }
    $branch = (& git -C $projectRoot branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne "factory-current-baseline") {
        throw "每周导出只允许从 factory-current-baseline 正式工作树执行。"
    }
    $resultText = & $python -X utf8 $helper export `
        --database $database `
        --output-root $OutputRoot `
        --project-root $projectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "每周 UAT 包导出失败：$resultText"
    }
    $result = $resultText | ConvertFrom-Json
    Write-Host "家庭 UAT 包已生成：$($result.package_dir)"
    Write-Host "代码 SHA：$($result.git_sha)"
    Write-Host "数据库 revision：$($result.revision)"
    Write-Host "数据库 SHA-256：$($result.database_sha256)"
    Write-Host "请只传输整个包目录，不要传输 .env、密钥、Cookie 或正式密码。"
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}

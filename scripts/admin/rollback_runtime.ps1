#Requires -Version 5.1
<#
.SYNOPSIS
    天明 ERP P0-5B 独立旧版本兼容演练（不执行正式回退）。

.DESCRIPTION
    根据最近一次受签名保护的发布证据，在全新的隔离目录准备上一版本代码和
    SQLite 副本，使用 loopback 非正式端口完成健康启动与数据不变核对。
    本脚本不会停止正式 ERP、不会切换正式 Git、不会覆盖正式数据库。
#>

[CmdletBinding()]
param(
    [string]$PointerPath,

    [ValidateRange(18000, 19999)]
    [int]$Port = 18120,

    [switch]$TechnicalDetails
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath(
    (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$helper = Join-Path $PSScriptRoot "rollback_runtime.py"
$database = Join-Path $projectRoot "data\carton_erp.sqlite3"
if ([string]::IsNullOrWhiteSpace($PointerPath)) {
    $PointerPath = Join-Path $projectRoot "data\release_state\latest_completed_release.json"
}
$PointerPath = [System.IO.Path]::GetFullPath($PointerPath)
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$runtimeBase = Join-Path (Split-Path $projectRoot -Parent) "tm-rollback-runtimes"
$outputRoot = Join-Path $runtimeBase ("compatibility_{0}" -f $timestamp)
$reportPath = Join-Path $outputRoot "rollback_compatibility.json"

function Format-ProgramId([object]$Value) {
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { return "无法读取" }
    if ($text.Length -le 7) { return $text }
    return $text.Substring(0, 7)
}
try {
    Write-Host ""
    Write-Host "========== 天明 ERP 上一版本隔离演练 =========="
    Write-Host "本工具不会停止正式 ERP，也不会修改正式代码或数据库。"
    Write-Host "正在准备上一版本和数据库副本，请勿重复打开..."
    Write-Host ""

    foreach ($requiredFile in @($python, $helper, $database, $PointerPath)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "隔离演练所需文件不存在：$requiredFile"
        }
    }

    $previousBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE
    try {
        $env:PYTHONDONTWRITEBYTECODE = "1"
        $output = @(
            & $python -I -B -X utf8 $helper prepare-compatibility `
                "--pointer" $PointerPath `
                "--database" $database `
                "--output-root" $outputRoot `
                "--report" $reportPath `
                "--bind-host" "127.0.0.1" `
                "--port" $Port 2>&1
        )
    } finally {
        if ($null -eq $previousBytecodeSetting) {
            Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONDONTWRITEBYTECODE = $previousBytecodeSetting
        }
    }
    if ($LASTEXITCODE -ne 0 -or $output.Count -lt 1) {
        $detail = if ($output.Count -gt 0) { $output[-1].ToString() } else { "没有返回结果" }
        try {
            $errorPayload = $detail | ConvertFrom-Json
            if (-not [string]::IsNullOrWhiteSpace([string]$errorPayload.error)) {
                $detail = [string]$errorPayload.error
            }
        } catch { }
        throw $detail
    }

    $result = $output[-1].ToString() | ConvertFrom-Json
    Write-Host "隔离演练完成。"
    Write-Host ""
    Write-Host ("演练结论：{0}" -f $result.mode_label)
    Write-Host (
        "上一程序：程序编号 {0}" -f
        (Format-ProgramId $result.previous_code_sha)
    )
    Write-Host "正式 ERP：未停止"
    Write-Host "正式数据库：未修改"
    Write-Host "正式 Git：未切换"
    Write-Host ""
    Write-Host "说明：当前只生成受保护的隔离兼容证据，尚未执行任何正式回退。"
    Write-Host "下一步：把本页结论交给工厂 Codex 复核；正式切换必须再次单独授权。"

    if ($TechnicalDetails) {
        Write-Host ""
        Write-Host ("兼容模式：{0}" -f $result.mode)
        Write-Host ("旧版本目录：{0}" -f $result.runtime_dir)
        Write-Host ("演练数据库：{0}" -f $result.rehearsal_database)
        Write-Host ("兼容报告：{0}" -f $result.report_path)
        Write-Host ("隔离健康地址：{0}" -f $result.health_url)
    }
    exit 0
} catch {
    Write-Host ""
    Write-Host ("隔离演练已安全阻断：{0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host "未停止正式 ERP，未切换正式 Git，未写入正式数据库。"
    exit 1
}

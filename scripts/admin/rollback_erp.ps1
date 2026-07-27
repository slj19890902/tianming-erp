#Requires -Version 5.1
<#
.SYNOPSIS
    天明 ERP P0-5A 离线故障与回退资格检查（只读）。

.DESCRIPTION
    本脚本只读取最近一次发布证据、Git、配置和 SQLite，不停服务、不切换代码、
    不恢复数据库，也不会生成回退授权口令。
#>

[CmdletBinding()]
param(
    [string]$PointerPath,
    [switch]$TechnicalDetails
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath(
    (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$helper = Join-Path $PSScriptRoot "rollback_erp.py"
$database = Join-Path $projectRoot "data\carton_erp.sqlite3"
if ([string]::IsNullOrWhiteSpace($PointerPath)) {
    $PointerPath = Join-Path $projectRoot "data\release_state\latest_completed_release.json"
}
$PointerPath = [System.IO.Path]::GetFullPath($PointerPath)

function Write-CheckLine([string]$Label, [string]$Value) {
    Write-Host ("{0}：{1}" -f $Label, $Value)
}

function Format-ProgramId([object]$Value) {
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { return "无法读取" }
    if ($text.Length -le 7) { return $text }
    return $text.Substring(0, 7)
}

try {
    Write-Host ""
    Write-Host "========== 天明 ERP 故障检查（只读） =========="
    Write-Host "本工具不会停止 ERP，也不会修改代码或数据库。"
    Write-Host ""

    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "项目 Python 环境不存在：$python"
    }
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        throw "只读检查程序不存在：$helper"
    }
    if (-not (Test-Path -LiteralPath $database -PathType Leaf)) {
        throw "ERP 数据库不存在：$database"
    }
    if (-not (Test-Path -LiteralPath $PointerPath -PathType Leaf)) {
        throw "尚无 P0-5A 格式的最近发布证据；请先按新版发布门禁完成一次发布。"
    }

    $serviceStatus = if (
        Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    ) {
        "正在运行"
    } else {
        "未运行"
    }
    Write-CheckLine "ERP 服务" $serviceStatus
    Write-Host "正在制作只读一致性副本并核对全部数据，可能需要几十秒，请勿重复打开..."

    $output = @(
        & $python -X utf8 $helper `
            "--pointer" $PointerPath `
            "--database" $database `
            "--project-root" $projectRoot 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $output.Count -lt 1) {
        $detail = if ($output.Count -gt 0) { $output[-1].ToString() } else { "没有返回结果" }
        $friendlyDetail = $detail
        try {
            $errorPayload = $detail | ConvertFrom-Json
            if (-not [string]::IsNullOrWhiteSpace([string]$errorPayload.error)) {
                $friendlyDetail = [string]$errorPayload.error
            }
        } catch {
            $friendlyDetail = $detail
        }
        throw "只读检查失败：$friendlyDetail"
    }
    $result = $output[-1].ToString() | ConvertFrom-Json

    Write-Host "核对完成。"
    Write-Host ""
    Write-CheckLine "检查结论" $result.mode_label
    Write-CheckLine "当前程序" ("程序编号 " + (Format-ProgramId $result.current_code_sha))
    Write-CheckLine "上一程序" ("程序编号 " + (Format-ProgramId $result.previous_code_sha))
    $databaseChangedLabel = if ($null -eq $result.database_changed_since_release) {
        "无法判断"
    } elseif ($result.database_changed_since_release) {
        "发布后已有新记录或内容变化"
    } else {
        "未检测到变化"
    }
    Write-CheckLine "数据状态" $databaseChangedLabel
    $backupStatus = if ($result.backup_verified) { "已验证" } else { "未验证" }
    Write-CheckLine "更新前备份" $backupStatus

    $changed = @($result.changed_tables)
    if ($changed.Count -gt 0) {
        Write-Host ""
        Write-Host ("检测到 {0} 张数据表内容变化；登录和操作日志变化也按最安全方式处理。" -f $changed.Count)
        if ($TechnicalDetails) {
            foreach ($item in $changed) {
                if ($item.completed_rows -eq $item.current_rows) {
                    Write-Host (
                        "  - {0}：内容已变化，行数仍为 {1}" -f
                        $item.table, $item.current_rows
                    )
                } else {
                    Write-Host (
                        "  - {0}：发布完成时 {1} 行，当前 {2} 行" -f
                        $item.table, $item.completed_rows, $item.current_rows
                    )
                }
            }
        }
    }
    if ($TechnicalDetails) {
        Write-Host ""
        Write-CheckLine "技术原因" $result.reason
        Write-CheckLine "当前完整 SHA" $result.current_code_sha
        Write-CheckLine "上一完整 SHA" $result.previous_code_sha
        Write-CheckLine "备份路径" $result.backup_path
    }

    Write-Host ""
    if ($result.mode -eq "full_rollback_allowed") {
        Write-Host "说明：当前只证明具备申请完整回退的条件；本工具不会执行回退。" -ForegroundColor Green
        Write-Host "下一步：把本页结论交给工厂 Codex，由它继续隔离演练和申请回退。"
    } else {
        Write-Host "说明：暂时不能安全回退，请保留当前数据，不要手工覆盖数据库。" -ForegroundColor Red
        Write-Host "下一步：联系工厂 Codex 做兼容检查或前向修复。"
    }
    exit 0
} catch {
    Write-Host ""
    Write-Host ("检查已安全阻断：{0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host "未停止 ERP，未修改 Git，未写入数据库。"
    exit 1
}

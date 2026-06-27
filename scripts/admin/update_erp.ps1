#Requires -Version 5.1
<#
.SYNOPSIS
    工厂 ERP 安全更新脚本

.DESCRIPTION
    执行顺序：
      1. 备份正式数据库（带时间戳，保留在 backups\ 下）
      2. git pull 拉取最新代码
      3. 停止正在运行的 ERP 服务（端口 8000）
      4. 运行 alembic upgrade head（有迁移则执行，无迁移无副作用）
      5. 重启 ERP 服务并等待健康检查
      6. 打印更新后版本号
      如任一步骤失败，脚本停止并输出错误，不会强制继续。

.NOTES
    在工厂 PC 上以管理员或普通用户权限均可运行（不需要 UAC）。
    运行前确保已连接网络（用于 git pull）。
#>

$ErrorActionPreference = "Stop"

# ── 路径计算 ────────────────────────────────────────────────────────────────────
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$backupDir   = Join-Path $projectRoot "backups"
$logDir      = Join-Path $projectRoot "logs"
$updateLog   = Join-Path $logDir "erp_update.log"

New-Item -ItemType Directory -Path $logDir   -Force | Out-Null
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

function Write-Log([string]$Message, [string]$Level = "INFO") {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Host $line
    Add-Content -LiteralPath $updateLog -Encoding UTF8 -Value $line
}

function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        (Join-Path $projectRoot ".venv\Scripts\python.exe"),
        (Join-Path $projectRoot "venv\Scripts\python.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Stop-ErpService {
    $conns = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Log "ERP 服务未在运行，无需停止。"
        return
    }
    $pids = $conns | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique
    foreach ($pid in $pids) {
        try {
            $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Log "停止进程 PID=$pid ($($proc.ProcessName))..."
                Stop-Process -Id $pid -Force
            }
        } catch {
            Write-Log "停止进程 $pid 失败：$($_.Exception.Message)" "WARN"
        }
    }
    # 等待端口释放（最多 10 秒）
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Seconds 1
        $still = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
        if (-not $still) { break }
    }
    $still = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($still) {
        throw "端口 8000 在 10 秒内未释放，请手动检查。"
    }
    Write-Log "ERP 服务已停止。"
}

# ── 主流程 ───────────────────────────────────────────────────────────────────────
Write-Log "========== ERP 更新开始 =========="

# 1. 备份数据库
$dbPath = $env:ERP_DATABASE_PATH
if (-not $dbPath) {
    # 尝试从 .env 读取
    $envFile = Join-Path $projectRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        $envLine = Get-Content $envFile | Where-Object { $_ -match "^ERP_DATABASE_PATH\s*=" }
        if ($envLine) {
            $dbPath = ($envLine -split "=", 2)[1].Trim().Trim('"').Trim("'")
        }
    }
}
if (-not $dbPath) {
    $dbPath = Join-Path $projectRoot "erp.db"
}

if (-not (Test-Path -LiteralPath $dbPath)) {
    throw "数据库文件不存在：$dbPath"
}

$ts         = Get-Date -Format "yyyyMMdd_HHmmss"
$backupPath = Join-Path $backupDir "erp_pre_update_$ts.db"
Write-Log "备份数据库：$dbPath → $backupPath"
Copy-Item -LiteralPath $dbPath -Destination $backupPath -Force
$backupSize = (Get-Item -LiteralPath $backupPath).Length
Write-Log "备份完成，大小 $([Math]::Round($backupSize/1MB, 1)) MB"

# 2. git pull
Write-Log "执行 git pull..."
Push-Location $projectRoot
try {
    $pullOutput = git pull 2>&1
    $pullOutput | ForEach-Object { Write-Log "  git: $_" }
    if ($LASTEXITCODE -ne 0) {
        throw "git pull 失败（退出码 $LASTEXITCODE）。请检查网络或手动解决冲突。"
    }
    Write-Log "git pull 完成。"
} finally {
    Pop-Location
}

# 3. 停止现有服务
Write-Log "停止 ERP 服务..."
Stop-ErpService

# 4. 数据库迁移
$python = Find-Python
if (-not $python) { throw "找不到 Python 可执行文件。" }
Write-Log "使用 Python：$python"

Write-Log "运行数据库迁移（alembic upgrade head）..."
Push-Location $projectRoot
try {
    $alembicOutput = & $python -m alembic upgrade head 2>&1
    $alembicOutput | ForEach-Object { Write-Log "  alembic: $_" }
    if ($LASTEXITCODE -ne 0) {
        throw "alembic upgrade head 失败（退出码 $LASTEXITCODE）。数据库备份在：$backupPath"
    }
    Write-Log "数据库迁移完成（或无需迁移）。"
} finally {
    Pop-Location
}

# 5. 启动服务
Write-Log "启动 ERP 服务..."
$startScript = Join-Path $PSScriptRoot "start_erp_background.ps1"
if (-not (Test-Path -LiteralPath $startScript)) {
    throw "找不到启动脚本：$startScript"
}
& $startScript
if ($LASTEXITCODE -ne 0) {
    throw "ERP 启动失败，请查看 logs\erp_server_error.log。数据库备份在：$backupPath"
}

# 6. 打印版本
Write-Log "查询当前版本..."
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/version" -UseBasicParsing -TimeoutSec 5
    $info = $resp.Content | ConvertFrom-Json
    Write-Log "========== 更新完成，当前版本：$($info.version) — $($info.version_name) =========="
} catch {
    Write-Log "ERP 已启动，但无法读取版本号：$($_.Exception.Message)" "WARN"
    Write-Log "========== 更新流程完成 =========="
}

Write-Log "数据库备份保留在：$backupPath"

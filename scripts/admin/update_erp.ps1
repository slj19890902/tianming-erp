#Requires -Version 5.1
<#
.SYNOPSIS
    工厂 ERP 安全更新脚本

.DESCRIPTION
    执行顺序：
      1. 校验正式分支、工作区、Python 和正式数据库路径
      2. 停止正在运行的 ERP 服务（端口 8000）
      3. 使用 SQLite Backup API 创建完整备份并校验
      4. 仅快进拉取 factory-current-baseline
      5. 运行 alembic upgrade head（有迁移则执行，无迁移无副作用）
      6. 重启 ERP 服务并等待健康检查
      7. 打印更新后版本号
      如任一步骤失败，脚本停止并输出错误，不会强制继续。

.NOTES
    在工厂 PC 上以管理员或普通用户权限均可运行（不需要 UAC）。
    运行前确保已连接网络（用于 git pull）。
#>

$ErrorActionPreference = "Stop"

# ── 路径计算 ────────────────────────────────────────────────────────────────────
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$backupDir   = Join-Path $projectRoot "data\backups"
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

# 1. 更新前检查
$python = Find-Python
if (-not $python) { throw "找不到 Python 可执行文件。" }
Write-Log "使用 Python：$python"

Push-Location $projectRoot
try {
    $branch = (git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne "factory-current-baseline") {
        throw "当前必须位于 factory-current-baseline 分支，实际为：$branch"
    }
    $dirty = git status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取 Git 工作区状态。"
    }
    if ($dirty) {
        throw "工作区存在未提交修改，禁止自动更新。请先联系技术人员。"
    }
    Write-Log "Git 分支和工作区检查通过。"
    Write-Log "更新前获取远端 factory-current-baseline..."
    $fetchOutput = git fetch origin factory-current-baseline 2>&1
    $fetchOutput | ForEach-Object { Write-Log "  git: $_" }
    if ($LASTEXITCODE -ne 0) {
        throw "git fetch 失败。ERP 服务尚未停止，请检查网络后重试。"
    }
    Write-Log "远端代码获取完成。"
} finally {
    Pop-Location
}

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
    $dbPath = Join-Path $projectRoot "data\carton_erp.sqlite3"
}
if (-not [System.IO.Path]::IsPathRooted($dbPath)) {
    $dbPath = Join-Path $projectRoot $dbPath
}
$dbPath = [System.IO.Path]::GetFullPath($dbPath)

if (-not (Test-Path -LiteralPath $dbPath)) {
    throw "数据库文件不存在：$dbPath"
}

# 2. 先停止服务，避免更新期间继续写库或加载到一半的新旧代码
Write-Log "停止 ERP 服务..."
Stop-ErpService

# 3. 使用 SQLite Backup API 创建一致性备份
$backupHelper = Join-Path $PSScriptRoot "pre_update_backup.py"
if (-not (Test-Path -LiteralPath $backupHelper)) {
    throw "找不到更新前备份脚本：$backupHelper"
}
Write-Log "使用 SQLite Backup API 备份数据库：$dbPath"
$backupOutput = & $python $backupHelper --database $dbPath --backup-dir $backupDir
if ($LASTEXITCODE -ne 0) {
    throw "数据库备份失败，禁止继续更新。"
}
$backupInfo = $backupOutput | ConvertFrom-Json
$backupPath = $backupInfo.path
if ($backupInfo.integrity_check -ne "ok") {
    throw "备份完整性检查失败：$($backupInfo.integrity_check)"
}
Write-Log "备份完成：$backupPath"
Write-Log "备份大小：$([Math]::Round([double]$backupInfo.size/1MB, 1)) MB"
Write-Log "备份 SHA-256：$($backupInfo.sha256)"
Write-Log "备份 integrity_check：$($backupInfo.integrity_check)"
if ($backupInfo.cleanup_error) {
    Write-Log "常规备份保留策略执行异常：$($backupInfo.cleanup_error)" "WARN"
}

# 4. 使用更新前已获取的远端提交，只允许正式基线分支快进更新
Write-Log "执行 git merge --ff-only origin/factory-current-baseline..."
Push-Location $projectRoot
try {
    $pullOutput = git merge --ff-only origin/factory-current-baseline 2>&1
    $pullOutput | ForEach-Object { Write-Log "  git: $_" }
    if ($LASTEXITCODE -ne 0) {
        throw "git merge --ff-only 失败（退出码 $LASTEXITCODE）。禁止自动合并，请联系技术人员。"
    }
    Write-Log "代码快进更新完成。"
} finally {
    Pop-Location
}

# 5. 数据库迁移
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

# 6. 启动服务
Write-Log "启动 ERP 服务..."
$startScript = Join-Path $PSScriptRoot "start_erp_background.ps1"
if (-not (Test-Path -LiteralPath $startScript)) {
    throw "找不到启动脚本：$startScript"
}
& $startScript
if ($LASTEXITCODE -ne 0) {
    throw "ERP 启动失败，请查看 logs\erp_server_error.log。数据库备份在：$backupPath"
}

# 7. 从刚更新的本地代码读取版本。精确版本 API 需要登录，避免对外暴露指纹。
Write-Log "读取当前版本..."
try {
    $versionOutput = @(
        & $python -X utf8 -c "from app.version import APP_VERSION, APP_VERSION_NAME; print(APP_VERSION); print(APP_VERSION_NAME)" 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $versionOutput.Count -lt 2) {
        throw "无法从本地版本文件读取版本号"
    }
    $currentVersion = $versionOutput[-2].ToString().Trim()
    $currentVersionName = $versionOutput[-1].ToString().Trim()
    Write-Log "========== 更新完成，当前版本：$currentVersion — $currentVersionName =========="
} catch {
    Write-Log "ERP 已启动，但无法读取版本号：$($_.Exception.Message)" "WARN"
    Write-Log "========== 更新流程完成 =========="
}

Write-Log "数据库备份保留在：$backupPath"

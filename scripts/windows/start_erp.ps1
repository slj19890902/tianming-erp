$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\纸箱厂erp软件搭建"
$HealthUrl = "http://127.0.0.1:8000/api/health"
$BrowserUrl = "http://127.0.0.1:8000/"
$LogDir = Join-Path $ProjectRoot "logs"
$StartupLog = Join-Path $LogDir "erp_startup.log"
$ServerLog = Join-Path $LogDir "erp_server.log"
$ServerErrorLog = Join-Path $LogDir "erp_server_error.log"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-Log([string]$Message) {
    Add-Content -LiteralPath $StartupLog -Encoding UTF8 -Value (
        "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    )
}

function Test-ErpRunning {
    try {
        $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

try {
    Write-Log "启动流程开始。"

    if (Test-ErpRunning) {
        Write-Log "ERP 已经在运行，直接打开网页。"
        Start-Process $BrowserUrl | Out-Null
        Write-Host "ERP 已经在运行，网页已打开。"
        exit 0
    }

    $listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Log "8000 端口被其他程序占用。"
        throw "8000 端口被其他程序占用，ERP 无法启动，请联系管理员。"
    }

    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Log "没有找到 .venv\Scripts\python.exe。"
        throw "没有找到 ERP 的 Python 环境，请联系管理员检查 .venv 是否存在。"
    }

    Write-Log "开始执行 alembic upgrade head。"
    & $Python -X utf8 -m alembic upgrade head 2>&1 | Tee-Object -FilePath $StartupLog -Append | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "数据库升级失败。请把这个窗口截图发给管理员。"
    }

    Write-Log "开始执行 alembic current。"
    & $Python -X utf8 -m alembic current 2>&1 | Tee-Object -FilePath $StartupLog -Append | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "数据库版本检查失败。请把这个窗口截图发给管理员。"
    }

    Write-Log "开始启动 uvicorn。"
    Start-Process `
        -FilePath $Python `
        -ArgumentList @("-X", "utf8", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--workers", "1") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $ServerLog `
        -RedirectStandardError $ServerErrorLog | Out-Null

    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-ErpRunning) {
            $ready = $true
            break
        }
    }

    if (-not $ready) {
        throw "ERP 启动超时，网页没有在规定时间内打开。"
    }

    Write-Log "ERP 启动成功。"
    Start-Process $BrowserUrl | Out-Null
    Write-Host "ERP 已启动，网页已打开。"
    exit 0
} catch {
    Write-Log ("启动失败: {0}" -f $_.Exception.Message)
    Write-Host $_.Exception.Message
    Write-Host "ERP 启动失败，请把这个窗口截图发给管理员。"
    exit 1
}

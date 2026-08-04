#Requires -Version 5.1
<#
.SYNOPSIS
    使用独立 SQLite 副本和独立端口启动家庭 UAT。

.DESCRIPTION
    本入口强制 test + loopback，不读取或写入工厂正式数据库，不执行 Alembic 迁移。
    UAT 副本 revision 与当前代码 head 不一致时直接拒绝启动。
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$DatabasePath,

    [ValidateRange(18000, 19999)]
    [int]$Port = 18080,

    [string]$UatRoot,

    [string]$PythonPath,

    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = if ($PythonPath) {
    [System.IO.Path]::GetFullPath($PythonPath)
} else {
    Join-Path $projectRoot ".venv\Scripts\python.exe"
}
$releaseGate = Join-Path $projectRoot "scripts\admin\release_erp.py"
$weeklyUatGate = Join-Path $projectRoot "scripts\admin\weekly_home_uat.py"
$checkoutDatabase = [System.IO.Path]::GetFullPath(
    (Join-Path $projectRoot "data\carton_erp.sqlite3")
)
$factoryDatabase = [System.IO.Path]::GetFullPath(
    "D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3"
)
$resolvedDatabase = [System.IO.Path]::GetFullPath($DatabasePath)
$weeklyMode = -not [string]::IsNullOrWhiteSpace($UatRoot)
$resolvedUatRoot = $null
$resolvedRunsRoot = $null
if ($weeklyMode) {
    $resolvedUatRoot = [System.IO.Path]::GetFullPath($UatRoot)
    $resolvedRunsRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $resolvedUatRoot "runs")
    ).TrimEnd("\") + "\"
}
$logDir = Join-Path $projectRoot "logs"
$serverLog = Join-Path $logDir ("erp_uat_{0}.log" -f $Port)
$serverErrorLog = Join-Path $logDir ("erp_uat_{0}_error.log" -f $Port)
$healthUrl = "http://127.0.0.1:$Port/api/health"
$browserUrl = "http://127.0.0.1:$Port/"
$process = $null
$pidFile = if ($weeklyMode) {
    Join-Path ([System.IO.Path]::GetDirectoryName($resolvedDatabase)) ("erp_uat_{0}.pid" -f $Port)
} else { $null }

try {
    if ($weeklyMode -and $env:COMPUTERNAME -ieq "PC-20250926DZYH") {
        throw "家庭 UAT 启动器禁止在工厂正式主机运行。"
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "UAT 必须使用当前工作树 .venv，禁止回退到全局 Python。"
    }
    if (-not (Test-Path -LiteralPath $releaseGate -PathType Leaf)) {
        throw "启动 revision 门禁不存在：$releaseGate"
    }
    if ($weeklyMode -and -not (Test-Path -LiteralPath $weeklyUatGate -PathType Leaf)) {
        throw "每周家庭 UAT 隔离门禁不存在：$weeklyUatGate"
    }
    if (-not (Test-Path -LiteralPath $resolvedDatabase -PathType Leaf)) {
        throw "UAT 数据库副本不存在：$resolvedDatabase"
    }
    $protectedDatabases = @($checkoutDatabase, $factoryDatabase)
    if ($protectedDatabases | Where-Object {
        [System.StringComparer]::OrdinalIgnoreCase.Equals($resolvedDatabase, $_)
    }) {
        throw "UAT 禁止连接工厂正式数据库：$resolvedDatabase"
    }
    if ($weeklyMode -and -not $resolvedDatabase.StartsWith(
        $resolvedRunsRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "UAT 数据库必须位于 $resolvedRunsRoot 下。"
    }
    if ($Port -eq 8000) { throw "UAT 禁止使用正式端口 8000。" }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw "UAT 端口 $Port 已被占用。"
    }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $previous = @{
        ERP_ENVIRONMENT = $env:ERP_ENVIRONMENT
        ERP_DATABASE_PATH = $env:ERP_DATABASE_PATH
        ERP_BIND_HOST = $env:ERP_BIND_HOST
        ERP_PORT = $env:ERP_PORT
        ERP_WORKERS = $env:ERP_WORKERS
        ERP_HEALTH_URL = $env:ERP_HEALTH_URL
        ERP_BROWSER_URL = $env:ERP_BROWSER_URL
        ERP_SESSION_COOKIE_NAME = $env:ERP_SESSION_COOKIE_NAME
    }
    try {
        $env:ERP_ENVIRONMENT = "test"
        $env:ERP_DATABASE_PATH = $resolvedDatabase
        $env:ERP_BIND_HOST = "127.0.0.1"
        $env:ERP_PORT = $Port.ToString()
        $env:ERP_WORKERS = "1"
        $env:ERP_HEALTH_URL = $healthUrl
        $env:ERP_BROWSER_URL = $browserUrl
        if ($weeklyMode) {
            $env:ERP_SESSION_COOKIE_NAME = "erp_uat_$Port"
        }

        if ($weeklyMode) {
            $weeklyCheck = @(
                & $python -X utf8 $weeklyUatGate check-start `
                    --database $resolvedDatabase `
                    --uat-root $resolvedUatRoot `
                    --project-root $projectRoot `
                    --port $Port 2>&1
            )
            if ($LASTEXITCODE -ne 0) {
                throw "家庭 UAT 隔离门禁失败：$($weeklyCheck -join ' ')"
            }
        }

        $checkOutput = @(
            & $python -X utf8 $releaseGate check-startup --database $resolvedDatabase 2>&1
        )
        if ($LASTEXITCODE -ne 0) {
            throw "UAT 数据库 revision/完整性门禁失败：$($checkOutput -join ' ')"
        }

        $arguments = @(
            "-X", "utf8",
            "-m", "uvicorn",
            "app.main:app",
            "--app-dir", $projectRoot,
            "--host", "127.0.0.1",
            "--port", $Port.ToString(),
            "--workers", "1"
        )
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList $arguments `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $serverLog `
            -RedirectStandardError $serverErrorLog `
            -PassThru
        if ($weeklyMode) {
            Set-Content -LiteralPath $pidFile -Encoding ASCII -Value $process.Id
        }
    } finally {
        foreach ($name in $previous.Keys) {
            $value = $previous[$name]
            if ($null -eq $value) {
                Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
            } else {
                Set-Item -Path ("Env:" + $name) -Value $value
            }
        }
    }

    $ready = $false
    for ($index = 0; $index -lt 20; $index++) {
        Start-Sleep -Seconds 1
        if ($process.HasExited) { break }
        try {
            $response = Invoke-WebRequest `
                -Uri $healthUrl `
                -UseBasicParsing `
                -MaximumRedirection 0 `
                -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch { }
    }
    if (-not $ready) { throw "UAT 未在 20 秒内就绪。" }
    if (-not $NoBrowser) { Start-Process $browserUrl | Out-Null }
    Write-Host "UAT 已启动：$browserUrl"
    Write-Host "UAT 数据库副本：$resolvedDatabase"
    if ($weeklyMode) { Write-Host "UAT PID 文件：$pidFile" }
    Write-Host "本入口未执行数据库迁移。"
    exit 0
} catch {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    if ($pidFile) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
    Write-Error $_.Exception.Message
    exit 1
}

param(
    [ValidateRange(5, 300)]
    [int]$ProbeIntervalSeconds = 20,

    [switch]$RunOnce
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Launcher = Join-Path $ProjectRoot "scripts\windows\start_erp.ps1"
$MaintenanceLock = Join-Path $ProjectRoot "data\runtime\erp_maintenance.lock"
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "erp_health_guard.log"
$MutexName = "Global\TianmingErpHealthGuard"

function Write-GuardLog {
    param([string]$Message)

    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value (
        "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    )
}

function Test-LocalErpHealth {
    try {
        $response = Invoke-WebRequest `
            -Uri "http://127.0.0.1:8000/api/health" `
            -UseBasicParsing `
            -MaximumRedirection 0 `
            -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Invoke-HealthGuardIteration {
    if (Test-Path -LiteralPath $MaintenanceLock -PathType Leaf) {
        Write-GuardLog "Maintenance lock is active; recovery is paused."
        return
    }

    if (Test-LocalErpHealth) {
        return
    }

    if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
        throw "Hardened ERP launcher not found: $Launcher"
    }

    Write-GuardLog "ERP health probe failed; invoking the hardened launcher."
    & $Launcher -NoBrowser
    if ($LASTEXITCODE -ne 0) {
        throw "Hardened ERP launcher failed with exit code $LASTEXITCODE."
    }
    if (-not (Test-LocalErpHealth)) {
        throw "ERP launcher returned success but local health is still unavailable."
    }
    Write-GuardLog "ERP recovery succeeded."
}

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$hasMutex = $false
try {
    try {
        $hasMutex = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
    }
    if (-not $hasMutex) {
        Write-GuardLog "Another ERP health guard instance is already active."
        exit 0
    }

    do {
        try {
            Invoke-HealthGuardIteration
        } catch {
            Write-GuardLog ("Recovery attempt failed: {0}" -f $_.Exception.Message)
        }

        if (-not $RunOnce) {
            Start-Sleep -Seconds $ProbeIntervalSeconds
        }
    } while (-not $RunOnce)
} finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}

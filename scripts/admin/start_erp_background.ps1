$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$logDir = Join-Path $projectRoot "logs"
$launcherLog = Join-Path $logDir "erp_autostart.log"
$serverLog = Join-Path $logDir "erp_server.log"
$serverErrorLog = Join-Path $logDir "erp_server_error.log"

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Write-LauncherLog([string]$Message) {
    Add-Content -LiteralPath $launcherLog -Encoding UTF8 -Value (
        "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    )
}

try {
    $existing = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($existing) {
        Write-LauncherLog "ERP is already listening on port 8000."
        exit 0
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        (Join-Path $projectRoot ".venv\Scripts\python.exe"),
        (Join-Path $projectRoot "venv\Scripts\python.exe")
    )
    $python = $null
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $probe = Start-Process `
                -FilePath $candidate `
                -ArgumentList "--version" `
                -WindowStyle Hidden `
                -Wait `
                -PassThru
            if ($probe.ExitCode -eq 0) {
                $python = $candidate
                break
            }
        } catch {
            continue
        }
    }
    if (-not $python) {
        $command = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($command) { $python = $command.Source }
    }
    if (-not $python) {
        throw "No usable Python runtime was found."
    }

    Write-LauncherLog "Starting ERP with $python"
    Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $serverLog `
        -RedirectStandardError $serverErrorLog

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            # Service may still be starting.
        }
    }
    if (-not $ready) {
        throw "ERP did not become ready on port 8000."
    }
    Write-LauncherLog "ERP startup completed."
} catch {
    Write-LauncherLog "Startup failed: $($_.Exception.Message)"
    exit 1
}

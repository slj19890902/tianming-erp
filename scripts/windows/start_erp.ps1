param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $ProjectRoot
if (Test-Path -LiteralPath (Join-Path $ProjectRoot 'data/runtime/erp_managed_installation.json')) {
    throw 'This ERP has moved to the desktop assistant. Start ERP from the assistant; the old data directory must remain stopped.'
}
$ExternalHealthUrl = $null
$LocalHealthUrl = $null
$BrowserUrl = $null
$LanHttpOrigin = $null
$ErpPort = $null
$BindHost = $null
$RuntimeEnvironment = $null
$ProductionTransport = $null
$DatabasePath = $null
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "erp_startup.log"
$ServerLog = Join-Path $LogDir "erp_server.log"
$ServerErrorLog = Join-Path $LogDir "erp_server_error.log"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$process = $null

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-Log {
    param([string]$Message)
    Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value (
        "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    )
}

function Test-LocalErpRunning {
    try {
        $response = Invoke-WebRequest -Uri $LocalHealthUrl -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-ExternalErpHealth {
    try {
        $response = Invoke-WebRequest -Uri $ExternalHealthUrl -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Confirm-ProductionExternalHealth {
    if ($RuntimeEnvironment -ne "production") { return }
    # This check is deliberately advisory and runs only after loopback readiness.
    # A reverse-proxy/certificate failure must not kill a healthy local ERP process.
    if (Test-ExternalErpHealth) {
        Write-Log ("Configured production {0} health check succeeded." -f $ProductionTransport)
        return
    }
    $guidance = if ($ProductionTransport -eq "https_proxy") {
        "Check the reverse proxy and certificate"
    } else {
        "Check the private-network address and Windows Firewall LAN scope"
    }
    $message = (
        "ERP is ready on loopback, but configured production health failed: {0}. " +
        "{1}; the local ERP process remains running."
    ) -f $ExternalHealthUrl, $guidance
    Write-Log $message
    Write-Warning $message
}

function Open-Browser {
    if ($NoBrowser) { return }
    if ($LanHttpOrigin) {
        Start-Process ($LanHttpOrigin + "/") | Out-Null
        return
    }
    Start-Process $BrowserUrl | Out-Null
}

function Invoke-PythonCommand {
    param(
        [string]$Label,
        [string[]]$Arguments
    )

    $stdout = Join-Path $LogDir ("{0}.out.log" -f $Label)
    $stderr = Join-Path $LogDir ("{0}.err.log" -f $Label)

    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

    $fullArguments = @("-X", "utf8") + $Arguments

    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $fullArguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru `
        -Wait

    if (Test-Path -LiteralPath $stdout) {
        Get-Content -LiteralPath $stdout -ErrorAction SilentlyContinue | ForEach-Object {
            Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value $_
        }
    }
    if (Test-Path -LiteralPath $stderr) {
        Get-Content -LiteralPath $stderr -ErrorAction SilentlyContinue | ForEach-Object {
            Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value $_
        }
    }

    if ($process.ExitCode -ne 0) {
        throw ("{0} failed with exit code {1}." -f $Label, $process.ExitCode)
    }
}

try {
    Write-Log "Startup begin."

    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Log "Python runtime not found."
        throw "Python runtime not found. Please check the .venv folder."
    }

    $runtimeConfig = @(
        & $Python -X utf8 -c "import base64; from app.core.config import load_settings; s=load_settings(); print(s.lan_http_origin or '-'); print(s.bind_host); print(s.port); print(s.workers); print(s.environment); print(s.production_transport); print(s.health_url); print(s.browser_url); print(base64.b64encode(str(s.database_path).encode('utf-8')).decode('ascii'))" 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $runtimeConfig.Count -lt 9) {
        $runtimeConfig | ForEach-Object { Write-Log $_ }
        throw "ERP runtime configuration is invalid."
    }
    $LanHttpOrigin = $runtimeConfig[-9].ToString().Trim()
    if ($LanHttpOrigin -eq "-") { $LanHttpOrigin = $null }
    $BindHost = $runtimeConfig[-8].ToString().Trim()
    $ErpPort = [int]$runtimeConfig[-7].ToString().Trim()
    $Workers = [int]$runtimeConfig[-6].ToString().Trim()
    $RuntimeEnvironment = $runtimeConfig[-5].ToString().Trim()
    $ProductionTransport = $runtimeConfig[-4].ToString().Trim()
    $ExternalHealthUrl = $runtimeConfig[-3].ToString().Trim()
    $BrowserUrl = $runtimeConfig[-2].ToString().Trim()
    $databasePathText = [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String($runtimeConfig[-1].ToString().Trim())
    )
    $DatabasePath = [System.IO.Path]::GetFullPath($databasePathText)
    if ($RuntimeEnvironment -ne "production") {
        throw (
            "Factory launcher requires ERP_ENVIRONMENT=production. " +
            "Use scripts\windows\start_erp_uat.ps1 for an isolated UAT copy."
        )
    }
    if ($Workers -ne 1) {
        throw "ERP must run with exactly one worker for atomic login throttling."
    }
    $FormalDatabasePath = [System.IO.Path]::GetFullPath(
        (Join-Path $ProjectRoot "data\carton_erp.sqlite3")
    )
    if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals(
        $DatabasePath,
        $FormalDatabasePath
    )) {
        throw "Factory launcher only accepts the formal database path: $FormalDatabasePath"
    }
    $LocalHealthUrl = "http://127.0.0.1:$ErpPort/api/health"
    if ($RuntimeEnvironment -eq "production") {
        if ($ProductionTransport -eq "https_proxy") {
            if ($ExternalHealthUrl -notlike "https://*") {
                throw "https_proxy requires ERP_HEALTH_URL to use HTTPS."
            }
            if ($BrowserUrl -notlike "https://*") {
                throw "https_proxy requires ERP_BROWSER_URL to use HTTPS."
            }
        } elseif ($ProductionTransport -eq "lan_http") {
            if ($ExternalHealthUrl -notlike "http://*" -or $BrowserUrl -notlike "http://*") {
                throw "lan_http requires ERP_HEALTH_URL and ERP_BROWSER_URL to use HTTP."
            }
        } else {
            throw "Unknown ERP_PRODUCTION_TRANSPORT: $ProductionTransport"
        }
    }
    Write-Log ("Runtime transport/bind: {0} {1}:{2}" -f $ProductionTransport, $BindHost, $ErpPort)

    $releaseGate = Join-Path $ProjectRoot "scripts\admin\release_erp.py"
    if (-not (Test-Path -LiteralPath $releaseGate -PathType Leaf)) {
        throw "Release gate helper not found: $releaseGate"
    }
    Write-Log "Checking database integrity and Alembic revision without migration."
    Invoke-PythonCommand `
        -Label "startup_revision_check" `
        -Arguments @($releaseGate, "check-startup", "--database", $DatabasePath)

    if (Test-LocalErpRunning) {
        Write-Log "ERP already running."
        Confirm-ProductionExternalHealth
        Open-Browser
        Write-Host "ERP already running. Browser opened."
        exit 0
    }

    $listening = Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Log ("Port {0} is already in use." -f $ErpPort)
        throw ("Port {0} is already in use. ERP cannot start." -f $ErpPort)
    }

    Write-Log "Starting uvicorn."
    $arguments = @(
        "-X", "utf8",
        "-m", "uvicorn",
        "app.main:app",
        "--app-dir", $ProjectRoot,
        "--host", $BindHost,
        "--port", $ErpPort.ToString(),
        "--workers", "1"
    )
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $ServerLog `
        -RedirectStandardError $ServerErrorLog `
        -PassThru

    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        if (Test-LocalErpRunning) {
            $ready = $true
            break
        }
        if ($process.HasExited) {
            break
        }
    }

    if (-not $ready) {
        throw "ERP did not become ready within 20 seconds."
    }

    Confirm-ProductionExternalHealth
    Write-Log "ERP startup succeeded."
    Open-Browser
    Write-Host "ERP started. Browser opened."
    exit 0
} catch {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        try { $process.WaitForExit(5000) | Out-Null } catch { }
        Write-Log ("Stopped failed ERP process PID={0}." -f $process.Id)
    }
    Write-Log ("Startup failed: {0}" -f $_.Exception.Message)
    Write-Host $_.Exception.Message
    Write-Host "ERP startup failed. Please check logs\\erp_startup.log"
    exit 1
}

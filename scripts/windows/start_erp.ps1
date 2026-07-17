$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$HealthUrl = $null
$BrowserUrl = $null
$ErpPort = $null
$BindHost = $null
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "erp_startup.log"
$ServerLog = Join-Path $LogDir "erp_server.log"
$ServerErrorLog = Join-Path $LogDir "erp_server_error.log"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-Log {
    param([string]$Message)
    Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value (
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

function Open-Browser {
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
        & $Python -X utf8 -c "from app.core.config import load_settings; s=load_settings(); print(s.bind_host); print(s.port); print(s.environment)" 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $runtimeConfig.Count -lt 3) {
        $runtimeConfig | ForEach-Object { Write-Log $_ }
        throw "ERP runtime configuration is invalid."
    }
    $BindHost = $runtimeConfig[-3].ToString().Trim()
    $ErpPort = [int]$runtimeConfig[-2].ToString().Trim()
    $RuntimeEnvironment = $runtimeConfig[-1].ToString().Trim()
    if ($RuntimeEnvironment -eq "production") {
        if (-not $env:ERP_HEALTH_URL -or -not $env:ERP_HEALTH_URL.StartsWith("https://")) {
            throw "Production requires ERP_HEALTH_URL to use the HTTPS reverse-proxy health endpoint."
        }
        if (-not $env:ERP_BROWSER_URL -or -not $env:ERP_BROWSER_URL.StartsWith("https://")) {
            throw "Production requires ERP_BROWSER_URL to use the HTTPS reverse-proxy ERP endpoint."
        }
    }
    $HealthUrl = if ($env:ERP_HEALTH_URL) {
        $env:ERP_HEALTH_URL
    } else {
        "http://127.0.0.1:$ErpPort/api/health"
    }
    $BrowserUrl = if ($env:ERP_BROWSER_URL) {
        $env:ERP_BROWSER_URL
    } else {
        "http://127.0.0.1:$ErpPort/"
    }
    Write-Log ("Runtime bind: {0}:{1}" -f $BindHost, $ErpPort)

    if (Test-ErpRunning) {
        Write-Log "ERP already running."
        Open-Browser
        Write-Host "ERP already running. Browser opened."
        exit 0
    }

    $listening = Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Log ("Port {0} is already in use." -f $ErpPort)
        throw ("Port {0} is already in use. ERP cannot start." -f $ErpPort)
    }

    Write-Log "Running alembic upgrade head."
    Invoke-PythonCommand -Label "alembic_upgrade" -Arguments @("-m", "alembic", "upgrade", "head")

    Write-Log "Running alembic current."
    Invoke-PythonCommand -Label "alembic_current" -Arguments @("-m", "alembic", "current")

    Write-Log "Starting uvicorn."
    $arguments = @(
        "-X", "utf8",
        "-m", "uvicorn",
        "app.main:app",
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
        if (Test-ErpRunning) {
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

    Write-Log "ERP startup succeeded."
    Open-Browser
    Write-Host "ERP started. Browser opened."
    exit 0
} catch {
    Write-Log ("Startup failed: {0}" -f $_.Exception.Message)
    Write-Host $_.Exception.Message
    Write-Host "ERP startup failed. Please check logs\\erp_startup.log"
    exit 1
}

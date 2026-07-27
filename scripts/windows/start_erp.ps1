#Requires -Version 5.1
param(
    [switch]$NoBrowser,
    [string]$ValidationTicket
)

$ErrorActionPreference = "Stop"

# ControlRoot is the formally installed controller checkout.  ApplicationRoot
# is resolved from the signed active-runtime pointer and may be an immutable
# archived runtime.  Logs, secrets and private files always remain owned by
# ControlRoot.
$ControlRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ControlPython = Join-Path $ControlRoot ".venv\Scripts\python.exe"
$RollbackController = Join-Path $ControlRoot "scripts\admin\rollback_execute.py"
$ActivePointer = Join-Path $ControlRoot "data\release_state\active_runtime.json"
$IdentityLibrary = Join-Path $ControlRoot "scripts\windows\erp_process_identity.ps1"
$ExpectedIdentityLibrarySha256 = (
    "78ea3977c4cd37bfa2cd1d077bd9d2f8d824f3f692a574fdca5fc18dc62f4598"
)
$ApplicationRoot = $ControlRoot
$ValidationMode = -not [string]::IsNullOrWhiteSpace($ValidationTicket)
$Python = $ControlPython
$DatabasePath = $null
$ExternalHealthUrl = $null
$LocalHealthUrl = $null
$BrowserUrl = $null
$ErpPort = $null
$BindHost = $null
$Workers = $null
$RuntimeEnvironment = $null
$ProductionTransport = $null
$LogDir = Join-Path $ControlRoot "logs"
$LogFile = Join-Path $LogDir "erp_startup.log"
$ServerLog = Join-Path $LogDir "erp_server.log"
$ServerErrorLog = Join-Path $LogDir "erp_server_error.log"
$process = $null
$validatedProcess = $null
$startedByThisScript = $false
$savedEnvironment = @{}
$environmentPrepared = $false

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-Log {
    param([string]$Message)
    Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value (
        "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    )
}

function Assert-ErpIdentityLibrary {
    if (-not (Test-Path -LiteralPath $IdentityLibrary -PathType Leaf)) {
        throw "ERP process identity library not found: $IdentityLibrary"
    }
    $content = [System.IO.File]::ReadAllText(
        $IdentityLibrary,
        [System.Text.Encoding]::UTF8
    )
    if ($content.Length -gt 0 -and $content[0] -eq [char]0xFEFF) {
        $content = $content.Substring(1)
    }
    $normalized = $content.Replace("`r`n", "`n").Replace("`r", "`n")
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $actual = [BitConverter]::ToString(
            $sha256.ComputeHash(
                [System.Text.Encoding]::UTF8.GetBytes($normalized)
            )
        ).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }
    if ($actual -ne $ExpectedIdentityLibrarySha256) {
        throw "ERP process identity library checksum mismatch."
    }
}

Assert-ErpIdentityLibrary
. $IdentityLibrary

function Get-JsonProperty {
    param(
        [object]$Object,
        [string]$Name,
        [object]$Default = $null
    )
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return $Default }
    return $property.Value
}

function Invoke-JsonController {
    param(
        [string]$Label,
        [string[]]$Arguments
    )
    $output = @(
        & $ControlPython -I -B -X utf8 $RollbackController @Arguments 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $output.Count -lt 1) {
        $detail = if ($output.Count -gt 0) {
            $output[-1].ToString()
        } else {
            "$Label did not return a result."
        }
        try {
            $errorPayload = $detail | ConvertFrom-Json
            if (-not [string]::IsNullOrWhiteSpace([string]$errorPayload.error)) {
                $detail = [string]$errorPayload.error
            }
        } catch { }
        throw ("{0} failed: {1}" -f $Label, $detail)
    }
    try {
        return ($output[-1].ToString() | ConvertFrom-Json)
    } catch {
        throw "$Label did not return valid JSON."
    }
}

function Set-ChildEnvironmentValue {
    param(
        [string]$Name,
        [string]$Value
    )
    if (-not $savedEnvironment.ContainsKey($Name)) {
        $savedEnvironment[$Name] = [Environment]::GetEnvironmentVariable(
            $Name,
            "Process"
        )
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Restore-ChildEnvironment {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable(
            $name,
            $savedEnvironment[$name],
            "Process"
        )
    }
}

function Test-PathWithinRoot {
    param(
        [string]$Path,
        [string]$Root
    )
    try {
        $fullPath = [System.IO.Path]::GetFullPath($Path)
        $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        $prefix = $fullRoot + [System.IO.Path]::DirectorySeparatorChar
        return $fullPath.StartsWith(
            $prefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    } catch {
        return $false
    }
}

function Get-ControlRuntimeConfiguration {
    $script = @'
import json
import sys
sys.path.insert(0, sys.argv[1])
from app.core.config import load_settings
s = load_settings()
print(json.dumps({
    'bind_host': s.bind_host,
    'port': s.port,
    'workers': s.workers,
    'environment': s.environment,
    'production_transport': s.production_transport,
    'health_url': s.health_url,
    'browser_url': s.browser_url,
    'database_path': str(s.database_path),
    'backup_dir': str(s.backup_dir),
    'allowed_origins': list(s.allowed_origins),
    'trusted_hosts': list(s.trusted_hosts),
    'trusted_proxy_ips': list(s.trusted_proxy_ips),
    'sqlite_busy_timeout_ms': s.sqlite_busy_timeout_ms,
    'session_cookie_name': s.session_cookie_name,
    'session_expire_minutes': s.session_expire_minutes,
    'session_cookie_secure': s.session_cookie_secure,
}, ensure_ascii=False))
'@
    $output = @(
        & $ControlPython -I -B -X utf8 -c $script $ControlRoot 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $output.Count -lt 1) {
        $output | ForEach-Object { Write-Log $_ }
        throw "ERP control-root runtime configuration is invalid."
    }
    try {
        return ($output[-1].ToString() | ConvertFrom-Json)
    } catch {
        throw "ERP control-root runtime configuration is not valid JSON."
    }
}

function Initialize-ActiveRuntime {
    if (-not (Test-Path -LiteralPath $ControlPython -PathType Leaf)) {
        throw "Python runtime not found. Please check the control-root .venv folder."
    }
    if (-not (Test-Path -LiteralPath $RollbackController -PathType Leaf)) {
        throw "Signed active-runtime resolver not found: $RollbackController"
    }

    $controlConfig = Get-ControlRuntimeConfiguration
    if ($ValidationMode) {
        $resolvedTicket = [System.IO.Path]::GetFullPath($ValidationTicket)
        $resolved = Invoke-JsonController `
            -Label "validation_runtime_resolve" `
            -Arguments @(
                "resolve-validation",
                "--ticket", $resolvedTicket
            )
        if (-not [bool](Get-JsonProperty $resolved "validation_mode" $false)) {
            throw "Validation resolver did not return loopback mode."
        }
    } else {
        $resolved = Invoke-JsonController `
            -Label "active_runtime_resolve" `
            -Arguments @("resolve-active", "--pointer", $ActivePointer)
        if ([bool](Get-JsonProperty $resolved "validation_mode" $false)) {
            throw "Ordinary startup cannot use validation mode."
        }
    }

    $script:ApplicationRoot = [System.IO.Path]::GetFullPath(
        [string](Get-JsonProperty $resolved "runtime_dir" "")
    )
    $script:DatabasePath = [System.IO.Path]::GetFullPath(
        [string](Get-JsonProperty $resolved "database_path" "")
    )
    $script:Python = [System.IO.Path]::GetFullPath(
        [string](Get-JsonProperty $resolved "python_path" "")
    )
    if (-not (Test-Path -LiteralPath $ApplicationRoot -PathType Container)) {
        throw "Signed active application root does not exist: $ApplicationRoot"
    }
    if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
        throw "Signed active database does not exist: $DatabasePath"
    }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Signed active Python runtime does not exist: $Python"
    }

    # Use the current public production configuration for ordinary startup.
    # Validation mode is a signed loopback-only maintenance service and never
    # inherits the LAN/HTTPS exposure settings.
    $resolvedBindHost = [string](Get-JsonProperty $resolved "bind_host" "")
    $resolvedPort = [int](Get-JsonProperty $resolved "port" 0)
    $resolvedWorkers = [int](Get-JsonProperty $resolved "workers" 0)
    $resolvedHealthUrl = [string](Get-JsonProperty $resolved "health_url" "")
    if ($ValidationMode) {
        if (
            $resolvedBindHost -ne "127.0.0.1" -or
            $resolvedWorkers -ne 1 -or
            $resolvedHealthUrl -ne (
                "http://127.0.0.1:{0}/api/health" -f $resolvedPort
            )
        ) {
            throw "Validation runtime is not bound to the signed loopback service."
        }
        $runtimeOrigin = "http://127.0.0.1:$resolvedPort"
        $publicOverrides = @{
            ERP_ENVIRONMENT = "production"
            ERP_BIND_HOST = "127.0.0.1"
            ERP_PORT = $resolvedPort.ToString()
            ERP_WORKERS = "1"
            ERP_PRODUCTION_TRANSPORT = "lan_http"
            ERP_HEALTH_URL = $resolvedHealthUrl
            ERP_BROWSER_URL = "$runtimeOrigin/"
            ERP_ALLOWED_ORIGINS = $runtimeOrigin
            ERP_TRUSTED_HOSTS = "127.0.0.1,localhost"
            ERP_TRUSTED_PROXY_IPS = ""
            ERP_SESSION_COOKIE_SECURE = "false"
        }
    } else {
        $publicOverrides = @{
            ERP_ENVIRONMENT = [string]$controlConfig.environment
            ERP_BIND_HOST = [string]$controlConfig.bind_host
            ERP_PORT = ([int]$controlConfig.port).ToString()
            ERP_WORKERS = ([int]$controlConfig.workers).ToString()
            ERP_PRODUCTION_TRANSPORT = [string]$controlConfig.production_transport
            ERP_HEALTH_URL = [string]$controlConfig.health_url
            ERP_BROWSER_URL = [string]$controlConfig.browser_url
            ERP_ALLOWED_ORIGINS = (@($controlConfig.allowed_origins) -join ",")
            ERP_TRUSTED_HOSTS = (@($controlConfig.trusted_hosts) -join ",")
            ERP_TRUSTED_PROXY_IPS = (@($controlConfig.trusted_proxy_ips) -join ",")
            ERP_SESSION_COOKIE_SECURE = if (
                [bool]$controlConfig.session_cookie_secure
            ) { "true" } else { "false" }
        }
    }
    $publicOverrides.ERP_BACKUP_DIR = [System.IO.Path]::GetFullPath(
        [string]$controlConfig.backup_dir
    )
    $publicOverrides.ERP_SQLITE_BUSY_TIMEOUT_MS = (
        [int]$controlConfig.sqlite_busy_timeout_ms
    ).ToString()
    $publicOverrides.ERP_SESSION_COOKIE_NAME = (
        [string]$controlConfig.session_cookie_name
    )
    $publicOverrides.ERP_SESSION_EXPIRE_MINUTES = (
        [int]$controlConfig.session_expire_minutes
    ).ToString()
    foreach ($entry in $publicOverrides.GetEnumerator()) {
        Set-ChildEnvironmentValue -Name $entry.Key -Value ([string]$entry.Value)
    }
    # Force production authentication to use the control-root secret file.
    # An inherited inline secret must not silently override that signed path.
    Set-ChildEnvironmentValue -Name "ERP_SECRET_KEY" -Value $null

    $signedOverrides = Get-JsonProperty $resolved "environment_overrides" $null
    if ($null -eq $signedOverrides) {
        throw "Signed active runtime is missing environment overrides."
    }
    foreach ($property in $signedOverrides.PSObject.Properties) {
        Set-ChildEnvironmentValue `
            -Name ([string]$property.Name) `
            -Value ([string]$property.Value)
    }
    $script:environmentPrepared = $true

    # Assert the signed mutable-data boundaries again after applying them.
    # Formal startup owns ControlRoot data.  Loopback validation must instead
    # use disposable paths inside this switch's output_root so old code cannot
    # mutate live uploads or the live session secret during warmup.
    if ($ValidationMode) {
        $validationRoot = [System.IO.DirectoryInfo]::new(
            $ApplicationRoot
        ).Parent.Parent.FullName
        $validationPaths = @{
            ERP_DATABASE_PATH = $DatabasePath
            ERP_SECRET_KEY_FILE = [Environment]::GetEnvironmentVariable(
                "ERP_SECRET_KEY_FILE",
                "Process"
            )
            ERP_FILE_STORAGE_DIR = [Environment]::GetEnvironmentVariable(
                "ERP_FILE_STORAGE_DIR",
                "Process"
            )
            ERP_UPLOAD_TEMP_DIR = [Environment]::GetEnvironmentVariable(
                "ERP_UPLOAD_TEMP_DIR",
                "Process"
            )
            ERP_BACKUP_DIR = [Environment]::GetEnvironmentVariable(
                "ERP_BACKUP_DIR",
                "Process"
            )
        }
        foreach ($entry in $validationPaths.GetEnumerator()) {
            if (-not (Test-PathWithinRoot `
                -Path ([string]$entry.Value) `
                -Root $validationRoot
            )) {
                throw (
                    "Validation runtime contains an unsafe {0} path." -f
                    $entry.Key
                )
            }
        }
        $validationSecret = [System.IO.Path]::GetFullPath(
            [string]$validationPaths.ERP_SECRET_KEY_FILE
        )
        if (-not (Test-Path -LiteralPath $validationSecret -PathType Leaf)) {
            throw "Validation runtime session secret does not exist."
        }
        $validationStorage = [System.IO.Path]::GetFullPath(
            [string]$validationPaths.ERP_FILE_STORAGE_DIR
        )
        $validationTemp = [System.IO.Path]::GetFullPath(
            [string]$validationPaths.ERP_UPLOAD_TEMP_DIR
        )
        if (-not (Test-PathWithinRoot `
            -Path $validationTemp `
            -Root $validationStorage
        )) {
            throw "Validation upload temp must be inside validation storage."
        }
    } else {
        $requiredOverrides = @{
            ERP_DATABASE_PATH = $DatabasePath
            ERP_SECRET_KEY_FILE = [System.IO.Path]::GetFullPath(
                (Join-Path $ControlRoot "data\session_secret.key")
            )
            ERP_FILE_STORAGE_DIR = [System.IO.Path]::GetFullPath(
                (Join-Path $ControlRoot "data\private_uploads")
            )
            ERP_UPLOAD_TEMP_DIR = [System.IO.Path]::GetFullPath(
                (Join-Path $ControlRoot "data\private_uploads\_temporary")
            )
        }
        foreach ($entry in $requiredOverrides.GetEnumerator()) {
            $actual = [System.IO.Path]::GetFullPath(
                [Environment]::GetEnvironmentVariable($entry.Key, "Process")
            )
            if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals(
                $actual,
                [string]$entry.Value
            )) {
                throw "Signed active runtime contains an unsafe $($entry.Key) path."
            }
        }
    }

    $script:BindHost = $resolvedBindHost
    $script:ErpPort = $resolvedPort
    $script:LocalHealthUrl = $resolvedHealthUrl
    $script:Workers = $resolvedWorkers
    $script:RuntimeEnvironment = [string](
        Get-JsonProperty $resolved "environment" ""
    )
    $script:ProductionTransport = if ($ValidationMode) {
        "lan_http"
    } else {
        [string]$controlConfig.production_transport
    }
    $script:ExternalHealthUrl = if ($ValidationMode) {
        $resolvedHealthUrl
    } else {
        [string]$controlConfig.health_url
    }
    $script:BrowserUrl = if ($ValidationMode) {
        "http://127.0.0.1:$ErpPort/"
    } else {
        [string]$controlConfig.browser_url
    }

    if (-not $ValidationMode -and (
        $BindHost -ne [string]$controlConfig.bind_host -or
        $ErpPort -ne [int]$controlConfig.port -or
        $Workers -ne [int]$controlConfig.workers -or
        $LocalHealthUrl -ne ("http://127.0.0.1:{0}/api/health" -f $ErpPort)
    )) {
        throw "Signed active runtime service identity differs from current production configuration."
    }
    if ($RuntimeEnvironment -ne "production") {
        throw (
            "Factory launcher requires ERP_ENVIRONMENT=production. " +
            "Use scripts\windows\start_erp_uat.ps1 for an isolated UAT copy."
        )
    }
    if ($Workers -ne 1) {
        throw "ERP must run with exactly one worker for atomic login throttling."
    }
    if ($ValidationMode) {
        Write-Log "Validation runtime is loopback-only and cannot open a browser."
    } elseif ($ProductionTransport -eq "https_proxy") {
        if ($ExternalHealthUrl -notlike "https://*" -or $BrowserUrl -notlike "https://*") {
            throw "https_proxy requires HTTPS health and browser URLs."
        }
    } elseif ($ProductionTransport -eq "lan_http") {
        if ($ExternalHealthUrl -notlike "http://*" -or $BrowserUrl -notlike "http://*") {
            throw "lan_http requires HTTP health and browser URLs."
        }
    } else {
        throw "Unknown ERP_PRODUCTION_TRANSPORT: $ProductionTransport"
    }

    Write-Log (
        "Resolved signed runtime {0} ({1}) at {2}." -f
        [string](Get-JsonProperty $resolved "runtime_id" ""),
        [string](Get-JsonProperty $resolved "runtime_kind" ""),
        $ApplicationRoot
    )
    if (
        $ValidationMode -or
        [bool](Get-JsonProperty $resolved "pointer_present" $false)
    ) {
        $runtimeLogId = (
            [string](Get-JsonProperty $resolved "runtime_id" "active")
        ) -replace '[^A-Za-z0-9_-]', '_'
        $runtimeLogStamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
        $script:ServerLog = Join-Path $LogDir (
            "erp_server_{0}_{1}.log" -f $runtimeLogId, $runtimeLogStamp
        )
        $script:ServerErrorLog = Join-Path $LogDir (
            "erp_server_{0}_{1}_error.log" -f
            $runtimeLogId,
            $runtimeLogStamp
        )
        Write-Log ("Active runtime server logs: {0}" -f $ServerLog)
    }
}

function Test-LocalErpRunning {
    return (Test-ErpHealthContract -Url $LocalHealthUrl -TimeoutSeconds 2)
}

function Test-ExternalErpHealth {
    return (Test-ErpHealthContract -Url $ExternalHealthUrl -TimeoutSeconds 5)
}

function Get-ValidatedActiveErpProcess {
    return (Get-ValidatedErpProcessIdentity `
        -Port $ErpPort `
        -Workers $Workers `
        -PythonPath $Python `
        -ApplicationRoot $ApplicationRoot `
        -BindHost $BindHost)
}

function Confirm-ProductionExternalHealth {
    if ($ValidationMode) { return }
    if ($RuntimeEnvironment -ne "production") { return }
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
    if ($NoBrowser -or $ValidationMode) { return }
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
    $command = Start-Process `
        -FilePath $Python `
        -ArgumentList $fullArguments `
        -WorkingDirectory $ApplicationRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru `
        -Wait
    foreach ($path in @($stdout, $stderr)) {
        if (Test-Path -LiteralPath $path) {
            Get-Content -LiteralPath $path -ErrorAction SilentlyContinue |
                ForEach-Object {
                    Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value $_
                }
        }
    }
    if ($command.ExitCode -ne 0) {
        throw ("{0} failed with exit code {1}." -f $Label, $command.ExitCode)
    }
}

try {
    Write-Log "Startup begin."
    Initialize-ActiveRuntime

    $releaseGate = Join-Path $ApplicationRoot "scripts\admin\release_erp.py"
    if (-not (Test-Path -LiteralPath $releaseGate -PathType Leaf)) {
        throw "Active runtime release gate helper not found: $releaseGate"
    }
    Write-Log "Checking active database integrity and Alembic revision without migration."
    Invoke-PythonCommand `
        -Label "startup_revision_check" `
        -Arguments @($releaseGate, "check-startup", "--database", $DatabasePath)

    if (Test-LocalErpRunning) {
        $validatedProcess = Get-ValidatedActiveErpProcess
        Write-Log "ERP already running."
        Confirm-ProductionExternalHealth
        Open-Browser
        Write-Host "ERP already running. Browser opened."
        exit 0
    }

    $listening = Get-NetTCPConnection `
        -LocalPort $ErpPort `
        -State Listen `
        -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Log ("Port {0} is already in use." -f $ErpPort)
        throw ("Port {0} is already in use. ERP cannot start." -f $ErpPort)
    }

    Write-Log ("Starting uvicorn from signed active root: {0}" -f $ApplicationRoot)
    $arguments = @(
        "-X", "utf8",
        "-m", "uvicorn",
        "app.main:app",
        "--app-dir", $ApplicationRoot,
        "--host", $BindHost,
        "--port", $ErpPort.ToString(),
        "--workers", "1"
    )
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $arguments `
        -WorkingDirectory $ApplicationRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $ServerLog `
        -RedirectStandardError $ServerErrorLog `
        -PassThru
    $startedByThisScript = $true

    $ready = $false
    for ($index = 0; $index -lt 20; $index++) {
        Start-Sleep -Seconds 1
        if (Test-LocalErpRunning) {
            $validatedProcess = Get-ValidatedActiveErpProcess
            $ready = $true
            break
        }
        if ($process.HasExited) { break }
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
    if ($startedByThisScript -and $validatedProcess) {
        $validatedProcessId = [int]$validatedProcess.ProcessId
        if (-not $process -or $validatedProcessId -ne $process.Id) {
            Stop-Process `
                -Id $validatedProcessId `
                -Force `
                -ErrorAction SilentlyContinue
            Write-Log (
                "Stopped failed validated ERP listener PID={0}." -f
                $validatedProcessId
            )
        }
    }
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        try { $process.WaitForExit(5000) | Out-Null } catch { }
        Write-Log ("Stopped failed ERP process PID={0}." -f $process.Id)
    }
    Write-Log ("Startup failed: {0}" -f $_.Exception.Message)
    Write-Host $_.Exception.Message
    Write-Host "ERP startup failed. Please check logs\erp_startup.log"
    exit 1
} finally {
    if ($environmentPrepared -or $savedEnvironment.Count -gt 0) {
        Restore-ChildEnvironment
    }
}

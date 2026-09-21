#Requires -Version 5.1
<#
.SYNOPSIS
  Start only the R03 disposable factory UAT, with the same isolation gate as the family launcher.

.DESCRIPTION
  This is a separate, factory-host-only entry point.  It does not weaken or call
  the family UAT launcher, which remains prohibited on the factory host.
#>
param(
    [Parameter(Mandatory = $true)][string]$DatabasePath,
    [Parameter(Mandatory = $true)][string]$UatRoot,
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [ValidateRange(18000, 19999)][int]$Port = 18123,
    [string]$GitPath = "D:\Git\cmd\git.exe",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$factoryHost = "PC-20250926DZYH"

function Test-PathInside([string]$Candidate, [string]$Root) {
    $candidateValue = [IO.Path]::GetFullPath($Candidate)
    $rootValue = [IO.Path]::GetFullPath($Root).TrimEnd("\") + "\"
    return $candidateValue.StartsWith($rootValue, [StringComparison]::OrdinalIgnoreCase)
}
function ConvertTo-Base64Json([object]$Value) {
    $json = $Value | ConvertTo-Json -Compress -Depth 12
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
}
function Invoke-Helper([string[]]$Arguments, [string]$FailureLabel) {
    $output = @(& $script:python -I $script:helper @Arguments 2>&1)
    $text = ($output -join "`n").Trim()
    if ($LASTEXITCODE -ne 0) { throw ("{0}: {1}" -f $FailureLabel, $text) }
    try { return $text | ConvertFrom-Json } catch { throw ("{0}: invalid helper JSON" -f $FailureLabel) }
}
function Invoke-UatChild([string[]]$Command, [string]$FailureLabel) {
    $result = Invoke-Helper @("run", "--cwd", $script:projectRoot, "--python", $script:python, "--git", $script:git, "--temp-dir", $script:tempDir, "--environment-base64", $script:environmentBase64, "--command-base64", (ConvertTo-Base64Json @($Command))) $FailureLabel
    if ([int]$result.returncode -ne 0) { throw ("{0}: {1}`n{2}" -f $FailureLabel, $result.stderr, $result.stdout) }
    return [string]$result.stdout
}

if ([Environment]::MachineName -ine $factoryHost) { throw "This R03 factory entry point is restricted to $factoryHost." }
$script:projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:python = [IO.Path]::GetFullPath($PythonPath)
$script:git = [IO.Path]::GetFullPath($GitPath)
$script:helper = Join-Path $script:projectRoot "app\core\uat_isolation.py"
$root = [IO.Path]::GetFullPath($UatRoot)
$database = [IO.Path]::GetFullPath($DatabasePath)
$factoryUatBase = [IO.Path]::GetFullPath("D:\tm-uat")
if (-not (Test-Path -LiteralPath $script:python -PathType Leaf) -or -not (Test-Path -LiteralPath $script:git -PathType Leaf)) { throw "PythonPath and GitPath must name existing files." }
if (-not (Test-PathInside $root $factoryUatBase) -or ([IO.Path]::GetDirectoryName($root) -ine $factoryUatBase)) { throw "UatRoot must be a direct child of D:\tm-uat." }
if ($database -ine (Join-Path $root "carton_erp.sqlite3") -or -not (Test-Path -LiteralPath $database -PathType Leaf)) { throw "DatabasePath must be the existing disposable <UatRoot>\carton_erp.sqlite3." }
if ($Port -eq 8000 -or (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { throw "UAT port is invalid or already listening." }

$protectedPaths = @($script:projectRoot, "D:\TianmingERP", "D:\纸箱厂erp软件搭建", "Z:\sata1-18015598002\BoxERP\backups")
$nonce = [Guid]::NewGuid().ToString("N")
$prepareArguments = @("prepare", "--root", $root, "--database", $database, "--port", $Port.ToString(), "--layout-source", (Join-Path $script:projectRoot "static\factory_maps\twin_layout_v1.json"), "--launch-nonce", $nonce)
foreach ($path in $protectedPaths) { $prepareArguments += @("--protected-path", $path) }
foreach ($path in $protectedPaths) { $prepareArguments += @("--forbidden-root", $path) }
$isolation = Invoke-Helper $prepareArguments "Factory R03 UAT preparation failed"
$script:tempDir = [string]$isolation.temp_dir
$managed = [ordered]@{
    ERP_UAT_ROOT=[string]$isolation.root; ERP_UAT_ISOLATION_ID=[string]$isolation.isolation_id; ERP_UAT_LAUNCH_NONCE=$nonce; ERP_UAT_ATTESTATION_PATH=[string]$isolation.paths.attestation_file; ERP_UAT_LEASE_PATH=[string]$isolation.paths.lease_file; ERP_UAT_PID_PATH=[string]$isolation.paths.pid_file; ERP_UAT_STDOUT_PATH=[string]$isolation.paths.stdout_log; ERP_UAT_STDERR_PATH=[string]$isolation.paths.stderr_log; ERP_UAT_GIT_PATH=$script:git; ERP_UAT_PROTECTED_PATHS_JSON=($protectedPaths | ConvertTo-Json -Compress); ERP_UAT_FORBIDDEN_ROOTS_JSON=($protectedPaths | ConvertTo-Json -Compress); ERP_ENVIRONMENT="test"; ERP_DATABASE_PATH=[string]$isolation.paths.database; ERP_BIND_HOST="127.0.0.1"; ERP_PORT=$Port.ToString(); ERP_WORKERS="1"; ERP_HEALTH_URL="http://127.0.0.1:$Port/api/health"; ERP_BROWSER_URL="http://127.0.0.1:$Port/"; ERP_ALLOWED_ORIGINS="http://127.0.0.1:$Port,http://localhost:$Port"; ERP_TRUSTED_HOSTS=""; ERP_TRUSTED_PROXY_IPS=""; ERP_PRODUCTION_TRANSPORT="development"; ERP_SESSION_COOKIE_NAME=[string]$isolation.cookie_name; ERP_SESSION_COOKIE_SECURE="false"; ERP_SECRET_KEY=""; ERP_SECRET_KEY_FILE=[string]$isolation.paths.secret_file; ERP_LOG_DIR=[string]$isolation.paths.log_dir; ERP_BACKUP_DIR=[string]$isolation.paths.backup_dir; ERP_FILE_STORAGE_DIR=[string]$isolation.paths.private_upload_dir; ERP_UPLOAD_TEMP_DIR=[string]$isolation.paths.upload_temp_dir; ERP_INVOICE_EXPORT_DIR=[string]$isolation.paths.invoice_export_dir; ERP_INVOICE_ATTACHMENT_DIR=[string]$isolation.paths.invoice_attachment_dir; ERP_PDF_TRAINING_DIR=[string]$isolation.paths.pdf_training_dir; ERP_TWIN_LAYOUT_RUNTIME_PATH=[string]$isolation.paths.layout_runtime_file; ERP_TWIN_LAYOUT_DRAFT_PATH=[string]$isolation.paths.layout_draft_file; ERP_TWIN_LAYOUT_BACKUP_DIR=[string]$isolation.paths.layout_backup_dir; ERP_LEGACY_UPLOAD_DIR=[string]$isolation.paths.legacy_upload_dir; ERP_DELIVERY_PRINT_SETTINGS_PATH=[string]$isolation.paths.delivery_print_settings_file; ERP_FACTORY_TWIN_DATABASE_PATH=[string]$isolation.paths.factory_twin_database }
$script:environmentBase64 = ConvertTo-Base64Json $managed
Invoke-UatChild @($script:python, "-I", $script:helper, "validate") "Factory R03 write-root validation failed" | Out-Null
Invoke-UatChild @($script:python, "-I", "-X", "utf8", (Join-Path $script:projectRoot "scripts\admin\release_erp.py"), "check-startup", "--database", $database) "Factory R03 database validation failed" | Out-Null
if ($ValidateOnly) { $isolation | ConvertTo-Json -Depth 8; return }
$spawn = Invoke-Helper @("spawn", "--python", $script:python, "--git", $script:git, "--project-root", $script:projectRoot, "--port", $Port.ToString(), "--stdout", [string]$isolation.paths.stdout_log, "--stderr", [string]$isolation.paths.stderr_log, "--nonce", $nonce, "--lease", [string]$isolation.paths.lease_file, "--pid", [string]$isolation.paths.pid_file, "--attestation", [string]$isolation.paths.attestation_file, "--temp-dir", $script:tempDir, "--environment-base64", $script:environmentBase64) "Factory R03 UAT spawn failed"
$ready = $false; for ($i=0; $i -lt 30; $i++) { Start-Sleep -Seconds 1; try { if ((Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { $ready=$true; break } } catch {} }
if (-not $ready) { throw "Factory R03 UAT did not become healthy." }
Invoke-UatChild @($script:python, "-I", $script:helper, "assert-ownership", "--pid", [string]$isolation.paths.pid_file, "--attestation", [string]$isolation.paths.attestation_file, "--expected-pid", ([int]$spawn.pid).ToString(), "--expected-nonce", $nonce, "--port", $Port.ToString()) "Factory R03 ownership validation failed" | Out-Null
[pscustomobject]@{ url="http://127.0.0.1:$Port/"; pid=[int]$spawn.pid; root=[string]$isolation.root; database=[string]$isolation.paths.database; attestation=[string]$isolation.paths.attestation_file; cookie_name=[string]$isolation.cookie_name } | ConvertTo-Json

#Requires -Version 5.1
<#
.SYNOPSIS
    从独立 SQLite 副本和独立文件根启动家庭 UAT。

.DESCRIPTION
    本入口强制 test + loopback，不执行 Alembic 迁移。数据库、Cookie、密钥、日志、
    备份、上传、发票文件、PDF 训练、仓库地图和孪生库路径全部绑定到单一 UAT run 根。
    PowerShell 父进程环境保持不变；所有 Python gate 和服务只接收显式、去重的子环境。
#>

param(
    [string]$DatabasePath,
    [string]$UatRoot,

    [ValidateRange(18000, 19999)]
    [int]$Port = 18080,

    [string]$PythonPath,
    [string]$GitPath,
    [string]$SourceDatabasePath,
    [string]$FactoryTwinDatabaseSource,
    [switch]$NoBrowser,
    [switch]$ValidateOnly,
    [switch]$LibraryOnly
)

$ErrorActionPreference = "Stop"

function Test-PathInside {
    param([string]$Candidate, [string]$Root)
    $candidateValue = [System.IO.Path]::GetFullPath($Candidate)
    $rootValue = [System.IO.Path]::GetFullPath($Root).TrimEnd("\") + "\"
    return $candidateValue.StartsWith(
        $rootValue,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function ConvertTo-Base64Json {
    param([object]$Value)
    $json = $Value | ConvertTo-Json -Compress -Depth 12
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
}

function Invoke-IsolationHelper {
    param(
        [string[]]$Arguments,
        [string]$FailureLabel
    )
    $output = @(& $script:python -I $script:isolationHelper @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $text = ($output -join "`n").Trim()
    if ($exitCode -ne 0) {
        $detail = $text
        try {
            $failure = $text | ConvertFrom-Json
            if ($failure.error) { $detail = [string]$failure.error }
        } catch { }
        throw ("{0}：{1}" -f $FailureLabel, $detail)
    }
    try {
        return $text | ConvertFrom-Json
    } catch {
        throw ("{0}：helper 输出不是有效 JSON。" -f $FailureLabel)
    }
}

function Invoke-UatChild {
    param(
        [string[]]$Command,
        [string]$FailureLabel
    )
    $commandBase64 = ConvertTo-Base64Json -Value @($Command)
    $result = Invoke-IsolationHelper -FailureLabel $FailureLabel -Arguments @(
        "run",
        "--cwd", $script:projectRoot,
        "--python", $script:python,
        "--git", $script:git,
        "--temp-dir", $script:tempDir,
        "--environment-base64", $script:environmentBase64,
        "--command-base64", $commandBase64
    )
    if ([int]$result.returncode -ne 0) {
        $detail = (([string]$result.stderr) + "`n" + ([string]$result.stdout)).Trim()
        throw ("{0}：{1}" -f $FailureLabel, $detail)
    }
    return [string]$result.stdout
}

if ($LibraryOnly) { return }

$script:projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:isolationHelper = Join-Path $script:projectRoot "app\core\uat_isolation.py"
$script:python = if ($PythonPath) {
    [System.IO.Path]::GetFullPath($PythonPath)
} else {
    Join-Path $script:projectRoot ".venv\Scripts\python.exe"
}
$gitCandidates = @(
    $GitPath,
    "D:\Git\cmd\git.exe",
    "C:\Program Files\Git\cmd\git.exe",
    "C:\Program Files (x86)\Git\cmd\git.exe"
) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
$script:git = $null
foreach ($candidate in $gitCandidates) {
    $resolvedCandidate = [System.IO.Path]::GetFullPath([string]$candidate)
    if (Test-Path -LiteralPath $resolvedCandidate -PathType Leaf) {
        $script:git = $resolvedCandidate
        break
    }
}
if (-not $script:git) {
    throw "必须通过 -GitPath 提供可信的 git.exe 绝对路径。"
}

$releaseGate = Join-Path $script:projectRoot "scripts\admin\release_erp.py"
$weeklyUatGate = Join-Path $script:projectRoot "scripts\admin\weekly_home_uat.py"
$trackedLayout = Join-Path $script:projectRoot "static\factory_maps\twin_layout_v1.json"
$checkoutDatabase = [System.IO.Path]::GetFullPath(
    (Join-Path $script:projectRoot "data\carton_erp.sqlite3")
)
$factoryRoot = [System.IO.Path]::GetFullPath("D:\纸箱厂erp软件搭建")
$factoryDatabase = [System.IO.Path]::GetFullPath(
    "D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3"
)
$formalBackupRoot = [System.IO.Path]::GetFullPath(
    "Z:\sata1-18015598002\BoxERP\backups"
)
$isolation = $null
$process = $null
$launchNonce = [Guid]::NewGuid().ToString("N")

try {
    if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
        throw "必须显式提供 UAT 数据库副本路径。"
    }
    if ([string]::IsNullOrWhiteSpace($UatRoot)) {
        throw "必须显式提供 UatRoot；不再允许无根目录的非隔离模式。"
    }
    if ([Environment]::MachineName -ieq "PC-20250926DZYH") {
        throw "家庭 UAT 启动器禁止在工厂正式主机运行。"
    }
    foreach ($requiredFile in @(
        $script:python,
        $script:isolationHelper,
        $releaseGate,
        $weeklyUatGate,
        $trackedLayout
    )) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "UAT 启动门禁文件不存在：$requiredFile"
        }
    }

    $resolvedDatabase = [System.IO.Path]::GetFullPath($DatabasePath)
    $resolvedUatRoot = [System.IO.Path]::GetFullPath($UatRoot)
    $resolvedRunRoot = [System.IO.Path]::GetDirectoryName($resolvedDatabase)
    $resolvedRunsRoot = Join-Path $resolvedUatRoot "runs"
    if (-not (
        [System.StringComparer]::OrdinalIgnoreCase.Equals(
            $resolvedRunRoot,
            $resolvedUatRoot
        ) -or (Test-PathInside -Candidate $resolvedDatabase -Root $resolvedRunsRoot)
    )) {
        throw "UAT 数据库必须位于 UatRoot 或 UatRoot\runs 的独立运行目录。"
    }
    if ($Port -eq 8000) { throw "UAT 禁止使用正式端口 8000。" }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw "UAT 端口 $Port 已被占用。"
    }

    $protectedPaths = @(
        $checkoutDatabase,
        $factoryDatabase,
        (Join-Path $script:projectRoot "data"),
        (Join-Path $script:projectRoot "logs"),
        (Join-Path $script:projectRoot "static\uploads"),
        (Join-Path $script:projectRoot "factory_twin\data"),
        $formalBackupRoot
    )
    $forbiddenRoots = @($script:projectRoot, $factoryRoot, $formalBackupRoot)
    $prepareArguments = @(
        "prepare",
        "--root", $resolvedRunRoot,
        "--database", $resolvedDatabase,
        "--port", $Port.ToString(),
        "--layout-source", $trackedLayout,
        "--launch-nonce", $launchNonce
    )
    foreach ($path in $protectedPaths) {
        $prepareArguments += @("--protected-path", $path)
    }
    foreach ($root in $forbiddenRoots) {
        $prepareArguments += @("--forbidden-root", $root)
    }
    if (-not [string]::IsNullOrWhiteSpace($SourceDatabasePath)) {
        $prepareArguments += @(
            "--source-database",
            [System.IO.Path]::GetFullPath($SourceDatabasePath)
        )
    }
    if (-not [string]::IsNullOrWhiteSpace($FactoryTwinDatabaseSource)) {
        $prepareArguments += @(
            "--twin-database-source",
            [System.IO.Path]::GetFullPath($FactoryTwinDatabaseSource)
        )
    }
    $isolation = Invoke-IsolationHelper `
        -Arguments $prepareArguments `
        -FailureLabel "UAT 隔离根准备失败"
    $script:tempDir = [string]$isolation.temp_dir

    $managedValues = [ordered]@{
        ERP_UAT_ROOT = [string]$isolation.root
        ERP_UAT_ISOLATION_ID = [string]$isolation.isolation_id
        ERP_UAT_LAUNCH_NONCE = $launchNonce
        ERP_UAT_ATTESTATION_PATH = [string]$isolation.paths.attestation_file
        ERP_UAT_LEASE_PATH = [string]$isolation.paths.lease_file
        ERP_UAT_PID_PATH = [string]$isolation.paths.pid_file
        ERP_UAT_STDOUT_PATH = [string]$isolation.paths.stdout_log
        ERP_UAT_STDERR_PATH = [string]$isolation.paths.stderr_log
        ERP_UAT_GIT_PATH = $script:git
        ERP_UAT_PROTECTED_PATHS_JSON = ($protectedPaths | ConvertTo-Json -Compress)
        ERP_UAT_FORBIDDEN_ROOTS_JSON = ($forbiddenRoots | ConvertTo-Json -Compress)
        ERP_ENVIRONMENT = "test"
        ERP_DATABASE_PATH = [string]$isolation.paths.database
        ERP_BIND_HOST = "127.0.0.1"
        ERP_PORT = $Port.ToString()
        ERP_WORKERS = "1"
        ERP_HEALTH_URL = "http://127.0.0.1:$Port/api/health"
        ERP_BROWSER_URL = "http://127.0.0.1:$Port/"
        ERP_ALLOWED_ORIGINS = "http://127.0.0.1:$Port,http://localhost:$Port"
        ERP_TRUSTED_HOSTS = ""
        ERP_TRUSTED_PROXY_IPS = ""
        ERP_PRODUCTION_TRANSPORT = "development"
        ERP_SESSION_COOKIE_NAME = [string]$isolation.cookie_name
        ERP_SESSION_COOKIE_SECURE = "false"
        ERP_SECRET_KEY = ""
        ERP_SECRET_KEY_FILE = [string]$isolation.paths.secret_file
        ERP_LOG_DIR = [string]$isolation.paths.log_dir
        ERP_BACKUP_DIR = [string]$isolation.paths.backup_dir
        ERP_FILE_STORAGE_DIR = [string]$isolation.paths.private_upload_dir
        ERP_UPLOAD_TEMP_DIR = [string]$isolation.paths.upload_temp_dir
        ERP_INVOICE_EXPORT_DIR = [string]$isolation.paths.invoice_export_dir
        ERP_INVOICE_ATTACHMENT_DIR = [string]$isolation.paths.invoice_attachment_dir
        ERP_PDF_TRAINING_DIR = [string]$isolation.paths.pdf_training_dir
        ERP_TWIN_LAYOUT_RUNTIME_PATH = [string]$isolation.paths.layout_runtime_file
        ERP_TWIN_LAYOUT_DRAFT_PATH = [string]$isolation.paths.layout_draft_file
        ERP_TWIN_LAYOUT_BACKUP_DIR = [string]$isolation.paths.layout_backup_dir
        ERP_LEGACY_UPLOAD_DIR = [string]$isolation.paths.legacy_upload_dir
        ERP_DELIVERY_PRINT_SETTINGS_PATH = [string]$isolation.paths.delivery_print_settings_file
        ERP_FACTORY_TWIN_DATABASE_PATH = [string]$isolation.paths.factory_twin_database
    }
    $script:environmentBase64 = ConvertTo-Base64Json -Value $managedValues

    $runtimeFile = Join-Path $resolvedRunRoot "runtime.json"
    if (Test-Path -LiteralPath $runtimeFile -PathType Leaf) {
        Invoke-UatChild -FailureLabel "家庭 UAT runtime/SHA 门禁失败" -Command @(
            $script:python, "-I", "-X", "utf8", $weeklyUatGate, "check-start",
            "--database", $resolvedDatabase,
            "--uat-root", $resolvedUatRoot,
            "--project-root", $script:projectRoot,
            "--port", $Port.ToString()
        ) | Out-Null
    }
    Invoke-UatChild -FailureLabel "UAT 数据库 revision/完整性门禁失败" -Command @(
        $script:python, "-I", "-X", "utf8", $releaseGate, "check-startup",
        "--database", $resolvedDatabase
    ) | Out-Null
    Invoke-UatChild -FailureLabel "UAT 全写根门禁失败" -Command @(
        $script:python, "-I", $script:isolationHelper, "validate"
    ) | Out-Null

    Write-Host "UAT 启动前隔离清单（只含路径与状态，不含密钥内容）："
    Write-Host ("  isolation_id: " + $isolation.isolation_id)
    Write-Host ("  root: " + $isolation.root)
    Write-Host ("  port/bind/cookie: {0} / 127.0.0.1 / {1}" -f $Port, $isolation.cookie_name)
    foreach ($property in $isolation.paths.PSObject.Properties) {
        Write-Host ("  {0}: {1}" -f $property.Name, [string]$property.Value)
    }
    Write-Host ("  temporary: " + $isolation.temp_dir)
    Write-Host ("  factory_twin_status: " + $isolation.factory_twin_status)
    if ($ValidateOnly) {
        Write-Host "UAT 启动前隔离门禁通过；ValidateOnly 未启动服务。"
        return
    }

    $spawn = Invoke-IsolationHelper -FailureLabel "UAT 进程启动失败" -Arguments @(
        "spawn",
        "--python", $script:python,
        "--git", $script:git,
        "--project-root", $script:projectRoot,
        "--port", $Port.ToString(),
        "--stdout", [string]$isolation.paths.stdout_log,
        "--stderr", [string]$isolation.paths.stderr_log,
        "--nonce", $launchNonce,
        "--lease", [string]$isolation.paths.lease_file,
        "--pid", [string]$isolation.paths.pid_file,
        "--attestation", [string]$isolation.paths.attestation_file,
        "--temp-dir", $script:tempDir,
        "--environment-base64", $script:environmentBase64
    )
    $process = Get-Process -Id ([int]$spawn.pid) -ErrorAction Stop
    $healthUrl = "http://127.0.0.1:$Port/api/health"
    $ready = $false
    for ($index = 0; $index -lt 30; $index++) {
        Start-Sleep -Seconds 1
        $process.Refresh()
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
    $process.Refresh()
    if (-not $ready -or $process.HasExited) {
        throw "UAT 未在 30 秒内以本次进程就绪。"
    }
    Invoke-UatChild -FailureLabel "UAT 启动后实际 DB/写根/进程归属证明失败" -Command @(
        $script:python, "-I", $script:isolationHelper, "assert-ownership",
        "--pid", [string]$isolation.paths.pid_file,
        "--attestation", [string]$isolation.paths.attestation_file,
        "--expected-pid", $process.Id.ToString(),
        "--expected-nonce", $launchNonce,
        "--port", $Port.ToString()
    ) | Out-Null

    $browserUrl = "http://127.0.0.1:$Port/"
    if (-not $NoBrowser) {
        Invoke-UatChild -FailureLabel "打开 UAT 浏览器失败" -Command @(
            $script:python, "-I", "-c",
            "import sys,webbrowser; raise SystemExit(0 if webbrowser.open(sys.argv[1]) else 1)",
            $browserUrl
        ) | Out-Null
    }
    Write-Host "UAT 已启动：$browserUrl"
    Write-Host "UAT 数据库副本：$resolvedDatabase"
    Write-Host ("UAT PID ownership：" + $isolation.paths.pid_file)
    Write-Host "启动后实际 DB、全部写根、nonce 与监听进程复核通过；未执行数据库迁移。"
} catch {
    if ($isolation) {
        try {
            Invoke-IsolationHelper -FailureLabel "UAT 所有权清理失败" -Arguments @(
                "cleanup-owned",
                "--lease", [string]$isolation.paths.lease_file,
                "--pid", [string]$isolation.paths.pid_file,
                "--attestation", [string]$isolation.paths.attestation_file,
                "--nonce", $launchNonce,
                "--terminate"
            ) | Out-Null
        } catch {
            Write-Warning $_.Exception.Message
        }
    }
    Write-Error $_.Exception.Message
    exit 1
}

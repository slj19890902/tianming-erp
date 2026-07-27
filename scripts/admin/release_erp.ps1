#Requires -Version 5.1
<#
.SYNOPSIS
    天明 ERP P0-A 两阶段发布门禁。

.DESCRIPTION
    Prepare 阶段：验证生产配置/代码 SHA，停服，创建并校验备份，在隔离副本迁移，
    生成与本次证据绑定的人工授权口令。该阶段不写正式数据库，也不会自动重启。

    Apply 阶段：再次验证代码、数据库未变化和人工授权口令后，才允许把正式数据库
    精确迁移到指定 revision；任何失败都会保持停服。迁移及启动成功后更新发布报告。
#>

[CmdletBinding(DefaultParameterSetName = "Help")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Prepare")]
    [switch]$Prepare,

    [Parameter(Mandatory = $true, ParameterSetName = "Prepare")]
    [ValidatePattern("^[0-9a-fA-F]{40}$")]
    [string]$ExpectedCodeSha,

    [Parameter(Mandatory = $true, ParameterSetName = "Prepare")]
    [ValidatePattern("^[0-9a-fA-F]{40}$")]
    [string]$PreviousCodeSha,

    [Parameter(Mandatory = $true, ParameterSetName = "Prepare")]
    [string]$ExpectedRevision,

    [Parameter(Mandatory = $true, ParameterSetName = "Apply")]
    [switch]$Apply,

    [Parameter(Mandatory = $true, ParameterSetName = "Apply")]
    [string]$PlanPath,

    [Parameter(Mandatory = $true, ParameterSetName = "Apply")]
    [string]$ApprovalToken,

    [Parameter(ParameterSetName = "Library")]
    [switch]$LibraryOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$expectedErpPython = [System.IO.Path]::GetFullPath(
    (Join-Path $projectRoot ".venv\Scripts\python.exe")
)
$formalDatabasePath = [System.IO.Path]::GetFullPath(
    (Join-Path $projectRoot "data\carton_erp.sqlite3")
)
$backupDir = Join-Path $projectRoot "data\backups"
$rehearsalDir = Join-Path $projectRoot "data\release_rehearsals"
$reportDir = Join-Path $projectRoot "docs\migration_reports"
$releaseStateDir = Join-Path $projectRoot "data\release_state"
$latestReleasePointer = Join-Path $releaseStateDir "latest_completed_release.json"
$logDir = Join-Path $projectRoot "logs"
$releaseLog = Join-Path $logDir "erp_release.log"
$releaseHelper = Join-Path $PSScriptRoot "release_erp.py"
$ErpPort = $null
$python = $null
$expectedBasePython = $null
$serviceStartedByRelease = $false

function Write-Log([string]$Message, [string]$Level = "INFO") {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Host $line
    if (Test-Path -LiteralPath $logDir) {
        Add-Content -LiteralPath $releaseLog -Encoding UTF8 -Value $line
    }
}

function Split-ErpCommandLine([string]$CommandLine) {
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        throw "进程命令行为空。"
    }
    $tokens = New-Object 'System.Collections.Generic.List[string]'
    $buffer = New-Object System.Text.StringBuilder
    $inQuotes = $false
    for ($index = 0; $index -lt $CommandLine.Length; $index++) {
        $character = $CommandLine[$index]
        if ($character -eq '"') {
            $inQuotes = -not $inQuotes
            continue
        }
        if ([char]::IsWhiteSpace($character) -and -not $inQuotes) {
            if ($buffer.Length -gt 0) {
                $tokens.Add($buffer.ToString())
                $null = $buffer.Clear()
            }
            continue
        }
        $null = $buffer.Append($character)
    }
    if ($inQuotes) { throw "进程命令行包含未闭合引号。" }
    if ($buffer.Length -gt 0) { $tokens.Add($buffer.ToString()) }
    return $tokens.ToArray()
}

function Test-ErpCommandLineIdentity(
    [string]$CommandLine,
    [string]$ExpectedAppDir
) {
    try {
        $tokens = @(Split-ErpCommandLine -CommandLine $CommandLine)
        $moduleIndexes = @(
            for ($index = 0; $index -lt $tokens.Count; $index++) {
                if ($tokens[$index] -ieq "-m") { $index }
            }
        )
        if ($moduleIndexes.Count -ne 1) { return $false }
        $moduleIndex = [int]$moduleIndexes[0]
        if ($moduleIndex + 2 -ge $tokens.Count) { return $false }
        if ($tokens[$moduleIndex + 1] -ine "uvicorn") { return $false }
        if ($tokens[$moduleIndex + 2] -ine "app.main:app") { return $false }
        if (@($tokens | Where-Object { $_ -ieq "uvicorn" }).Count -ne 1) { return $false }
        if (@($tokens | Where-Object { $_ -ieq "app.main:app" }).Count -ne 1) { return $false }

        $appDirIndexes = @(
            for ($index = 0; $index -lt $tokens.Count; $index++) {
                if ($tokens[$index] -ieq "--app-dir") { $index }
            }
        )
        if ($appDirIndexes.Count -ne 1) { return $false }
        $appDirIndex = [int]$appDirIndexes[0]
        if ($appDirIndex + 1 -ge $tokens.Count) { return $false }
        if (-not [System.IO.Path]::IsPathRooted($tokens[$appDirIndex + 1])) { return $false }
        $actualAppDir = [System.IO.Path]::GetFullPath($tokens[$appDirIndex + 1])
        $expected = [System.IO.Path]::GetFullPath($ExpectedAppDir)
        if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals($actualAppDir, $expected)) {
            return $false
        }

        $workerIndexes = @(
            for ($index = 0; $index -lt $tokens.Count; $index++) {
                if ($tokens[$index] -ieq "--workers") { $index }
            }
        )
        if ($workerIndexes.Count -ne 1) { return $false }
        $workerIndex = [int]$workerIndexes[0]
        if ($workerIndex + 1 -ge $tokens.Count) { return $false }
        return $tokens[$workerIndex + 1] -eq "1"
    } catch {
        return $false
    }
}

function Get-ValidatedErpProcess([int]$ProcessId) {
    $processInfo = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction Stop
    if (-not $processInfo -or -not $processInfo.ExecutablePath) {
        throw "无法通过 CIM 验证端口进程 PID=$ProcessId，禁止停止。"
    }
    $actualExecutable = [System.IO.Path]::GetFullPath($processInfo.ExecutablePath)
    $allowedExecutables = @($expectedErpPython, $expectedBasePython)
    $executableAllowed = @($allowedExecutables | Where-Object {
        $_ -and [System.StringComparer]::OrdinalIgnoreCase.Equals($actualExecutable, $_)
    }).Count -gt 0
    if (-not $executableAllowed) {
        throw (
            "端口 $ErpPort 的 PID=$ProcessId 不是当前 ProjectRoot/.venv " +
            "对应的 Python，禁止停止。ExecutablePath=$actualExecutable"
        )
    }
    if (-not (Test-ErpCommandLineIdentity `
        -CommandLine ([string]$processInfo.CommandLine) `
        -ExpectedAppDir $projectRoot
    )) {
        throw "端口 $ErpPort 的 PID=$ProcessId 不符合唯一 ERP 单 worker 启动契约，禁止停止。"
    }
    return $processInfo
}

function Stop-ErpService {
    $connections = Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) {
        Write-Log "ERP 服务未运行，无需停止。"
        return
    }
    $processIds = @(
        $connections | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique
    )
    $validated = @(
        foreach ($processId in $processIds) {
            Get-ValidatedErpProcess -ProcessId $processId
        }
    )
    foreach ($processInfo in $validated) {
        $processId = [int]$processInfo.ProcessId
        Write-Log "停止已验证 ERP 进程 PID=$processId..."
        Stop-Process -Id $processId -Force -ErrorAction Stop
    }
    for ($index = 0; $index -lt 10; $index++) {
        Start-Sleep -Seconds 1
        if (-not (Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue)) {
            break
        }
    }
    if (Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue) {
        throw "端口 $ErpPort 在 10 秒内未释放，禁止继续。"
    }
    Write-Log "ERP 服务已停止。"
}

function Assert-ErpStopped {
    if (Get-NetTCPConnection -LocalPort $ErpPort -State Listen -ErrorAction SilentlyContinue) {
        throw "Apply 阶段要求 ERP 保持停服；端口 $ErpPort 仍在监听。"
    }
}

function Initialize-ReleaseRuntime {
    if (-not (Test-Path -LiteralPath $expectedErpPython -PathType Leaf)) {
        throw "当前 ProjectRoot 的 .venv Python 不存在，禁止回退到全局 Python。"
    }
    $script:python = [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $expectedErpPython -ErrorAction Stop).ProviderPath
    )
    if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals(
        $script:python,
        $expectedErpPython
    )) {
        throw "解析后的 Python 路径不是当前 ProjectRoot/.venv，禁止发布。"
    }
    $basePythonOutput = @(
        & $script:python -X utf8 -c "import sys; print(sys._base_executable)" 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $basePythonOutput.Count -lt 1) {
        throw "无法确认当前 .venv 对应的基础 Python，禁止发布。"
    }
    $script:expectedBasePython = [System.IO.Path]::GetFullPath(
        $basePythonOutput[-1].ToString().Trim()
    )
    if (-not (Test-Path -LiteralPath $releaseHelper -PathType Leaf)) {
        throw "发布门禁脚本不存在：$releaseHelper"
    }
    New-Item -ItemType Directory -Path $logDir, $backupDir, $rehearsalDir, $reportDir, $releaseStateDir -Force | Out-Null

    $runtimeConfig = @(
        & $script:python -X utf8 -c "from app.core.config import load_settings; s=load_settings(); print(s.port); print(s.workers); print(s.environment); print(s.production_transport); print(s.health_url); print(s.browser_url); print(s.bind_host); print(s.database_path)" 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $runtimeConfig.Count -lt 8) {
        $runtimeConfig | ForEach-Object { Write-Log "config: $_" "ERROR" }
        throw "ERP production runtime configuration is invalid."
    }
    $script:ErpPort = [int]$runtimeConfig[-8].ToString().Trim()
    $workers = [int]$runtimeConfig[-7].ToString().Trim()
    $environment = $runtimeConfig[-6].ToString().Trim()
    $productionTransport = $runtimeConfig[-5].ToString().Trim()
    $healthUrl = $runtimeConfig[-4].ToString().Trim()
    $browserUrl = $runtimeConfig[-3].ToString().Trim()
    $bindHost = $runtimeConfig[-2].ToString().Trim()
    $databasePath = [System.IO.Path]::GetFullPath($runtimeConfig[-1].ToString().Trim())

    if ($environment -ne "production") {
        throw "正式发布要求 ERP_ENVIRONMENT=production；当前为 $environment。"
    }
    if ($workers -ne 1) { throw "正式发布要求 ERP_WORKERS=1。" }
    if ($productionTransport -eq "https_proxy") {
        if ($healthUrl -notlike "https://*" -or $browserUrl -notlike "https://*") {
            throw "https_proxy 正式发布要求 HTTPS ERP_HEALTH_URL 与 ERP_BROWSER_URL。"
        }
        if ($bindHost -notin @("127.0.0.1", "::1")) {
            throw "https_proxy 正式发布后端只允许 loopback 监听。"
        }
    } elseif ($productionTransport -eq "lan_http") {
        if ($healthUrl -notlike "http://*" -or $browserUrl -notlike "http://*") {
            throw "lan_http 正式发布要求 HTTP ERP_HEALTH_URL 与 ERP_BROWSER_URL。"
        }
    } else {
        throw "不支持的 ERP_PRODUCTION_TRANSPORT：$productionTransport"
    }
    if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals(
        $databasePath,
        $formalDatabasePath
    )) {
        throw "正式发布只允许数据库：$formalDatabasePath"
    }
}

function Assert-ApprovedCheckout(
    [string]$ApprovedSha,
    [string]$PreviousOfficialSha = ""
) {
    Push-Location $projectRoot
    try {
        $branch = (git branch --show-current).Trim()
        if ($LASTEXITCODE -ne 0 -or $branch -ne "factory-current-baseline") {
            throw "正式发布必须位于 factory-current-baseline，实际为：$branch"
        }
        $actualSha = (git rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $actualSha -ne $ApprovedSha) {
            throw "正式发布 SHA 不匹配：actual=$actualSha expected=$ApprovedSha"
        }
        $dirty = git status --porcelain
        if ($LASTEXITCODE -ne 0 -or $dirty) {
            throw "正式发布工作区必须干净。"
        }
        if (-not [string]::IsNullOrWhiteSpace($PreviousOfficialSha)) {
            $remoteFormalSha = (git rev-parse origin/factory-current-baseline).Trim()
            if ($LASTEXITCODE -ne 0 -or $remoteFormalSha -ne $PreviousOfficialSha) {
                throw (
                    "更新前 SHA 必须是尚未推进的远端正式基线：" +
                    "remote=$remoteFormalSha expected=$PreviousOfficialSha"
                )
            }
        }
    } finally {
        Pop-Location
    }
}

function Invoke-ReleaseHelper([string[]]$Arguments) {
    $output = @(& $python -X utf8 $releaseHelper @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $output | ForEach-Object { Write-Log "gate: $_" "ERROR" }
        throw "发布门禁失败；ERP 必须保持停服。"
    }
    if ($output.Count -lt 1) { throw "发布门禁没有返回 JSON。" }
    return ($output[-1].ToString() | ConvertFrom-Json)
}

if ($LibraryOnly) { return }

try {
    if ($PSCmdlet.ParameterSetName -eq "Help") {
        throw (
            "旧的一键更新已停用。先运行 release_erp.ps1 -Prepare " +
            "-PreviousCodeSha <更新前40位SHA> -ExpectedCodeSha <目标40位SHA> " +
            "-ExpectedRevision <revision>；" +
            "人工核对报告后，再运行 -Apply -PlanPath <报告> -ApprovalToken <口令>。"
        )
    }

    Initialize-ReleaseRuntime
    Write-Log "========== ERP P0-A 发布门禁开始 =========="

    if ($Prepare) {
        Assert-ApprovedCheckout `
            -ApprovedSha $ExpectedCodeSha `
            -PreviousOfficialSha $PreviousCodeSha
        Write-Log "已确认生产配置、正式路径和代码 SHA；准备停服。"
        Stop-ErpService

        $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $reportPath = Join-Path $reportDir ("release_runtime_{0}.json" -f $timestamp)
        $plan = Invoke-ReleaseHelper -Arguments @(
            "prepare",
            "--database", $formalDatabasePath,
            "--backup-dir", $backupDir,
            "--rehearsal-dir", $rehearsalDir,
            "--report", $reportPath,
            "--previous-code-sha", $PreviousCodeSha,
            "--expected-code-sha", $ExpectedCodeSha,
            "--expected-revision", $ExpectedRevision
        )
        Write-Log "备份与隔离迁移演练通过；正式数据库尚未迁移。"
        Write-Log "发布报告：$($plan.report_path)"
        Write-Host ""
        Write-Host "请人工核对 SHA、revision、备份、integrity/FK 和核心表计数。"
        Write-Host "确认后使用以下一次性绑定口令执行 Apply："
        Write-Host $plan.approval_token
        Write-Host "ERP 将保持停服，直到 Apply 成功或人工按回退方案处理。"
        exit 0
    }

    $resolvedPlanPath = [System.IO.Path]::GetFullPath($PlanPath)
    if (-not (Test-Path -LiteralPath $resolvedPlanPath -PathType Leaf)) {
        throw "发布计划不存在：$resolvedPlanPath"
    }
    $planBeforeApply = Get-Content -LiteralPath $resolvedPlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-ApprovedCheckout `
        -ApprovedSha $planBeforeApply.code_sha `
        -PreviousOfficialSha $planBeforeApply.previous_code_sha
    if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals(
        [System.IO.Path]::GetFullPath($planBeforeApply.source.path),
        $formalDatabasePath
    )) {
        throw "发布计划不属于当前正式数据库。"
    }
    Assert-ErpStopped
    $applied = Invoke-ReleaseHelper -Arguments @(
        "apply",
        "--plan", $resolvedPlanPath,
        "--approval-token", $ApprovalToken
    )
    Write-Log "正式数据库迁移与完整性复检通过，准备启动。"
    $startScript = Join-Path $PSScriptRoot "start_erp_background.ps1"
    & $startScript
    if ($LASTEXITCODE -ne 0) {
        throw "数据库已完成迁移，但 ERP 启动失败；保持现场并查看日志。"
    }
    $serviceStartedByRelease = $true
    Invoke-ReleaseHelper -Arguments @(
        "mark-started",
        "--plan", $resolvedPlanPath,
        "--latest-pointer", $latestReleasePointer
    ) | Out-Null
    Write-Log "========== ERP 发布完成，服务健康检查通过 =========="
    Write-Log "发布报告：$resolvedPlanPath"
    Write-Log "最近已完成发布指针：$latestReleasePointer"
    exit 0
} catch {
    Write-Log $_.Exception.Message "ERROR"
    if ($serviceStartedByRelease) {
        try {
            Write-Log "发布完成证据校验失败，重新停止刚启动的 ERP。" "ERROR"
            Stop-ErpService
        } catch {
            Write-Log ("重新停服失败：" + $_.Exception.Message) "ERROR"
        }
    }
    Write-Host "发布流程已停止；不得跳过失败步骤或手工继续迁移。"
    exit 1
}

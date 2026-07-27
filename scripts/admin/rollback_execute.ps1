#Requires -Version 5.1
<#
.SYNOPSIS
    天明 ERP P0-5B 阶段2离线回退执行入口。

.DESCRIPTION
    Prepare 只生成受签名计划、回退前现场备份和一次性口令，不停服。
    Apply 先由 Python 控制器验签并消费授权，再让目标版本在 loopback 端口
    完成预热。只有预热通过才精确停服并切换活动运行指针；正式开放证据形成
    前允许受控恢复，形成后只保留目标现场并转人工处理。
#>

[CmdletBinding(DefaultParameterSetName = "Help")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Prepare")]
    [switch]$Prepare,

    [Parameter(Mandatory = $true, ParameterSetName = "Prepare")]
    [string]$CompatibilityReport,

    [Parameter(ParameterSetName = "Prepare")]
    [string]$ActivePointerPath,

    [Parameter(ParameterSetName = "Prepare")]
    [string]$OutputRoot,

    [Parameter(Mandatory = $true, ParameterSetName = "Apply")]
    [switch]$Apply,

    [Parameter(Mandatory = $true, ParameterSetName = "Apply")]
    [Parameter(ParameterSetName = "Prepare")]
    [string]$PlanPath,

    [Parameter(Mandatory = $true, ParameterSetName = "Apply")]
    [string]$ApprovalToken,

    [Parameter(ParameterSetName = "Apply")]
    [string]$DatabaseApprovalToken,

    [Parameter(ParameterSetName = "Library")]
    [switch]$LibraryOnly,

    [switch]$TechnicalDetails
)

$ErrorActionPreference = "Stop"
$ControlRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ControlPython = Join-Path $ControlRoot ".venv\Scripts\python.exe"
$Controller = Join-Path $PSScriptRoot "rollback_execute.py"
$StartScript = Join-Path $ControlRoot "scripts\windows\start_erp.ps1"
$IdentityLibrary = Join-Path $ControlRoot "scripts\windows\erp_process_identity.ps1"
$ExpectedIdentityLibrarySha256 = (
    "78ea3977c4cd37bfa2cd1d077bd9d2f8d824f3f692a574fdca5fc18dc62f4598"
)
$LogDir = Join-Path $ControlRoot "logs"
$LogFile = Join-Path $LogDir "erp_rollback_execute.log"

function Write-RollbackLog {
    param(
        [string]$Message,
        [string]$Level = "INFO"
    )
    $line = "[{0}] [{1}] {2}" -f (
        Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    ), $Level, $Message
    Write-Host $line
    if (Test-Path -LiteralPath $LogDir) {
        Add-Content -LiteralPath $LogFile -Encoding UTF8 -Value $line
    }
}

function Assert-ErpIdentityLibrary {
    if (-not (Test-Path -LiteralPath $IdentityLibrary -PathType Leaf)) {
        throw "ERP 进程身份公共门禁不存在：$IdentityLibrary"
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
        throw "ERP 进程身份公共门禁哈希不匹配。"
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

function Invoke-RollbackController {
    param(
        [string]$Label,
        [string[]]$Arguments
    )
    $output = @(
        & $ControlPython -I -B -X utf8 $Controller @Arguments 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $output.Count -lt 1) {
        $detail = if ($output.Count -gt 0) {
            $output[-1].ToString()
        } else {
            "$Label 没有返回结果"
        }
        try {
            $payload = $detail | ConvertFrom-Json
            if (-not [string]::IsNullOrWhiteSpace([string]$payload.error)) {
                $detail = [string]$payload.error
            }
        } catch { }
        throw ("{0}失败：{1}" -f $Label, $detail)
    }
    try {
        return ($output[-1].ToString() | ConvertFrom-Json)
    } catch {
        throw "$Label 未返回有效 JSON。"
    }
}

function Get-ValidatedErpProcess {
    param(
        [object]$Service,
        [object]$Runtime,
        [switch]$AllowNotRunning
    )
    return (Get-ValidatedErpProcessIdentity `
        -Port ([int](Get-JsonProperty $Service "port" 0)) `
        -Workers ([int](Get-JsonProperty $Service "workers" 0)) `
        -PythonPath ([string](Get-JsonProperty $Runtime "python_path" "")) `
        -ApplicationRoot ([string](Get-JsonProperty $Runtime "runtime_dir" "")) `
        -BindHost ([string](Get-JsonProperty $Service "bind_host" "")) `
        -AllowNotRunning:$AllowNotRunning)
}

function Stop-ValidatedErpService {
    param(
        [object]$Service,
        [object]$Runtime,
        [switch]$AllowNotRunning
    )
    $processInfo = Get-ValidatedErpProcess `
        -Service $Service `
        -Runtime $Runtime `
        -AllowNotRunning:$AllowNotRunning
    if ($null -eq $processInfo) { return }
    $port = [int](Get-JsonProperty $Service "port" 0)
    $processId = [int]$processInfo.ProcessId
    $processCreation = [string]$processInfo.CreationDate
    Write-RollbackLog "停止已精确核对的 ERP 进程 PID=$processId。"
    try {
        Stop-Process -Id $processId -Force -ErrorAction Stop
    } catch {
        $remaining = Get-CimInstance `
            -ClassName Win32_Process `
            -Filter ("ProcessId = {0}" -f $processId) `
            -ErrorAction SilentlyContinue
        if (
            $remaining -and
            [string]$remaining.CreationDate -eq $processCreation
        ) {
            throw
        }
    }
    for ($index = 0; $index -lt 10; $index++) {
        Start-Sleep -Seconds 1
        $remaining = Get-CimInstance `
            -ClassName Win32_Process `
            -Filter ("ProcessId = {0}" -f $processId) `
            -ErrorAction SilentlyContinue
        $sameProcessRemains = (
            $remaining -and
            [string]$remaining.CreationDate -eq $processCreation
        )
        $listeners = @(
            Get-NetTCPConnection `
            -LocalPort $port `
            -State Listen `
            -ErrorAction SilentlyContinue
        )
        if (-not $sameProcessRemains -and $listeners.Count -eq 0) {
            return
        }
    }
    $remaining = Get-CimInstance `
        -ClassName Win32_Process `
        -Filter ("ProcessId = {0}" -f $processId) `
        -ErrorAction SilentlyContinue
    $sameProcessRemains = (
        $remaining -and
        [string]$remaining.CreationDate -eq $processCreation
    )
    $listeners = @(
        Get-NetTCPConnection `
            -LocalPort $port `
            -State Listen `
            -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -gt 0) {
        $listenerPids = @(
            $listeners |
                Select-Object -ExpandProperty OwningProcess |
                Sort-Object -Unique
        )
        $error = [System.InvalidOperationException]::new(
            (
                "原 ERP PID={0} 停止后端口 {1} 仍被监听；" +
                "当前监听 PID={2}，禁止误停。" -f
                $processId,
                $port,
                ($listenerPids -join ",")
            )
        )
        $error.Data["OriginalProcessStopped"] = (-not $sameProcessRemains)
        $error.Data["OriginalListenerStopped"] = (
            $listenerPids -notcontains $processId
        )
        $error.Data["PortStillListening"] = $true
        $error.Data["OriginalProcessId"] = $processId
        $error.Data["ObservedListenerProcessId"] = [int]$listenerPids[0]
        $error.Data["ListenerProcessIds"] = ($listenerPids -join ",")
        throw $error
    }
    throw "ERP PID=$processId 未在 10 秒内退出，禁止继续。"
}

function Start-And-ValidateRuntime {
    param(
        [object]$Service,
        [object]$Runtime,
        [string]$ValidationTicket
    )
    # start_erp.ps1 intentionally exits with an OS status code.  Run it in a
    # child Windows PowerShell process so its `exit` cannot terminate this
    # evidence/finalization controller.
    $windowsPowerShell = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
        throw "找不到 Windows PowerShell 5.1，无法启动活动运行目录。"
    }
    $launcherArguments = @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", $StartScript,
        "-NoBrowser"
    )
    if (-not [string]::IsNullOrWhiteSpace($ValidationTicket)) {
        $launcherArguments += @(
            "-ValidationTicket",
            [System.IO.Path]::GetFullPath($ValidationTicket)
        )
    }
    & $windowsPowerShell @launcherArguments
    if ($LASTEXITCODE -ne 0) {
        throw "活动运行目录未能健康启动。"
    }
    $healthUrl = [string](
        Get-JsonProperty $Service "local_health_url" ""
    )
    if (-not (Test-ErpHealthContract -Url $healthUrl -TimeoutSeconds 2)) {
        throw "活动运行目录健康响应必须精确为 {`"ok`":true}。"
    }
    Get-ValidatedErpProcess -Service $Service -Runtime $Runtime | Out-Null
}

function Test-PathEntryExists {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $parent = Split-Path $fullPath -Parent
    $leaf = Split-Path $fullPath -Leaf
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        return $false
    }
    return $null -ne (
        Get-ChildItem -LiteralPath $parent -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ceq $leaf } |
            Select-Object -First 1
    )
}

function Test-ExposureBoundary {
    param([object]$TargetRuntime)
    $permitPath = [string](
        Get-JsonProperty $TargetRuntime "auto_restore_permit_path" ""
    )
    $consumedPermitPath = [string](
        Get-JsonProperty `
            $TargetRuntime `
            "auto_restore_permit_consumed_path" `
            ""
    )
    $intentPath = [string](
        Get-JsonProperty `
            $TargetRuntime `
            "production_exposure_intent_path" `
            ""
    )
    $exposurePath = [string](
        Get-JsonProperty $TargetRuntime "production_exposure_path" ""
    )
    $permitPresent = Test-PathEntryExists -Path $permitPath
    $consumedPermitPresent = Test-PathEntryExists `
        -Path $consumedPermitPath

    # commit-exposure atomically consumes the one-time restore permit before
    # it writes the intent file.  The consumed witness, or an unexpectedly
    # missing original permit, is therefore already an irreversible boundary
    # even when neither exposure JSON file could be written.
    return (
        $consumedPermitPresent -or
        (-not $permitPresent) -or
        (Test-PathEntryExists -Path $intentPath) -or
        (Test-PathEntryExists -Path $exposurePath)
    )
}

function Get-FormalPrivateFileManifest {
    $dataRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $ControlRoot "data")
    )
    $privateRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $dataRoot "private_uploads")
    )
    $secretPath = [System.IO.Path]::GetFullPath(
        (Join-Path $dataRoot "session_secret.key")
    )
    $entries = New-Object 'System.Collections.Generic.List[object]'

    if (Test-Path -LiteralPath $privateRoot -PathType Container) {
        $privateItems = @(
            Get-ChildItem `
                -LiteralPath $privateRoot `
                -Force `
                -Recurse `
                -ErrorAction Stop |
                Sort-Object -Property FullName
        )
        foreach ($item in $privateItems) {
            $relativePath = $item.FullName.Substring(
                $privateRoot.Length
            ).TrimStart(
                [System.IO.Path]::DirectorySeparatorChar,
                [System.IO.Path]::AltDirectorySeparatorChar
            )
            if ($item.PSIsContainer) {
                $entries.Add([ordered]@{
                    kind = "directory"
                    relative_path = $relativePath
                })
            } else {
                $entries.Add([ordered]@{
                    kind = "file"
                    relative_path = $relativePath
                    length = [long]$item.Length
                    last_write_utc_ticks = [long](
                        $item.LastWriteTimeUtc.Ticks
                    )
                    sha256 = (
                        Get-FileHash `
                            -LiteralPath $item.FullName `
                            -Algorithm SHA256
                    ).Hash.ToLowerInvariant()
                })
            }
        }
    } else {
        $entries.Add([ordered]@{
            kind = "private_root_absent"
            relative_path = ""
        })
    }

    $secret = if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
        $secretInfo = Get-Item -LiteralPath $secretPath -Force
        [ordered]@{
            present = $true
            length = [long]$secretInfo.Length
            last_write_utc_ticks = [long]$secretInfo.LastWriteTimeUtc.Ticks
            sha256 = (
                Get-FileHash `
                    -LiteralPath $secretPath `
                    -Algorithm SHA256
            ).Hash.ToLowerInvariant()
        }
    } else {
        [ordered]@{ present = $false }
    }
    return ([ordered]@{
        private_root = $privateRoot
        entries = $entries.ToArray()
        session_secret = $secret
    } | ConvertTo-Json -Depth 6 -Compress)
}

function Assert-FormalPrivateFilesUnchanged {
    param([string]$BeforeManifest)
    if ([string]::IsNullOrWhiteSpace($BeforeManifest)) {
        throw "缺少 loopback 预热前正式私有文件清单。"
    }
    $afterManifest = Get-FormalPrivateFileManifest
    if (-not [System.StringComparer]::Ordinal.Equals(
        $BeforeManifest,
        $afterManifest
    )) {
        throw (
            "loopback 预热期间正式 private_uploads 或 session secret 已变化；" +
            "禁止停服和切换。"
        )
    }
}

function Finalize-ManualTargetRetained {
    param(
        [string]$TicketPath,
        [string]$ErrorText
    )
    try {
        return (Invoke-RollbackController `
            -Label "正式开放后人工恢复现场固化" `
            -Arguments @(
                "finalize",
                "--ticket", $TicketPath,
                "--status", "manual_target_retained",
                "--error", $ErrorText
            ))
    } catch {
        Write-RollbackLog (
            "目标指针和候选库仍已保留，但最终证据固化失败：{0}" -f
            $_.Exception.Message
        ) "ERROR"
        return $null
    }
}

function Try-FinalizeManualStop {
    param(
        [string]$TicketPath,
        [string]$ErrorText
    )
    try {
        Invoke-RollbackController `
            -Label "双重失败结果固化" `
            -Arguments @(
                "finalize",
                "--ticket", $TicketPath,
                "--status", "stopped_manual_recovery_required",
                "--error", $ErrorText
            ) | Out-Null
    } catch {
        Write-RollbackLog (
            "无法固化双重失败结果，现场证据仍保留：{0}" -f
            $_.Exception.Message
        ) "ERROR"
    }
}

function Finalize-StoppedBeforeActivation {
    param(
        [string]$TicketPath,
        [string]$ErrorText,
        [int]$StoppedProcessId,
        [int]$ObservedListenerProcessId
    )
    return (Invoke-RollbackController `
        -Label "正式停服后端口碰撞结果固化" `
        -Arguments @(
            "finalize",
            "--ticket", $TicketPath,
            "--status", "stopped_before_activation_manual_recovery_required",
            "--error", $ErrorText,
            "--stopped-process-id", $StoppedProcessId.ToString(),
            "--observed-listener-process-id",
            $ObservedListenerProcessId.ToString()
        ))
}

if ($LibraryOnly) { return }

try {
    foreach ($requiredFile in @($ControlPython, $Controller, $StartScript)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "阶段2执行文件不存在：$requiredFile"
        }
    }
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

    if ($PSCmdlet.ParameterSetName -eq "Help") {
        throw (
            "先运行 rollback_execute.ps1 -Prepare -CompatibilityReport <阶段1报告>；" +
            "人工核对备份和数据边界后，再运行 -Apply -PlanPath <计划> " +
            "-ApprovalToken <一次口令>。完整回退还必须提供 -DatabaseApprovalToken。"
        )
    }

    if ($Prepare) {
        $resolvedReport = [System.IO.Path]::GetFullPath($CompatibilityReport)
        if ([string]::IsNullOrWhiteSpace($ActivePointerPath)) {
            $ActivePointerPath = Join-Path `
                $ControlRoot `
                "data\release_state\active_runtime.json"
        }
        $resolvedPointer = [System.IO.Path]::GetFullPath($ActivePointerPath)
        if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
            $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
            $runtimeBase = Join-Path `
                (Split-Path $ControlRoot -Parent) `
                "tm-rollback-runtimes"
            $OutputRoot = Join-Path $runtimeBase ("switch_{0}" -f $timestamp)
        }
        $resolvedOutput = [System.IO.Path]::GetFullPath($OutputRoot)
        if ([string]::IsNullOrWhiteSpace($PlanPath)) {
            $PlanPath = Join-Path $resolvedOutput "rollback_switch_plan.json"
        }
        $resolvedPlan = [System.IO.Path]::GetFullPath($PlanPath)

        Write-RollbackLog "开始 Prepare：只生成证据与 SQLite 副本，不停服。"
        $prepared = Invoke-RollbackController `
            -Label "阶段2 Prepare" `
            -Arguments @(
                "prepare",
                "--compatibility-report", $resolvedReport,
                "--active-pointer", $resolvedPointer,
                "--output-root", $resolvedOutput,
                "--plan", $resolvedPlan
            )
        Write-Host ""
        Write-Host "========== 天明 ERP 回退准备完成 =========="
        Write-Host ("模式：{0}" -f [string]$prepared.mode_label)
        Write-Host ("签名计划：{0}" -f [string]$prepared.plan_path)
        Write-Host (
            "回退前现场备份：{0}" -f
            [string](Get-JsonProperty $prepared.rollback_site_backup "path" "")
        )
        if ($null -ne $prepared.data_loss_window) {
            Write-Host ""
            Write-Host "完整数据库回退可能丢失以下时间段的新业务数据：" `
                -ForegroundColor Yellow
            Write-Host (
                "{0} 至 {1}" -f
                [string]$prepared.data_loss_window.from,
                [string]$prepared.data_loss_window.to
            ) -ForegroundColor Yellow
        }
        Write-Host ""
        Write-Host "负责人一次授权口令（只显示本次，请勿写入文档）："
        Write-Host ([string]$prepared.approval_token)
        if ([bool]$prepared.requires_database_approval) {
            Write-Host ""
            Write-Host "完整数据库回退第二授权口令（只显示本次）："
            Write-Host ([string]$prepared.database_approval_token)
        }
        Write-Host ""
        Write-Host "ERP 未停止，活动运行目录未切换，正式数据库未覆盖。"
        exit 0
    }

    $resolvedPlan = [System.IO.Path]::GetFullPath($PlanPath)
    $authorizeArguments = @(
        "authorize",
        "--plan", $resolvedPlan,
        "--approval-token", $ApprovalToken
    )
    if (-not [string]::IsNullOrWhiteSpace($DatabaseApprovalToken)) {
        $authorizeArguments += @(
            "--database-approval-token",
            $DatabaseApprovalToken
        )
    }

    # Token verification, expiry, evidence drift and database checks all happen
    # before we inspect or stop the service.  A bad token therefore cannot stop
    # ERP.
    Write-RollbackLog "正在停服前验签并消费一次授权。"
    $authorized = Invoke-RollbackController `
        -Label "阶段2授权" `
        -Arguments $authorizeArguments
    $ticketPath = [System.IO.Path]::GetFullPath(
        [string]$authorized.ticket_path
    )
    $service = $authorized.service
    $validationService = $authorized.validation_service
    $currentRuntime = $authorized.current_runtime
    $targetRuntime = $authorized.target_runtime

    $validationStopped = $false
    $formalPrivateBefore = $null
    $formalPrivateVerified = $false
    $currentWasStopped = $false
    $formalStopCollisionError = ""
    $formalStoppedProcessId = 0
    $formalObservedListenerProcessId = 0
    $pointerActivated = $false
    $exposureBoundary = $false
    try {
        # Authorization never stops production.  The old target must first
        # prove it can boot on its signed loopback-only maintenance port while
        # the current formal ERP remains available.
        Get-ValidatedErpProcess `
            -Service $service `
            -Runtime $currentRuntime | Out-Null
        $formalPrivateBefore = Get-FormalPrivateFileManifest
        Start-And-ValidateRuntime `
            -Service $validationService `
            -Runtime $targetRuntime `
            -ValidationTicket $ticketPath
        Invoke-RollbackController `
            -Label "loopback 目标预热复核" `
            -Arguments @(
                "verify-loopback",
                "--ticket", $ticketPath
            ) | Out-Null
        Stop-ValidatedErpService `
            -Service $validationService `
            -Runtime $targetRuntime
        $validationStopped = $true
        Assert-FormalPrivateFilesUnchanged `
            -BeforeManifest $formalPrivateBefore
        $formalPrivateVerified = $true

        # Re-check the formal listener after warmup.  Unknown or additional
        # listeners are never stopped.
        Get-ValidatedErpProcess `
            -Service $service `
            -Runtime $currentRuntime | Out-Null
        try {
            Stop-ValidatedErpService `
                -Service $service `
                -Runtime $currentRuntime
            $currentWasStopped = $true
        } catch {
            if (
                [bool]$_.Exception.Data["OriginalProcessStopped"] -or
                [bool]$_.Exception.Data["OriginalListenerStopped"]
            ) {
                $currentWasStopped = $true
            }
            if ([bool]$_.Exception.Data["OriginalProcessStopped"]) {
                $formalStopCollisionError = $_.Exception.Message
                $formalStoppedProcessId = [int](
                    $_.Exception.Data["OriginalProcessId"]
                )
                $formalObservedListenerProcessId = [int](
                    $_.Exception.Data["ObservedListenerProcessId"]
                )
            }
            throw
        }

        Invoke-RollbackController `
            -Label "活动运行目录切换" `
            -Arguments @("activate", "--ticket", $ticketPath) | Out-Null
        $pointerActivated = $true

        # The Python command consumes the one-time auto-restore permit first,
        # then writes intent and durable exposure records.  From the instant
        # the permit is consumed/missing or either exposure path appears,
        # automatic revoke or restore is permanently forbidden.
        try {
            Invoke-RollbackController `
                -Label "正式网络开放承诺" `
                -Arguments @(
                    "commit-exposure",
                    "--ticket", $ticketPath
                ) | Out-Null
        } finally {
            $exposureBoundary = Test-ExposureBoundary `
                -TargetRuntime $targetRuntime
        }
        if (-not $exposureBoundary) {
            throw "正式网络开放承诺未形成持久证据。"
        }

        Start-And-ValidateRuntime `
            -Service $service `
            -Runtime $targetRuntime
        Invoke-RollbackController `
            -Label "目标运行目录健康复核" `
            -Arguments @("verify-active", "--ticket", $ticketPath) | Out-Null
        $completed = Invoke-RollbackController `
            -Label "切换结果固化" `
            -Arguments @(
                "finalize",
                "--ticket", $ticketPath,
                "--status", "switch_completed"
            )
        Write-RollbackLog "旧版本活动运行目录切换成功。"
        if ($TechnicalDetails) {
            Write-Host ("执行票据：{0}" -f $ticketPath)
            Write-Host ("结果证据：{0}" -f [string]$completed.result_path)
        }
        exit 0
    } catch {
        $targetError = $_.Exception.Message
        $exposureBoundary = (
            $exposureBoundary -or
            (Test-ExposureBoundary -TargetRuntime $targetRuntime)
        )
        if ($exposureBoundary) {
            Write-RollbackLog (
                "自动恢复许可已消费/缺失或正式开放证据已落盘，" +
                "禁止 revoke/restore；" +
                "保留目标指针和候选数据库：{0}" -f
                $targetError
            ) "ERROR"
            $manual = Finalize-ManualTargetRetained `
                -TicketPath $ticketPath `
                -ErrorText $targetError
            if ($TechnicalDetails -and $manual) {
                Write-Host ("执行票据：{0}" -f $ticketPath)
                Write-Host ("结果证据：{0}" -f [string]$manual.result_path)
            }
            throw (
                "不可逆开放边界后失败；目标指针和候选数据库已保留，" +
                "禁止自动恢复，请按证据人工处理。"
            )
        }

        Write-RollbackLog (
            "尚未进入正式开放边界，执行受控清理/恢复：{0}" -f
            $targetError
        ) "ERROR"

        if (-not $validationStopped) {
            try {
                Stop-ValidatedErpService `
                    -Service $validationService `
                    -Runtime $targetRuntime `
                    -AllowNotRunning
                $validationStopped = $true
            } catch {
                throw (
                    "loopback 监听身份异常，禁止误停；当前正式 ERP 未被本步骤停止。" +
                    $_.Exception.Message
                )
            }
        }
        if (
            $validationStopped -and
            -not $formalPrivateVerified -and
            -not [string]::IsNullOrWhiteSpace($formalPrivateBefore)
        ) {
            Assert-FormalPrivateFilesUnchanged `
                -BeforeManifest $formalPrivateBefore
            $formalPrivateVerified = $true
        }

        if (-not [string]::IsNullOrWhiteSpace($formalStopCollisionError)) {
            $manualStop = Finalize-StoppedBeforeActivation `
                -TicketPath $ticketPath `
                -ErrorText $formalStopCollisionError `
                -StoppedProcessId $formalStoppedProcessId `
                -ObservedListenerProcessId $formalObservedListenerProcessId
            if ($TechnicalDetails) {
                Write-Host (
                    "结果证据：{0}" -f [string]$manualStop.result_path
                )
            }
            throw (
                "原正式 ERP 已停止，但正式端口被未知监听占用；" +
                "未误停新监听，已转人工恢复。"
            )
        }

        if (-not $currentWasStopped) {
            throw "loopback 预热阶段已阻断；当前正式 ERP 保持运行。$targetError"
        }

        if ($pointerActivated) {
            try {
                Stop-ValidatedErpService `
                    -Service $service `
                    -Runtime $targetRuntime `
                    -AllowNotRunning
            } catch {
                $unknownListenerError = $_.Exception.Message
                Invoke-RollbackController `
                    -Label "未知监听下撤销目标指针" `
                    -Arguments @(
                        "revoke",
                        "--ticket", $ticketPath,
                        "--reason", $unknownListenerError
                    ) | Out-Null
                Invoke-RollbackController `
                    -Label "未知监听人工恢复结果固化" `
                    -Arguments @(
                        "finalize",
                        "--ticket", $ticketPath,
                        "--status", "manual_recovery_required",
                        "--error", $unknownListenerError
                    ) | Out-Null
                throw (
                    "正式端口出现未知监听，未误停该进程；目标指针已撤销并保留，" +
                    "系统等待人工恢复。"
                )
            }
            Invoke-RollbackController `
                -Label "恢复切换前活动运行指针" `
                -Arguments @(
                    "restore",
                    "--ticket", $ticketPath
                ) | Out-Null
        }

        try {
            Start-And-ValidateRuntime `
                -Service $service `
                -Runtime $currentRuntime
            if ($pointerActivated) {
                $restored = Invoke-RollbackController `
                    -Label "当前版本恢复结果固化" `
                    -Arguments @(
                        "finalize",
                        "--ticket", $ticketPath,
                        "--status", "rolled_back_to_current_runtime",
                        "--error", $targetError
                    )
                if ($TechnicalDetails) {
                    Write-Host ("结果证据：{0}" -f [string]$restored.result_path)
                }
            }
            Write-RollbackLog "正式开放前失败；当前版本已恢复运行。" "ERROR"
            exit 0
        } catch {
            $recoveryError = $_.Exception.Message
            try {
                Stop-ValidatedErpService `
                    -Service $service `
                    -Runtime $currentRuntime `
                    -AllowNotRunning
            } catch {
                $recoveryError = (
                    $recoveryError + "；恢复进程停止失败：" +
                    $_.Exception.Message
                )
            }
            if ($pointerActivated) {
                Try-FinalizeManualStop `
                    -TicketPath $ticketPath `
                    -ErrorText (
                        "正式开放前目标失败：{0}；当前版本恢复失败：{1}" -f
                        $targetError,
                        $recoveryError
                    )
            }
            throw (
                "当前版本自动恢复失败，系统保持停机；" +
                "请按证据目录人工恢复。$recoveryError"
            )
        }
    }
} catch {
    Write-RollbackLog $_.Exception.Message "ERROR"
    Write-Host "阶段2流程已安全停止；未执行 Git 切换、迁移或数据库覆盖。"
    exit 1
}

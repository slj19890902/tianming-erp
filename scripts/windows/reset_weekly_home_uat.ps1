#Requires -Version 5.1
param(
    [string]$RuntimeFile,

    [string]$UatRoot = "D:\tm-weekly-uat",

    [ValidateRange(18000, 19999)]
    [int]$Port = 18200,

    [string]$PythonPath = "D:\纸箱厂erp软件搭建\.venv\Scripts\python.exe",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

function Get-FullPath([string]$PathValue) {
    return [System.IO.Path]::GetFullPath($PathValue)
}

function Test-PathWithin([string]$Candidate, [string]$Root) {
    $candidatePath = Get-FullPath $Candidate
    $rootPath = (Get-FullPath $Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    return $candidatePath.StartsWith(
        $rootPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NoReparseComponents([string]$PathValue, [string]$StopRoot) {
    $fullPath = Get-FullPath $PathValue
    $boundary = Get-FullPath $StopRoot
    if (Test-Path -LiteralPath $fullPath) {
        $item = Get-Item -LiteralPath $fullPath -Force
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "UAT 所有权路径包含重解析点，拒绝清理：$($item.FullName)"
        }
        if ($item.PSIsContainer) {
            $cursor = [System.IO.DirectoryInfo]$item
        } else {
            $cursor = $item.Directory
        }
    } else {
        $cursor = [System.IO.DirectoryInfo]([System.IO.Path]::GetDirectoryName($fullPath))
    }
    while ($null -ne $cursor) {
        if ($cursor.Exists -and
            ($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "UAT 所有权路径包含重解析点，拒绝清理：$($cursor.FullName)"
        }
        if ($cursor.FullName -ieq $boundary) {
            return
        }
        $cursor = $cursor.Parent
    }
    throw "UAT 所有权路径无法回溯到隔离根，拒绝清理：$PathValue"
}

function Test-UatProcessCommandLine(
    [string]$CommandLine,
    [string]$ExpectedProjectRoot,
    [int]$ExpectedPort
) {
    if ([string]::IsNullOrWhiteSpace($CommandLine) -or
        $CommandLine -notmatch "(?i)(?:^|\s)(?:-m\s+)?uvicorn(?:\.exe)?(?:\s|$)" -and
        $CommandLine -notmatch "(?i)from\s+uvicorn\.main\s+import\s+main") {
        return $false
    }
    $projectPattern = [Regex]::Escape((Get-FullPath $ExpectedProjectRoot))
    $portPattern = (
        '(?i)(?:^|\s)"?--port"?(?:\s+|=)"?' +
        [Regex]::Escape($ExpectedPort.ToString()) +
        '"?(?:\s|$)'
    )
    return $CommandLine -match $projectPattern -and $CommandLine -match $portPattern
}

function Read-OwnershipJson([string]$PathValue, [string]$Label) {
    try {
        $raw = Get-Content -LiteralPath $PathValue -Raw -Encoding UTF8
        $record = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
    } catch {
        throw "$Label 不是有效 JSON，拒绝清理：$PathValue"
    }
    if ($null -eq $record -or $record -is [System.Array]) {
        throw "$Label 必须是 JSON 对象，拒绝清理：$PathValue"
    }
    return $record
}

function Assert-OwnedRecordNonce(
    [string]$PathValue,
    [string]$ExpectedNonce,
    [string]$Label
) {
    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) {
        return
    }
    $record = Read-OwnershipJson -PathValue $PathValue -Label $Label
    if ([string]::IsNullOrWhiteSpace([string]$record.nonce) -or
        [string]$record.nonce -cne $ExpectedNonce) {
        throw "$Label nonce 与 PID 所有权不匹配，拒绝清理：$PathValue"
    }
}

try {
    if ([Environment]::MachineName -ieq "PC-20250926DZYH") {
        throw "家庭 UAT 重置入口禁止在工厂正式主机运行。"
    }
    if ([string]::IsNullOrWhiteSpace($RuntimeFile)) {
        $latestRuntime = Get-ChildItem `
            -LiteralPath (Join-Path $UatRoot "runs") `
            -Filter "runtime.json" `
            -File `
            -Recurse `
            -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if (-not $latestRuntime) {
            throw "没有找到可重置的每周家庭 UAT，请先导入一次工厂数据包。"
        }
        $RuntimeFile = $latestRuntime.FullName
    }
    $runtimePath = (Resolve-Path -LiteralPath $RuntimeFile).Path
    $runtime = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $projectRoot = Get-FullPath ([string]$runtime.project_root)
    $database = Get-FullPath ([string]$runtime.working_database)
    $resolvedUatRoot = Get-FullPath $UatRoot
    $runRoot = Get-FullPath ([System.IO.Path]::GetDirectoryName($database))
    $python = Get-FullPath $PythonPath
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "家庭重置必须使用项目 Python：$python"
    }
    $helper = Join-Path $projectRoot "scripts\admin\weekly_home_uat.py"
    $launcher = Join-Path $projectRoot "scripts\windows\start_erp_uat.ps1"
    # New JSON ownership records and legacy numeric records intentionally share
    # this run-root path; their content determines which compatibility path runs.
    $pidFile = Join-Path $runRoot ("erp_uat_{0}.pid" -f $Port)

    if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
        Assert-NoReparseComponents -PathValue $pidFile -StopRoot $runRoot
        $pidText = (Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8).Trim()
        $pidValue = 0
        $ownership = $null
        $structuredOwnership = $false
        if ($pidText -match "^\d+$") {
            if (-not [int]::TryParse($pidText, [ref]$pidValue) -or $pidValue -le 0) {
                throw "旧版 UAT PID 文件数值无效，拒绝停止未知进程。"
            }
        } else {
            $ownership = Read-OwnershipJson -PathValue $pidFile -Label "UAT PID 所有权文件"
            $requiredFields = @(
                "nonce", "pid", "process_creation_token", "port", "isolation_id",
                "lease_path", "attestation_path"
            )
            foreach ($field in $requiredFields) {
                if ($ownership.PSObject.Properties.Name -notcontains $field) {
                    throw "UAT PID 所有权文件缺少字段 $field，拒绝停止未知进程。"
                }
            }
            if (-not [int]::TryParse([string]$ownership.pid, [ref]$pidValue) -or $pidValue -le 0) {
                throw "UAT PID 所有权文件 pid 无效，拒绝停止未知进程。"
            }
            $recordPort = 0
            if (-not [int]::TryParse([string]$ownership.port, [ref]$recordPort) -or
                $recordPort -ne $Port) {
                throw "UAT PID 所有权文件端口不匹配，拒绝停止未知进程。"
            }
            $nonce = [string]$ownership.nonce
            $isolationId = [string]$ownership.isolation_id
            $processCreationToken = [string]$ownership.process_creation_token
            if ($nonce -notmatch "^[0-9A-Fa-f]{16,}$" -or
                $isolationId -notmatch "^[0-9A-Fa-f]{12}$" -or
                $processCreationToken -notmatch "^[0-9A-Fa-f]{16}$") {
                throw "UAT PID 所有权 nonce/isolation_id/process token 无效，拒绝停止未知进程。"
            }
            $ownershipRoot = Join-Path (Join-Path $runRoot ".erp-uat") $isolationId
            $leasePath = Get-FullPath ([string]$ownership.lease_path)
            $attestationPath = Get-FullPath ([string]$ownership.attestation_path)
            foreach ($ownedPath in @($leasePath, $attestationPath)) {
                if (-not (Test-PathWithin -Candidate $ownedPath -Root $ownershipRoot)) {
                    throw "UAT 所有权记录指向隔离目录之外，拒绝清理：$ownedPath"
                }
                Assert-NoReparseComponents -PathValue $ownedPath -StopRoot $runRoot
            }
            $expectedLeasePath = Join-Path $ownershipRoot ("startup_{0}.lease.json" -f $Port)
            $expectedAttestationPath = Join-Path (
                Join-Path $ownershipRoot "attestations"
            ) ("startup_{0}_{1}.json" -f $Port, $nonce)
            if ($leasePath -ine (Get-FullPath $expectedLeasePath) -or
                $attestationPath -ine (Get-FullPath $expectedAttestationPath)) {
                throw "UAT 所有权记录路径与 isolation_id/port/nonce 不匹配，拒绝清理。"
            }
            Assert-OwnedRecordNonce -PathValue $leasePath -ExpectedNonce $nonce -Label "UAT lease"
            Assert-OwnedRecordNonce -PathValue $attestationPath -ExpectedNonce $nonce -Label "UAT attestation"
            $structuredOwnership = $true
        }

        $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $pidValue) -ErrorAction Stop
        if ($structuredOwnership) {
            $helperPython = Join-Path $projectRoot "app\core\uat_isolation.py"
            $cleanupOutput = @(& $python -I $helperPython cleanup-owned `
                --lease $leasePath `
                --pid $pidFile `
                --attestation $attestationPath `
                --nonce $nonce `
                --terminate 2>&1)
            if ($LASTEXITCODE -ne 0) {
                throw "UAT 所有权 helper 清理失败：$($cleanupOutput -join '`n')"
            }
            $process = $null
        } elseif ($process) {
            if (-not (Test-UatProcessCommandLine `
                -CommandLine ([string]$process.CommandLine) `
                -ExpectedProjectRoot $projectRoot `
                -ExpectedPort $Port)) {
                throw "PID 对应进程不是本周家庭 UAT，拒绝停止。"
            }
            Stop-Process -Id $pidValue -Force
            Wait-Process -Id $pidValue -Timeout 5 -ErrorAction SilentlyContinue
            if (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $pidValue) -ErrorAction Stop) {
                throw "UAT 进程未成功停止，拒绝清理所有权文件。"
            }
            Remove-Item -LiteralPath $pidFile -Force
        } else {
            Remove-Item -LiteralPath $pidFile -Force
        }
    }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw "端口 $Port 仍被占用，拒绝重置数据库。"
    }

    $resetText = & $python -X utf8 $helper reset `
        --runtime-file $runtimePath `
        --uat-root $UatRoot `
        --confirm-reset
    if ($LASTEXITCODE -ne 0) { throw "家庭 UAT 重置失败：$resetText" }

    & $launcher `
        -DatabasePath $database `
        -UatRoot $UatRoot `
        -Port $Port `
        -PythonPath $python `
        -NoBrowser:$NoBrowser
    if ($LASTEXITCODE -ne 0) { throw "重置后重新启动家庭副 ERP 失败。" }
    Write-Host "本周家庭 UAT 已恢复到工厂只读快照并重新启动。"
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}

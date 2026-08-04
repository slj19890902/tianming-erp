#Requires -Version 5.1
param(
    [string]$PackageDir,

    [string]$RepositoryRoot = "D:\纸箱厂erp软件搭建",
    [string]$UatRoot = "D:\tm-weekly-uat",
    [string]$WorktreeRoot = "D:\tm-worktrees",

    [ValidateRange(18000, 19999)]
    [int]$Port = 18200,

    [string]$PythonPath,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

function Assert-ManifestChecksum {
    param([string]$Directory)
    $manifestPath = Join-Path $Directory "manifest.json"
    $checksumPath = Join-Path $Directory "manifest.sha256"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
        throw "UAT 包缺少 manifest.json 或 manifest.sha256。"
    }
    $expected = (Get-Content -LiteralPath $checksumPath -Raw -Encoding ASCII).Trim().ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expected -ne $actual) {
        throw "UAT manifest SHA-256 校验失败。"
    }
    return $manifestPath
}

try {
    if ($env:COMPUTERNAME -ieq "PC-20250926DZYH") {
        throw "家庭导入与启动入口禁止在工厂正式主机运行。"
    }
    if ([string]::IsNullOrWhiteSpace($PackageDir)) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = "请选择完整的工厂每周家庭 UAT 包目录"
        $dialog.ShowNewFolderButton = $false
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            throw "未选择 UAT 包目录。"
        }
        $PackageDir = $dialog.SelectedPath
    }
    $package = (Resolve-Path -LiteralPath $PackageDir).Path
    $repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
    $manifestPath = Assert-ManifestChecksum -Directory $package
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.package_kind -ne "tianming-erp-weekly-home-uat") {
        throw "这不是天明 ERP 每周家庭 UAT 包。"
    }
    $gitSha = [string]$manifest.source.git_sha
    if ($gitSha -notmatch "^[0-9a-f]{40}$") {
        throw "manifest 中的 Git SHA 非法。"
    }
    $packageId = [string]$manifest.package_id
    if ($packageId -notmatch "^[A-Za-z0-9_-]+$") {
        throw "manifest 中的 package_id 非法。"
    }
    $python = if ($PythonPath) {
        [System.IO.Path]::GetFullPath($PythonPath)
    } else {
        Join-Path $repository ".venv\Scripts\python.exe"
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "家庭导入必须使用项目 Python：$python"
    }

    & git -C $repository fetch --prune origin
    if ($LASTEXITCODE -ne 0) { throw "Git fetch 失败，拒绝猜测工厂代码版本。" }
    & git -C $repository cat-file -e "$gitSha`^{commit}"
    if ($LASTEXITCODE -ne 0) { throw "工厂数据包对应的代码 SHA 不存在：$gitSha" }
    & git -C $repository merge-base --is-ancestor $gitSha origin/factory-current-baseline
    if ($LASTEXITCODE -ne 0) {
        throw "数据包代码 SHA 不属于远端正式基线，拒绝启动。"
    }

    New-Item -ItemType Directory -Path $WorktreeRoot -Force | Out-Null
    $worktree = Join-Path ([System.IO.Path]::GetFullPath($WorktreeRoot)) ("erp-weekly-uat-" + $packageId)
    if (Test-Path -LiteralPath $worktree) {
        $existingSha = (& git -C $worktree rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $existingSha -ne $gitSha) {
            throw "既有每周 UAT 工作树 SHA 不一致，拒绝覆盖：$worktree"
        }
    } else {
        & git -C $repository worktree add --detach $worktree $gitSha
        if ($LASTEXITCODE -ne 0) { throw "创建每周 UAT 独立工作树失败。" }
    }

    $helper = Join-Path $worktree "scripts\admin\weekly_home_uat.py"
    $launcher = Join-Path $worktree "scripts\windows\start_erp_uat.ps1"
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf) -or
        -not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "目标正式版本缺少每周家庭 UAT 工具。"
    }
    $importText = & $python -X utf8 $helper import `
        --package-dir $package `
        --uat-root $UatRoot `
        --project-root $worktree
    $importExitCode = $LASTEXITCODE
    $importDetails = ($importText | Out-String).Trim()
    if ($importExitCode -ne 0 -and
        $importDetails -match "WinError 1005|此卷不包含可识别的文件系统") {
        Write-Host "检测到 NAS 映射盘路径兼容问题，正在复制到家庭本地临时目录后重新验签。"
        $stagingRoot = Join-Path `
            ([System.IO.Path]::GetFullPath($UatRoot)) `
            (".nas-package-" + $packageId + "-" + [Guid]::NewGuid().ToString("N"))
        try {
            New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
            Copy-Item -LiteralPath $package -Destination $stagingRoot -Recurse
            $localPackage = Join-Path $stagingRoot $packageId
            Assert-ManifestChecksum -Directory $localPackage | Out-Null
            $importText = & $python -X utf8 $helper import `
                --package-dir $localPackage `
                --uat-root $UatRoot `
                --project-root $worktree
            if ($LASTEXITCODE -ne 0) {
                throw "家庭 UAT 包本地暂存后仍导入失败：$importText"
            }
        } finally {
            if (Test-Path -LiteralPath $stagingRoot) {
                Remove-Item -LiteralPath $stagingRoot -Recurse -Force
            }
        }
    } elseif ($importExitCode -ne 0) {
        throw "家庭 UAT 包导入失败：$importText"
    }
    $importResult = $importText | ConvertFrom-Json

    & $launcher `
        -DatabasePath $importResult.working_database `
        -UatRoot $UatRoot `
        -Port $Port `
        -PythonPath $python `
        -NoBrowser:$NoBrowser
    if ($LASTEXITCODE -ne 0) { throw "家庭副 ERP 启动失败。" }

    Write-Host "家庭副 ERP：http://127.0.0.1:$Port/"
    Write-Host "工厂代码 SHA：$gitSha"
    Write-Host "只读收到件：$($importResult.received_database)"
    Write-Host "可写工作副本：$($importResult.working_database)"
    Write-Host "重置凭据：$($importResult.runtime_file)"
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}

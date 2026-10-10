param(
    [string]$Template,
    [string]$PhotosDir,
    [string]$OutputDir,
    [string]$Mapping
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not $Template) {
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "选择已登记样品信息的 ERP Excel"
    $dialog.Filter = "Excel 工作簿 (*.xlsx)|*.xlsx"
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        exit 0
    }
    $Template = $dialog.FileName
}

if (-not $PhotosDir) {
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "选择整批原始照片所在文件夹"
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        exit 0
    }
    $PhotosDir = $dialog.SelectedPath
}

if (-not $OutputDir) {
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "选择 ERP 图片导入包输出文件夹"
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        exit 0
    }
    $OutputDir = $dialog.SelectedPath
}

if (-not $Mapping) {
    $answer = [System.Windows.Forms.MessageBox]::Show(
        "如果历史照片还是 IMG_7655 这种文件名，请选择【是】并提供三列表：样品号、照片1、照片2。`n如果照片已经按【样品号_图1/图2】命名，或 Excel J 列已经写了两个原文件名，请选择【否】。",
        "是否选择照片配对 CSV",
        [System.Windows.Forms.MessageBoxButtons]::YesNoCancel,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )
    if ($answer -eq [System.Windows.Forms.DialogResult]::Cancel) {
        exit 0
    }
    if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "选择照片配对 CSV"
        $dialog.Filter = "CSV 文件 (*.csv)|*.csv"
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            exit 0
        }
        $Mapping = $dialog.FileName
    }
}

$pythonCandidates = @(
    (Join-Path $repoRoot ".venv\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv_p122\Scripts\python.exe")
)
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $python = $pythonCommand.Source
    }
}
if (-not $python) {
    [System.Windows.Forms.MessageBox]::Show(
        "未找到 ERP Python 环境，请联系开发处理。",
        "无法启动",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 2
}

$arguments = @(
    "-X", "utf8",
    (Join-Path $repoRoot "scripts\prepare_product_photo_workbooks.py"),
    "--template", $Template,
    "--photos-dir", $PhotosDir,
    "--output-dir", $OutputDir
)
$resultJson = Join-Path (
    [System.IO.Path]::GetTempPath()
) ("tm_product_photo_batch_{0}.json" -f [guid]::NewGuid().ToString("N"))
$arguments += @("--result-json", $resultJson)
if ($Mapping) {
    $arguments += @("--mapping", $Mapping)
}

$rawOutput = & $python @arguments 2>&1 | Out-String
$exitCode = $LASTEXITCODE
$payload = $null
try {
    if (Test-Path -LiteralPath $resultJson) {
        $jsonText = [System.IO.File]::ReadAllText(
            $resultJson,
            [System.Text.Encoding]::UTF8
        )
        $payload = $jsonText | ConvertFrom-Json -ErrorAction Stop
    }
}
catch {
    $payload = $null
}
finally {
    Remove-Item -LiteralPath $resultJson -Force -ErrorAction SilentlyContinue
}

if ($exitCode -ne 0 -and $payload) {
    $issueItems = @($payload.issues)
    $nonUnusedItems = @(
        $issueItems | Where-Object { $_.code -ne "PHOTO_UNUSED" }
    )
    if ($issueItems.Count -gt 0 -and $nonUnusedItems.Count -eq 0) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "发现 $($issueItems.Count) 张照片未写入配对表。它们不会进入 ERP。`n是否忽略这些额外照片，继续生成已正确配对的导入 Excel？`n`n选择【否】可打开异常报告核对文件名。",
            "发现未配对照片",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Question
        )
        if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
            $arguments += "--allow-unused"
            $rawOutput = & $python @arguments 2>&1 | Out-String
            $exitCode = $LASTEXITCODE
            $payload = $null
            try {
                if (Test-Path -LiteralPath $resultJson) {
                    $jsonText = [System.IO.File]::ReadAllText(
                        $resultJson,
                        [System.Text.Encoding]::UTF8
                    )
                    $payload = $jsonText | ConvertFrom-Json -ErrorAction Stop
                }
            }
            catch {
                $payload = $null
            }
            finally {
                Remove-Item -LiteralPath $resultJson -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

if ($exitCode -ne 0) {
    $reportHint = "未生成异常报告，请保留下方错误内容并联系开发处理。"
    $reason = "程序未返回可识别的错误说明。"
    $issueCount = 0
    if ($payload) {
        if ($payload.message) {
            $reason = [string]$payload.message
        }
        if ($payload.issue_count) {
            $issueCount = [int]$payload.issue_count
        }
        if ($payload.report_path -and (Test-Path -LiteralPath $payload.report_path)) {
            $reportHint = "异常报告：$($payload.report_path)"
        }
    }
    elseif ($rawOutput.Trim()) {
        $reason = $rawOutput.Trim()
    }
    [System.Windows.Forms.MessageBox]::Show(
        "批量处理未生成可导入文件。`n原因：$reason`n异常数量：$issueCount`n$reportHint",
        "批量图片处理被阻断",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
    Start-Process explorer.exe -ArgumentList $OutputDir
    exit $exitCode
}

$volumeCount = 0
$sampleCount = 0
$photoCount = 0
if ($payload) {
    $volumeCount = @($payload.volumes).Count
    $sampleCount = [int]$payload.sample_count
    $photoCount = [int]$payload.photo_count
}
[System.Windows.Forms.MessageBox]::Show(
    "批量图片已转正、压缩、嵌入并按大小拆卷。`n样品：$sampleCount 款；图片：$photoCount 张；导入卷：$volumeCount 个。`n请先核对明细报告，再按 001 到最后一卷的顺序上传 ERP。`n当前客户正式存货编码映射未确认前，不得正式导入。",
    "批量图片处理完成",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information
) | Out-Null
Start-Process explorer.exe -ArgumentList $OutputDir

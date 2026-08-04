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

try {
    if ($env:COMPUTERNAME -ieq "PC-20250926DZYH") {
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
    $projectRoot = [System.IO.Path]::GetFullPath([string]$runtime.project_root)
    $database = [System.IO.Path]::GetFullPath([string]$runtime.working_database)
    $python = [System.IO.Path]::GetFullPath($PythonPath)
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "家庭重置必须使用项目 Python：$python"
    }
    $helper = Join-Path $projectRoot "scripts\admin\weekly_home_uat.py"
    $launcher = Join-Path $projectRoot "scripts\windows\start_erp_uat.ps1"
    $pidFile = Join-Path ([System.IO.Path]::GetDirectoryName($database)) ("erp_uat_{0}.pid" -f $Port)

    if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
        $pidValue = (Get-Content -LiteralPath $pidFile -Raw -Encoding ASCII).Trim()
        if ($pidValue -notmatch "^\d+$") { throw "UAT PID 文件损坏，拒绝停止未知进程。" }
        $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $pidValue) -ErrorAction SilentlyContinue
        if ($process) {
            $expectedAppDir = "--app-dir " + $projectRoot
            if ($process.CommandLine -notlike "*uvicorn*" -or
                $process.CommandLine -notlike ("*" + $expectedAppDir + "*") -or
                $process.CommandLine -notlike ("*--port " + $Port + "*")) {
                throw "PID 对应进程不是本周家庭 UAT，拒绝停止。"
            }
            Stop-Process -Id ([int]$pidValue) -Force
            Wait-Process -Id ([int]$pidValue) -Timeout 5 -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
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

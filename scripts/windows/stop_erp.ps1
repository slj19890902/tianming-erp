$ErrorActionPreference = "Stop"

$Port = 8000

function Get-ErpCandidateProcessIds {
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) {
        return @()
    }

    $candidateIds = New-Object System.Collections.Generic.List[int]
    foreach ($connection in $connections) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $commandLine = [string]$process.CommandLine
        if ($commandLine -match 'uvicorn' -or $commandLine -match 'app\.main:app' -or $commandLine -match 'python.exe') {
            [void]$candidateIds.Add([int]$process.ProcessId)
        }
    }

    return $candidateIds | Select-Object -Unique
}

$ids = Get-ErpCandidateProcessIds
if (-not $ids -or $ids.Count -eq 0) {
    Write-Host "ERP 当前没有运行。"
    exit 0
}

Write-Host "发现以下可能的 ERP 进程："
foreach ($pid in $ids) {
    $process = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($process) {
        Write-Host ("- PID {0}: {1}" -f $pid, $process.ProcessName)
    }
}

$answer = Read-Host "确定要关闭 ERP 吗？输入 Y 继续"
if ($answer -notin @("Y", "y", "Yes", "YES")) {
    Write-Host "已取消关闭。"
    exit 0
}

foreach ($pid in $ids) {
    Stop-Process -Id $pid -Force
}

Write-Host "ERP 服务已关闭。"

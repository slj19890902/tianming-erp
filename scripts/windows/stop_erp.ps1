$ErrorActionPreference = "Stop"

$Port = 8000

function Get-TargetProcessIds {
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) {
        return @()
    }

    $candidateIds = New-Object System.Collections.Generic.List[int]
    foreach ($connection in $connections) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" -ErrorAction SilentlyContinue
        if (-not $process) {
            continue
        }
        $commandLine = [string]$process.CommandLine
        if ($commandLine -match 'uvicorn' -or $commandLine -match 'app\.main:app' -or $commandLine -match 'python.exe') {
            [void]$candidateIds.Add([int]$process.ProcessId)
        }
    }

    return $candidateIds | Select-Object -Unique
}

$ids = Get-TargetProcessIds
if (-not $ids -or $ids.Count -eq 0) {
    Write-Host "ERP is not running."
    exit 0
}

Write-Host "Possible ERP processes:"
foreach ($processId in $ids) {
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) {
        Write-Host ("- PID {0}: {1}" -f $processId, $process.ProcessName)
    }
}

$answer = Read-Host "Type Y to stop ERP"
if ($answer -notin @("Y", "y", "Yes", "YES")) {
    Write-Host "Stop cancelled."
    exit 0
}

foreach ($processId in $ids) {
    Stop-Process -Id $processId -Force
}

Write-Host "ERP service stopped."

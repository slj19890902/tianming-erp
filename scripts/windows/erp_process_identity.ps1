#Requires -Version 5.1
<#
.SYNOPSIS
    天明 ERP Windows 单 worker 进程与健康响应公共门禁。

.DESCRIPTION
    本文件只提供函数，不启动或停止进程。调用方必须先校验本文件的规范化
    SHA-256，再 dot-source 使用。
#>

function Split-ErpProcessCommandLine {
    param([string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        throw "ERP 进程命令行为空。"
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
    if ($inQuotes) { throw "ERP 进程命令行包含未闭合引号。" }
    if ($buffer.Length -gt 0) { $tokens.Add($buffer.ToString()) }
    return $tokens.ToArray()
}

function Get-ErpProcessArgumentValue {
    param(
        [string[]]$Tokens,
        [string]$Name
    )
    $indexes = @(
        for ($index = 0; $index -lt $Tokens.Count; $index++) {
            if ($Tokens[$index] -ieq $Name) { $index }
        }
    )
    if ($indexes.Count -ne 1) { return $null }
    $valueIndex = [int]$indexes[0] + 1
    if ($valueIndex -ge $Tokens.Count) { return $null }
    return $Tokens[$valueIndex]
}

function Get-ErpAllowedPythonExecutables {
    param([string]$PythonPath)
    $resolvedPython = [System.IO.Path]::GetFullPath($PythonPath)
    if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
        throw "计划绑定的 Python 不存在：$resolvedPython"
    }
    $paths = New-Object 'System.Collections.Generic.List[string]'
    $paths.Add($resolvedPython)
    $baseOutput = @(
        & $resolvedPython `
            -I `
            -B `
            -X utf8 `
            -c "import sys; print(sys._base_executable)" 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $baseOutput.Count -lt 1) {
        throw "无法核对计划 Python 对应的基础解释器。"
    }
    $basePath = [System.IO.Path]::GetFullPath(
        $baseOutput[-1].ToString().Trim()
    )
    if (-not ($paths | Where-Object {
        [System.StringComparer]::OrdinalIgnoreCase.Equals($_, $basePath)
    })) {
        $paths.Add($basePath)
    }
    return $paths.ToArray()
}

function Test-ErpProcessCommandLineIdentity {
    param(
        [string]$CommandLine,
        [string]$ExpectedPythonPath,
        [string]$ExpectedApplicationRoot,
        [string]$ExpectedBindHost,
        [int]$ExpectedPort,
        [int]$ExpectedWorkers,
        [string[]]$AllowedPythonExecutables = @()
    )
    try {
        if ($ExpectedWorkers -ne 1) { return $false }
        $tokens = @(Split-ErpProcessCommandLine -CommandLine $CommandLine)
        # The launcher owns this exact argv.  An exact positional whitelist
        # rejects -c, script paths, extra modules, reordered flags, unknown
        # flags and every duplicate argument.
        if ($tokens.Count -ne 14) {
            return $false
        }
        if (
            -not [System.IO.Path]::IsPathRooted($tokens[0]) -or
            $tokens[1] -cne "-X" -or
            $tokens[2] -cne "utf8" -or
            $tokens[3] -cne "-m" -or
            $tokens[4] -cne "uvicorn" -or
            $tokens[5] -cne "app.main:app" -or
            $tokens[6] -cne "--app-dir" -or
            $tokens[8] -cne "--host" -or
            $tokens[10] -cne "--port" -or
            $tokens[12] -cne "--workers"
        ) {
            return $false
        }
        $commandPython = [System.IO.Path]::GetFullPath($tokens[0])
        $allowedPython = if ($AllowedPythonExecutables.Count -gt 0) {
            @($AllowedPythonExecutables)
        } else {
            @([System.IO.Path]::GetFullPath($ExpectedPythonPath))
        }
        if (-not ($allowedPython | Where-Object {
            [System.StringComparer]::OrdinalIgnoreCase.Equals(
                [System.IO.Path]::GetFullPath([string]$_),
                $commandPython
            )
        })) {
            return $false
        }
        if (
            @($tokens | Where-Object { $_ -ceq "-c" }).Count -gt 0 -or
            @($tokens | Where-Object { $_ -ceq "-m" }).Count -ne 1 -or
            @($tokens | Where-Object { $_ -ceq "--app-dir" }).Count -ne 1 -or
            @($tokens | Where-Object { $_ -ceq "--host" }).Count -ne 1 -or
            @($tokens | Where-Object { $_ -ceq "--port" }).Count -ne 1 -or
            @($tokens | Where-Object { $_ -ceq "--workers" }).Count -ne 1
        ) {
            return $false
        }

        $actualAppDirValue = $tokens[7]
        if (
            -not $actualAppDirValue -or
            -not [System.IO.Path]::IsPathRooted($actualAppDirValue)
        ) {
            return $false
        }
        $actualAppDir = [System.IO.Path]::GetFullPath($actualAppDirValue)
        $expectedAppDir = [System.IO.Path]::GetFullPath(
            $ExpectedApplicationRoot
        )
        if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals(
            $actualAppDir,
            $expectedAppDir
        )) {
            return $false
        }

        $actualHost = $tokens[9]
        if ($actualHost -ne $ExpectedBindHost) { return $false }
        $actualPort = $tokens[11]
        if ($actualPort -ne $ExpectedPort.ToString()) { return $false }
        $actualWorkers = $tokens[13]
        return $actualWorkers -eq $ExpectedWorkers.ToString()
    } catch {
        return $false
    }
}

function Get-ValidatedErpProcessIdentity {
    param(
        [int]$Port,
        [int]$Workers,
        [string]$PythonPath,
        [string]$ApplicationRoot,
        [string]$BindHost,
        [switch]$AllowNotRunning
    )
    if (
        $Port -lt 1 -or
        $Port -gt 65535 -or
        $Workers -ne 1 -or
        [string]::IsNullOrWhiteSpace($PythonPath) -or
        [string]::IsNullOrWhiteSpace($ApplicationRoot) -or
        [string]::IsNullOrWhiteSpace($BindHost)
    ) {
        throw "ERP 端口或单 worker 进程身份参数无效。"
    }
    $connections = @(
        Get-NetTCPConnection `
            -LocalPort $Port `
            -State Listen `
            -ErrorAction SilentlyContinue
    )
    if ($connections.Count -eq 0) {
        if ($AllowNotRunning) { return $null }
        throw "ERP 端口 $Port 当前没有监听进程。"
    }
    $processIds = @(
        $connections |
            Select-Object -ExpandProperty OwningProcess |
            Sort-Object -Unique
    )
    if ($processIds.Count -ne 1) {
        throw "ERP 端口存在额外 worker 或多个监听进程，禁止继续。"
    }

    $processId = [int]$processIds[0]
    $processInfo = Get-CimInstance `
        -ClassName Win32_Process `
        -Filter ("ProcessId = {0}" -f $processId) `
        -ErrorAction Stop
    if (-not $processInfo -or -not $processInfo.ExecutablePath) {
        throw "无法通过 CIM 核对 ERP 进程 PID=$processId。"
    }
    $actualExecutable = [System.IO.Path]::GetFullPath(
        [string]$processInfo.ExecutablePath
    )
    $allowedExecutables = @(
        Get-ErpAllowedPythonExecutables -PythonPath $PythonPath
    )
    if (-not ($allowedExecutables | Where-Object {
        [System.StringComparer]::OrdinalIgnoreCase.Equals(
            [string]$_,
            $actualExecutable
        )
    })) {
        throw "端口 $Port 的 PID=$processId 不是绑定的 Python，禁止继续。"
    }
    $commandTokens = @(
        Split-ErpProcessCommandLine `
            -CommandLine ([string]$processInfo.CommandLine)
    )
    $commandPython = if (
        $commandTokens.Count -gt 0 -and
        [System.IO.Path]::IsPathRooted($commandTokens[0])
    ) {
        [System.IO.Path]::GetFullPath($commandTokens[0])
    } else {
        ""
    }
    if (-not ($allowedExecutables | Where-Object {
        [System.StringComparer]::OrdinalIgnoreCase.Equals(
            [string]$_,
            $commandPython
        )
    })) {
        throw "端口 $Port 的 PID=$processId 命令行 Python 不匹配，禁止继续。"
    }
    if (-not (Test-ErpProcessCommandLineIdentity `
        -CommandLine ([string]$processInfo.CommandLine) `
        -ExpectedPythonPath $PythonPath `
        -ExpectedApplicationRoot $ApplicationRoot `
        -ExpectedBindHost $BindHost `
        -ExpectedPort $Port `
        -ExpectedWorkers $Workers `
        -AllowedPythonExecutables $allowedExecutables
    )) {
        throw "端口 $Port 的 PID=$processId 不符合精确 ERP 启动身份，禁止继续。"
    }
    return $processInfo
}

function Test-ErpHealthContract {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 2
    )
    try {
        $response = Invoke-WebRequest `
            -Uri $Url `
            -UseBasicParsing `
            -MaximumRedirection 0 `
            -TimeoutSec $TimeoutSeconds
        if ($response.StatusCode -ne 200) { return $false }
        $payload = $response.Content | ConvertFrom-Json
        if ($null -eq $payload -or $payload -is [System.Array]) {
            return $false
        }
        $properties = @($payload.PSObject.Properties)
        if (
            $properties.Count -ne 1 -or
            $properties[0].Name -cne "ok" -or
            $payload.ok -isnot [bool] -or
            $payload.ok -ne $true
        ) {
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

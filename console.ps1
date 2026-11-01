# brodi console: start/status stream activity; --once prints a snapshot.
# Usage: brodi start        (brodi.cmd on PATH calls this)
#        .\console.ps1 start
param(
    [ValidateSet("start", "stop", "status", "log", "restart", "doctor", "debug", "tray", "wake")][string]$Action = "start",
    [switch]$Once,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArguments
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Snapshot = $Once -or ($ExtraArguments -contains '--once')
$Log = if ($env:BRODI_LOG) { $env:BRODI_LOG } else { Join-Path $env:TEMP "brodi-console.log" }

function Get-BrodiProcess {
    # uv is the launcher; python is the actual agent. Both carry "agent.py console".
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like "*agent.py*console*" -and $_.CommandLine -like "*$PSScriptRoot*" -and $_.Name -match "^(python|uv)" }
}

function Invoke-BrodiPython([string[]]$PythonArguments) {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $python) { & $python @PythonArguments }
    else { & uv run python @PythonArguments }
}

function Show-Activity([switch]$History) {
    $running = @(Get-BrodiProcess)
    if (-not $running -and -not $History) { "Brodi is stopped. Run brodi start."; return }
    $viewArguments = @('-m', 'brodi.activity')
    if ($Snapshot -or $History) { $viewArguments += '--once' }
    if (-not $History) {
        # A virtualenv launcher can have the same command line as the actual Python child.
        $parents = @($running | ForEach-Object { $_.ParentProcessId })
        $agentProcess = $running | Where-Object { $_.ProcessId -notin $parents } | Select-Object -First 1
        if (-not $agentProcess) { $agentProcess = $running[0] }
        $viewArguments += @('--pid', [string]$agentProcess.ProcessId)
        Write-Host 'Brodi is running.' -ForegroundColor Green
    }
    Invoke-BrodiPython $viewArguments
}

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $name, $value = $Matches[1], $Matches[2].Trim('"').Trim("'")
            if (-not (Get-Item "Env:$name" -ErrorAction SilentlyContinue)) {
                Set-Item "Env:$name" $value
            }
        }
    }
}

function Stop-Brodi {
    Invoke-BrodiPython @('-m', 'brodi.background', 'stop')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Start-Tray([switch]$Manual) {
    $trayArguments = @('-m', 'brodi.tray')
    if ($Manual) { $trayArguments += '--force' }
    $trayPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    if (Test-Path -LiteralPath $trayPython) {
        Start-Process -FilePath $trayPython -ArgumentList $trayArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
    } else {
        Start-Process -FilePath 'uv' -ArgumentList (@('run', 'python') + $trayArguments) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
    }
}

function Start-Brodi {
    $running = Get-BrodiProcess
    if ($running) { Start-Tray; Show-Activity; return }
    Write-Host 'Starting Brodi...' -ForegroundColor Cyan

    Import-DotEnv (Join-Path $PSScriptRoot ".env.local")
    Import-DotEnv (Join-Path $PSScriptRoot ".env")
    if (-not $env:OPENAI_API_KEY) {
        "missing: OPENAI_API_KEY (set it in .env.local or your environment)"
        exit 1
    }

    Invoke-BrodiPython @('-m', 'brodi.background', 'start')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Start-Sleep -Seconds 4

    $running = Get-BrodiProcess
    if (-not $running) {
        "failed to start; last log lines:"
        if (Test-Path $Log) { Get-Content $Log -Tail 15 | ForEach-Object { "  $_" } }
        exit 1
    }
    Write-Host 'Brodi started. Waiting for the voice session...' -ForegroundColor Green
    Start-Tray
    Show-Activity
}

switch ($Action) {
    "wake" {
        $wakeAction = if ($ExtraArguments.Count -gt 0) { $ExtraArguments[0] } else { 'status' }
        if ($wakeAction -notin @('enable', 'disable', 'status')) { throw 'Usage: brodi wake enable|disable|status' }
        Invoke-BrodiPython @('-m', 'brodi.background', $wakeAction)
        exit $LASTEXITCODE
    }
    "doctor" {
        Invoke-BrodiPython @('-m', 'brodi.cli', '--doctor')
        exit $LASTEXITCODE
    }
    "start" { Start-Brodi }
    "tray" { Start-Tray -Manual }
    "stop" { Stop-Brodi }
    "restart" { Stop-Brodi; Start-Brodi }
    "status" { Show-Activity }
    "log" { Show-Activity -History }
    "debug" {
        if (-not (Test-Path $Log)) { "no log at $Log"; exit 1 }
        Get-Content $Log -Tail 300 |
            ForEach-Object { $_ -replace "\x1b\[[0-9;?]*[A-Za-z]", "" }
    }
}

# brodi console (Windows 11): start / stop / status / log / restart
# Usage: brodi start        (brodi.cmd on PATH calls this)
#        .\console.ps1 start
param([ValidateSet("start", "stop", "status", "log", "restart")][string]$Action = "status")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Log = if ($env:BRODI_LOG) { $env:BRODI_LOG } else { Join-Path $env:TEMP "brodi-console.log" }

function Get-BrodiProcess {
    # uv is the launcher; python is the actual agent. Both carry "agent.py console".
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like "*agent.py console*" -and $_.Name -match "^(python|uv)" }
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
    $running = Get-BrodiProcess
    if (-not $running) { "not running"; return }
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
    "stopped (pid $($running.ProcessId -join ', '))"
}

function Start-Brodi {
    $running = Get-BrodiProcess
    if ($running) { "already running (pid $($running[0].ProcessId))"; return }

    Import-DotEnv (Join-Path $PSScriptRoot ".env.local")
    Import-DotEnv (Join-Path $PSScriptRoot ".env")
    if (-not $env:OPENAI_API_KEY) {
        "missing: OPENAI_API_KEY (set it in .env.local or your environment)"
        exit 1
    }

    # One log file, raw bytes: cmd redirection keeps the agent's own encoding and merges stderr.
    $inner = "/c cd /d `"$PSScriptRoot`" && uv run python agent.py console >> `"$Log`" 2>&1"
    $wrapper = Start-Process -FilePath "cmd.exe" `
        -ArgumentList $inner -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 4

    $running = Get-BrodiProcess
    if (-not $running) {
        "failed to start; last log lines:"
        if (Test-Path $Log) { Get-Content $Log -Tail 15 | ForEach-Object { "  $_" } }
        exit 1
    }
    "started (pid $($running[0].ProcessId)), log: $Log"
    if ($wrapper.HasExited) { "note: launcher exited, agent continues under uv/python" }
}

switch ($Action) {
    "start" { Start-Brodi }
    "stop" { Stop-Brodi }
    "restart" { Stop-Brodi; Start-Brodi }
    "status" {
        $running = Get-BrodiProcess
        if (-not $running) { "not running" }
        else {
            $running | ForEach-Object {
                $started = try { $_.CreationDate } catch { $null }
                $uptime = if ($started) { [int]((Get-Date) - $started).TotalSeconds } else { 0 }
                "running (pid $($_.ProcessId), ${uptime}s): $($_.CommandLine)"
            }
        }
    }
    "log" {
        if (-not (Test-Path $Log)) { "no log at $Log"; exit 1 }
        Get-Content $Log -Tail 300 |
            ForEach-Object { $_ -replace "\x1b\[[0-9;?]*[A-Za-z]", "" } |
            Where-Object { $_ -match "route|transcript|assistant|agent>|ERROR|learned|failed|pipeline:" }
    }
}

param(
    [ValidateSet('all', 'launch', 'workspace', 'research', 'followup', 'automation', 'polite', 'calculator', 'paint', 'downloads', 'calendar', 'github_search', 'time', 'date', 'battery', 'calculate')]
    [string]$Task = 'all',
    [switch]$Run,
    [ValidateRange(1, 100)][int]$Repeat = 1,
    [ValidateRange(1, 60000)][int]$MaxDispatchMs = 500,
    [string]$Output = '.freebuff/profiles/demo'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync in the project first.' }
$arguments = @('-m', 'brodi.demo', $Task, '--repeat', "$Repeat", '--max-dispatch-ms', "$MaxDispatchMs", '--output', $Output)
if ($Run) { $arguments += '--run' }
Push-Location $projectRoot
try {
    & $python @arguments
    $resultCode = $LASTEXITCODE
} finally { Pop-Location }
exit $resultCode

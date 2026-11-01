param(
    [ValidateSet('all', 'launch', 'workspace', 'research', 'followup', 'automation', 'polite')]
    [string]$Task = 'all',
    [switch]$Run,
    [ValidateRange(1, 100)][int]$Repeat = 1,
    [string]$Output = '.freebuff/profiles/demo'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync in the project first.' }
$arguments = @('-m', 'brodi.demo', $Task, '--repeat', "$Repeat", '--output', $Output)
if ($Run) { $arguments += '--run' }
Push-Location $projectRoot
try {
    & $python @arguments
    $resultCode = $LASTEXITCODE
} finally { Pop-Location }
exit $resultCode

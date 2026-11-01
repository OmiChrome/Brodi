param(
    [ValidateRange(1, 10000)][int]$Iterations = 100,
    [switch]$Decisions,
    [switch]$Speech,
    [string]$Output = '.freebuff/profiles/benchmark'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync in the project first.' }
$arguments = @('-m', 'brodi.performance', '--iterations', "$Iterations", '--output', $Output)
if ($Decisions) { $arguments += '--decisions' }
if ($Speech) { $arguments += '--speech' }
Push-Location $projectRoot
try {
    & $python @arguments
    $resultCode = $LASTEXITCODE
} finally { Pop-Location }
exit $resultCode

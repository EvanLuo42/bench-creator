$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'benchctl.py'

if ($env:AI_WORK_BENCH_PYTHON) {
    & $env:AI_WORK_BENCH_PYTHON -B $scriptPath @args
    exit $LASTEXITCODE
}

foreach ($name in @('python3', 'python')) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) {
        & $command.Source -B $scriptPath @args
        exit $LASTEXITCODE
    }
}

$pyLauncher = Get-Command 'py' -ErrorAction SilentlyContinue
if ($pyLauncher) {
    & $pyLauncher.Source -3 -B $scriptPath @args
    exit $LASTEXITCODE
}

if ($env:USERPROFILE) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
        & $bundledPython -B $scriptPath @args
        exit $LASTEXITCODE
    }
}

Write-Error 'Python 3 was not found. Set AI_WORK_BENCH_PYTHON to an absolute Python executable path.'
exit 127

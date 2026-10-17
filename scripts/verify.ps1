param([switch]$SkipCapture)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$previousPythonPath = $env:PYTHONPATH
$previousIntegration = $env:RUN_CAPTURE_INTEGRATION
try {
    $env:PYTHONPATH = "$projectRoot;$(Join-Path $projectRoot 'backend')"
    & $pythonPath -X utf8 backend/manage.py check
    if ($LASTEXITCODE -ne 0) { throw 'Django system check failed' }
    & $pythonPath -X utf8 backend/manage.py makemigrations --check --dry-run
    if ($LASTEXITCODE -ne 0) { throw 'Database migration drift detected' }
    & $pythonPath -X utf8 backend/manage.py test library runtime tests.portability --verbosity 1
    if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed' }
    if (-not $SkipCapture) {
        $env:RUN_CAPTURE_INTEGRATION = '1'
        & $pythonPath -X utf8 -m unittest discover -s tests/capture -p 'test_*.py' -v
        if ($LASTEXITCODE -ne 0) { throw 'Capture tests failed' }
    }
    Push-Location -LiteralPath (Join-Path $projectRoot 'frontend')
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend typecheck/build failed' }
    } finally { Pop-Location }
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:RUN_CAPTURE_INTEGRATION = $previousIntegration
}

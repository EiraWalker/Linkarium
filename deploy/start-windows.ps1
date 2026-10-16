param([string]$DataRoot = '', [int]$Port = 8000, [switch]$SkipBuild, [string]$PrivateKey, [switch]$DebugAuth)
$ErrorActionPreference = 'Stop'
if (-not $DebugAuth -and -not $PrivateKey) { throw 'Normal initialization requires -PrivateKey <existing Ed25519 PKCS8 PEM file>' }
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
if ($DataRoot) { $env:BOOKMARK_DATA_ROOT = $DataRoot }
if (-not $SkipBuild) {
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
    Push-Location (Join-Path $projectRoot 'frontend')
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    } finally { Pop-Location }
}
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$initializeArguments = @('backend\manage.py', 'initialize')
if ($PrivateKey) { $initializeArguments += @('--private-key', $PrivateKey) }
if ($DebugAuth) { $initializeArguments += '--debug' }
& $pythonPath @initializeArguments
if ($LASTEXITCODE -ne 0) { throw 'Initialization failed' }
$supervisorArguments = @('backend\supervisor.py', '--host', '127.0.0.1', '--port', "$Port")
if ($DebugAuth) { $supervisorArguments += '--debug' }
& $pythonPath @supervisorArguments
if ($LASTEXITCODE -ne 0) { throw 'Supervisor stopped with an error' }

param([switch]$SkipCapture, [switch]$DebugAuth, [string]$PrivateKey)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
uv sync --frozen --python 3.13
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
Push-Location -LiteralPath (Join-Path $projectRoot 'frontend')
try {
    npm ci --registry=https://registry.npmjs.org --cache .npm-cache
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
} finally { Pop-Location }
if (-not $SkipCapture) {
    & (Join-Path $PSScriptRoot 'install-capture.ps1')
}
$initializeArguments = @((Join-Path $projectRoot 'backend\manage.py'), 'initialize')
if ($DebugAuth) { $initializeArguments += '--debug' }
if ($PrivateKey) { $initializeArguments += @('--private-key', $PrivateKey) }
& (Join-Path $projectRoot '.venv\Scripts\python.exe') @initializeArguments
if ($LASTEXITCODE -ne 0) { throw 'Offline database preparation failed' }

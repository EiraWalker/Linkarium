param([string]$DataRoot = '', [int]$Port = 8000, [switch]$SkipBuild, [string]$PrivateKey, [switch]$DebugAuth, [string]$PasskeyOrigin,
    [switch]$LanHttps, [string]$NetworkAdapter, [int]$HttpsPort, [int]$CertificatePort, [string]$LanDomain, [string]$ImportLanHttps)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
if ($DataRoot) { $env:BOOKMARK_DATA_ROOT = $DataRoot }
if ($PasskeyOrigin) { $env:BOOKMARK_PASSKEY_ORIGIN = $PasskeyOrigin }
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
    if ($LanHttps) { & (Join-Path $projectRoot 'scripts/install-https.ps1') }
}
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
# Fresh databases are prepared by the supervisor under its instance lock.
# Existing schema upgrades must be performed explicitly while services are off.
if ($PrivateKey -or $DebugAuth) {
    $initializeArguments = @('-X', 'utf8', 'backend\manage.py', 'initialize')
    if ($PrivateKey) { $initializeArguments += @('--private-key', $PrivateKey) }
    if ($DebugAuth) { $initializeArguments += '--debug' }
    & $pythonPath @initializeArguments
    if ($LASTEXITCODE -ne 0) { throw 'Initialization failed' }
}
$supervisorArguments = @('-X', 'utf8', 'backend\supervisor.py', '--host', '127.0.0.1', '--port', "$Port", '--network-auto')
if ($DebugAuth) { $supervisorArguments += '--debug' }
if ($LanHttps) { $supervisorArguments += '--lan-https' }
if ($NetworkAdapter) { $supervisorArguments += @('--network-adapter', $NetworkAdapter) }
if ($PSBoundParameters.ContainsKey('HttpsPort')) { $supervisorArguments += @('--https-port', "$HttpsPort") }
if ($PSBoundParameters.ContainsKey('CertificatePort')) { $supervisorArguments += @('--certificate-port', "$CertificatePort") }
if ($LanDomain) { $supervisorArguments += @('--lan-domain', $LanDomain) }
if ($ImportLanHttps) { $supervisorArguments += @('--lan-import', $ImportLanHttps) }
& $pythonPath @supervisorArguments
if ($LASTEXITCODE -ne 0) { throw 'Supervisor stopped with an error' }

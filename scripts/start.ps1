param(
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [string]$BindAddress = '127.0.0.1',
    [switch]$DevelopmentCapture,
    [switch]$PublicDns,
    [switch]$PerformanceMonitoring,
    [string]$PasskeyOrigin,
    [switch]$LanHttps,
    [string]$NetworkAdapter,
    [ValidateRange(1, 65535)][int]$HttpsPort,
    [ValidateRange(1, 65535)][int]$CertificatePort,
    [string]$LanDomain,
    [string]$ImportLanHttps,
    [switch]$DebugAuth
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$previousCaptureMode = $env:BOOKMARK_CAPTURE_MODE
$previousDnsMode = $env:BOOKMARK_CAPTURE_DNS_MODE
$previousPerformanceMode = $env:BOOKMARK_PERF
$previousPasskeyOrigin = $env:BOOKMARK_PASSKEY_ORIGIN
try {
    if ($PublicDns -and -not $DevelopmentCapture) { throw '-PublicDns requires -DevelopmentCapture' }
    if ($DevelopmentCapture) { $env:BOOKMARK_CAPTURE_MODE = 'development' }
    if ($PublicDns) { $env:BOOKMARK_CAPTURE_DNS_MODE = 'public-doh' }
    if ($PerformanceMonitoring) { $env:BOOKMARK_PERF = '1' }
    if ($PasskeyOrigin) { $env:BOOKMARK_PASSKEY_ORIGIN = $PasskeyOrigin }
    $supervisorArguments = @('-X', 'utf8', (Join-Path $projectRoot 'backend\supervisor.py'), '--host', $BindAddress, '--port', "$Port", '--network-auto')
    if (-not $PSBoundParameters.ContainsKey('Port')) { $supervisorArguments += '--auto-port' }
    if ($DebugAuth) { $supervisorArguments += '--debug' }
    if ($LanHttps) { $supervisorArguments += '--lan-https' }
    if ($NetworkAdapter) { $supervisorArguments += @('--network-adapter', $NetworkAdapter) }
    if ($PSBoundParameters.ContainsKey('HttpsPort')) { $supervisorArguments += @('--https-port', "$HttpsPort") }
    if ($PSBoundParameters.ContainsKey('CertificatePort')) { $supervisorArguments += @('--certificate-port', "$CertificatePort") }
    if ($LanDomain) { $supervisorArguments += @('--lan-domain', $LanDomain) }
    if ($ImportLanHttps) { $supervisorArguments += @('--lan-import', $ImportLanHttps) }
    & (Join-Path $projectRoot '.venv\Scripts\python.exe') @supervisorArguments
    if ($LASTEXITCODE -ne 0) { throw 'Application stopped with an error' }
} finally {
    $env:BOOKMARK_CAPTURE_MODE = $previousCaptureMode
    $env:BOOKMARK_CAPTURE_DNS_MODE = $previousDnsMode
    $env:BOOKMARK_PERF = $previousPerformanceMode
    $env:BOOKMARK_PASSKEY_ORIGIN = $previousPasskeyOrigin
}

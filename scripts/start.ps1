param(
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [string]$BindAddress = '127.0.0.1',
    [switch]$DevelopmentCapture,
    [switch]$PublicDns,
    [switch]$PerformanceMonitoring,
    [switch]$DebugAuth
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$previousCaptureMode = $env:BOOKMARK_CAPTURE_MODE
$previousDnsMode = $env:BOOKMARK_CAPTURE_DNS_MODE
$previousPerformanceMode = $env:BOOKMARK_PERF
try {
    if ($PublicDns -and -not $DevelopmentCapture) { throw '-PublicDns requires -DevelopmentCapture' }
    if ($DevelopmentCapture) { $env:BOOKMARK_CAPTURE_MODE = 'development' }
    if ($PublicDns) { $env:BOOKMARK_CAPTURE_DNS_MODE = 'public-doh' }
    if ($PerformanceMonitoring) { $env:BOOKMARK_PERF = '1' }
    $supervisorArguments = @('-X', 'utf8', (Join-Path $projectRoot 'backend\supervisor.py'), '--host', $BindAddress, '--port', "$Port")
    if (-not $PSBoundParameters.ContainsKey('Port')) { $supervisorArguments += '--auto-port' }
    if ($DebugAuth) { $supervisorArguments += '--debug' }
    & (Join-Path $projectRoot '.venv\Scripts\python.exe') @supervisorArguments
    if ($LASTEXITCODE -ne 0) { throw 'Application stopped with an error' }
} finally {
    $env:BOOKMARK_CAPTURE_MODE = $previousCaptureMode
    $env:BOOKMARK_CAPTURE_DNS_MODE = $previousDnsMode
    $env:BOOKMARK_PERF = $previousPerformanceMode
}

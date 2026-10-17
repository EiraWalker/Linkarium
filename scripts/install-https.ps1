<#
.SYNOPSIS
Prepare the pinned local HTTPS runtime without installing a service or trusted CA.
#>
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot 'tools/caddy'
$version = '2.11.7'
$expectedHash = '0a1edc0b799512051c57071ce0e798d3f2cf67dc98171366f1d2b326072e3b06'
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
$archive = Join-Path $runtimeRoot "caddy_${version}_windows_amd64.zip"
if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
    $download = "$archive.download"
    Invoke-WebRequest -Uri "https://github.com/caddyserver/caddy/releases/download/v$version/caddy_${version}_windows_amd64.zip" -OutFile $download
    if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
        throw 'Caddy download checksum mismatch; runtime was not installed.'
    }
    Move-Item -LiteralPath $download -Destination $archive -Force
}
Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot -Force
& (Join-Path $runtimeRoot 'caddy.exe') version
if ($LASTEXITCODE -ne 0) { throw 'Caddy runtime could not start.' }

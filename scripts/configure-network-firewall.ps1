#Requires -RunAsAdministrator
<#
.SYNOPSIS
Allow the running instance's actual LAN ports on its selected adapter and subnet.
#>
param([string]$DataRoot)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $DataRoot) { $DataRoot = if ($env:BOOKMARK_DATA_ROOT) { $env:BOOKMARK_DATA_ROOT } else { Join-Path $projectRoot 'data' } }
$recordPath = Join-Path $DataRoot 'runtime/network-access.json'
if (-not (Test-Path -LiteralPath $recordPath)) { throw 'Start the application before configuring its LAN firewall rules.' }
$connection = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$connection.process_id)"
if (-not $serverProcess -or $serverProcess.CommandLine -notmatch 'supervisor\.py') { throw 'The recorded server is no longer running; restart it first.' }
$recordedAt = [DateTimeOffset]::FromUnixTimeMilliseconds([long]($connection.recorded_at * 1000)).UtcDateTime
if ($serverProcess.CreationDate.ToUniversalTime() -gt $recordedAt) { throw 'The recorded process ID was reused; restart the application first.' }
$address = Get-NetIPAddress -InterfaceAlias $connection.adapter -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -eq $connection.ipv4 -and $_.AddressState -eq 'Preferred' }
if (-not $address) { throw 'The selected adapter address has changed; correct the server configuration first.' }
if ($connection.mode -notin 'http','lan_https') { throw 'Configure the firewall of the external HTTPS proxy separately.' }
$ports = @(([uri]$connection.access_url).Port)
if ($connection.mode -eq 'lan_https') { $ports += ([uri]$connection.certificate_url).Port }
foreach ($port in ($ports | Select-Object -Unique)) {
    $rule = @{
        Name = "Linkarium-LAN-$($connection.ipv4.Replace('.', '-'))-TCP-$port"
        DisplayName = "Linkarium $($connection.adapter) TCP $port (local subnet)"
        Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'; Protocol = 'TCP'
        LocalPort = $port; LocalAddress = $connection.ipv4; RemoteAddress = 'LocalSubnet'
        InterfaceAlias = $connection.adapter; Profile = 'Any'
    }
    if (Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule @rule | Out-Null
    } else {
        New-NetFirewallRule @rule | Out-Null
    }
    Write-Host "$($connection.adapter): $($connection.ipv4):$port (LocalSubnet)"
}

param([string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe")
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$cache = Join-Path $workspace 'tools\monolith'
[System.IO.Directory]::CreateDirectory($cache) | Out-Null
$binary = Join-Path $cache 'monolith.exe'
if (-not (Test-Path -LiteralPath $binary)) {
    Invoke-WebRequest -Uri 'https://github.com/Y2Z/monolith/releases/download/v2.10.1/monolith.exe' -OutFile $binary
}
$expected = '9BB3517CEB8BE62E9E3570A08B13E7C5DCAC85FBE79494130A208CF9342BB1D2'
if ((Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash -ne $expected) {
    throw 'Monolith binary SHA-256 mismatch; installation stopped.'
}
& $binary -V
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $workspace 'tools\browsers'
& $Python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed.' }
Write-Output 'Capture tools installed. Native Windows capture is explicitly development-only.'

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not $env:VIRTUAL_ENV) {
    . .\.venv\Scripts\Activate.ps1
}

Write-Host "`n=== PARLEY LIVE-BROWSER PRODUCTION VERIFICATION ===" -ForegroundColor Cyan

python -m compileall -q .\parley .\parley.py .\parley_mcp.py .\tests
if ($LASTEXITCODE -ne 0) {
    throw "Compilation failed."
}

python -m unittest discover -s .\tests -v
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed."
}

Write-Host "`nLive-browser production verification passed." -ForegroundColor Green

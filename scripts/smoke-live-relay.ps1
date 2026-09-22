$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not $env:VIRTUAL_ENV) {
    . .\.venv\Scripts\Activate.ps1
}

Write-Host "`n=== PARLEY LIVE A <-> B SMOKE TEST ===" -ForegroundColor Cyan
Write-Host "Prerequisites:" -ForegroundColor Yellow
Write-Host "  1. In normal Chrome, enable remote debugging at chrome://inspect/#remote-debugging"
Write-Host "  2. Open two ChatGPT conversation tabs."
Write-Host "  3. In ChatGPT A, create a completed assistant reply that can be relayed."
Write-Host "  4. Leave both tabs open."
Write-Host ""
Write-Host "Chrome may ask once to Allow remote debugging for this Parley process." -ForegroundColor Yellow
Write-Host "This smoke test performs exactly ONE full A -> B -> A round." -ForegroundColor Yellow
Write-Host ""

python .\parley.py relay --rounds 1 --json
if ($LASTEXITCODE -ne 0) {
    throw "Live relay smoke test failed."
}

Write-Host "`nLive A <-> B smoke test completed successfully." -ForegroundColor Green

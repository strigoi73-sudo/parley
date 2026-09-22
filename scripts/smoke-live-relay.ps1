param(
    [ValidateRange(1, 1000000)]
    [int]$Rounds
)

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not $env:VIRTUAL_ENV) {
    . .\.venv\Scripts\Activate.ps1
}

Write-Host "`n=== PARLEY LIVE A <-> B SMOKE TEST ===" -ForegroundColor Cyan
Write-Host "Prerequisites:" -ForegroundColor Yellow
Write-Host "  1. In normal Chrome, enable remote debugging at chrome://inspect/#remote-debugging"
Write-Host "  2. Open two ChatGPT conversation tabs."
Write-Host "  3. Leave both ChatGPT tabs open."
Write-Host ""
Write-Host "Chrome may ask once to Allow remote debugging for this Parley process." -ForegroundColor Yellow
Write-Host "Choose a round limit after selecting ChatGPT A and B." -ForegroundColor Yellow
Write-Host "Then enter the initial prompt Parley should send to ChatGPT A." -ForegroundColor Yellow
Write-Host "While the relay is running, type EXTEND CHAT <new total rounds> to raise the limit." -ForegroundColor Yellow
Write-Host ""

if ($PSBoundParameters.ContainsKey("Rounds")) {
    python .\parley.py relay --rounds $Rounds --prompt-a --json
} else {
    python .\parley.py relay --prompt-a --json
}
if ($LASTEXITCODE -ne 0) {
    throw "Live relay smoke test failed."
}

Write-Host "`nLive A <-> B smoke test completed successfully." -ForegroundColor Green

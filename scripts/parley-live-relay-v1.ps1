param(
    [ValidateRange(1, 1000000)]
    [int]$Rounds,

    [switch]$FreshChats
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Parley virtual environment not found at $Python"
}

Write-Host "`n=== PARLEY LIVE RELAY v1 ===" -ForegroundColor Cyan
Write-Host "Prerequisites:" -ForegroundColor Yellow
Write-Host "  1. In normal Chrome, enable remote debugging at chrome://inspect/#remote-debugging"
if ($FreshChats) {
    Write-Host "  2. Parley will create two fresh ChatGPT tabs automatically."
    Write-Host "  3. Leave the created ChatGPT tabs open while the relay runs."
} else {
    Write-Host "  2. Open two ChatGPT conversation tabs."
    Write-Host "  3. Leave both ChatGPT tabs open."
}
Write-Host ""
Write-Host "Parley will provision and verify each chat's protocol file before activation." -ForegroundColor Yellow
Write-Host "Startup is barriered: A file/ACK, B file/ACK, then A activation, then B activation." -ForegroundColor Yellow
if ($FreshChats) {
    Write-Host "Fresh ChatGPT A and B will be initiated automatically before protocol upload." -ForegroundColor Yellow
} else {
    Write-Host "Choose a round limit after selecting ChatGPT A and B." -ForegroundColor Yellow
}
Write-Host "Paste the initial prompt for ChatGPT A. Multiline prompts are supported." -ForegroundColor Yellow
Write-Host "When finished, enter END PROMPT on a line by itself." -ForegroundColor Yellow
Write-Host "While the relay is running, use s/status to inspect state and q/stop to end it." -ForegroundColor Yellow
Write-Host "Use EXTEND CHAT <new total rounds> to raise the round limit." -ForegroundColor Yellow
Write-Host ""

$ParleyArgs = @(
    ".\parley.py",
    "relay",
    "--initialize",
    "--prompt-a",
    "--json"
)

if ($PSBoundParameters.ContainsKey("Rounds")) {
    $ParleyArgs += @("--rounds", "$Rounds")
}

if ($FreshChats) {
    $ParleyArgs += "--fresh-chats"
}

& $Python @ParleyArgs
if ($LASTEXITCODE -ne 0) {
    throw "Parley live relay v1 failed."
}

Write-Host "`nParley live relay v1 completed successfully." -ForegroundColor Green

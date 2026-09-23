param(
    [ValidateRange(1, 1000000)]
    [int]$Rounds,

    [switch]$FreshChats,

    [switch]$FreshA,

    [switch]$FreshB,

    [switch]$Json
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
if ($FreshChats -or ($FreshA -and $FreshB)) {
    Write-Host "  2. Parley will create fresh ChatGPT A and B automatically."
    Write-Host "  3. Leave the created ChatGPT tabs open while the relay runs."
} elseif ($FreshA -or $FreshB) {
    $ExistingRole = if ($FreshA) { "B" } else { "A" }
    $FreshRole = if ($FreshA) { "A" } else { "B" }
    Write-Host "  2. Open the existing ChatGPT conversation for participant $ExistingRole."
    Write-Host "  3. Parley will create a fresh ChatGPT $FreshRole and let you select the existing tab."
} else {
    Write-Host "  2. Open the ChatGPT conversations you want to use."
    Write-Host "  3. Parley will let you select ChatGPT A and B independently."
}
Write-Host ""
Write-Host "Parley will provision and verify each chat's protocol file before activation." -ForegroundColor Yellow
Write-Host "Startup is barriered: A file/ACK, B file/ACK, then A activation, then B activation." -ForegroundColor Yellow
if ($FreshChats -or ($FreshA -and $FreshB)) {
    Write-Host "Fresh ChatGPT A and B will be initiated automatically before protocol upload." -ForegroundColor Yellow
} elseif ($FreshA -or $FreshB) {
    Write-Host "Mixed setup enabled: one participant is fresh and one uses an open ChatGPT tab." -ForegroundColor Yellow
} else {
    Write-Host "Choose ChatGPT A and B from the eligible open tabs." -ForegroundColor Yellow
}
Write-Host "Participant sources can be mixed independently: existing/existing, existing/fresh, fresh/existing, or fresh/fresh." -ForegroundColor Yellow
Write-Host "Paste the initial prompt for ChatGPT A. Multiline prompts are supported." -ForegroundColor Yellow
Write-Host "When finished, enter END PROMPT on a line by itself." -ForegroundColor Yellow
Write-Host "While the relay is running, use s/status to inspect state and q/stop to end it." -ForegroundColor Yellow
Write-Host "Use EXTEND CHAT <new total rounds> to raise the round limit." -ForegroundColor Yellow
Write-Host ""

$ParleyArgs = @(
    ".\parley.py",
    "relay",
    "--initialize",
    "--prompt-a"
)

if ($PSBoundParameters.ContainsKey("Rounds")) {
    $ParleyArgs += @("--rounds", "$Rounds")
}

if ($FreshChats) {
    $ParleyArgs += "--fresh-chats"
} else {
    if ($FreshA) {
        $ParleyArgs += "--fresh-a"
    }
    if ($FreshB) {
        $ParleyArgs += "--fresh-b"
    }
}

if ($Json) {
    $ParleyArgs += "--json"
}

& $Python @ParleyArgs
if ($LASTEXITCODE -ne 0) {
    throw "Parley live relay v1 failed."
}

Write-Host "`nParley live relay v1 ended cleanly." -ForegroundColor Green

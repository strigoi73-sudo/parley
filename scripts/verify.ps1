$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Parley virtual environment not found at $Python"
}

Write-Host "`n=== PARLEY VERIFICATION ===" -ForegroundColor Cyan

& $Python -m compileall -q .\parley .\parley.py .\parley_mcp.py .\tests
if ($LASTEXITCODE -ne 0) {
    throw "Compilation failed."
}

& $Python -m unittest discover -s .\tests -v
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed."
}

Write-Host "`nParley verification passed." -ForegroundColor Green

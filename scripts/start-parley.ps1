$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Parley virtual environment not found at $Python"
}

& $Python ".\parley.py" "app"
if ($LASTEXITCODE -ne 0) {
    throw "Parley desktop app failed."
}

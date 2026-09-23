$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not $env:VIRTUAL_ENV) {
    . .\.venv\Scripts\Activate.ps1
}

$env:PARLEY_CONNECTION_MODE = "live"
python .\parley.py gui

if ($LASTEXITCODE -ne 0) {
    throw "Parley GUI exited with an error."
}

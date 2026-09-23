$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not $env:VIRTUAL_ENV) {
    . .\.venv\Scripts\Activate.ps1
}

python .\parley.py gui
if ($LASTEXITCODE -ne 0) {
    throw "Parley UI exited with an error."
}

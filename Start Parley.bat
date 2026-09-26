@echo off
cd /d "%~dp0"

echo Starting Parley...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-parley.ps1"

if errorlevel 1 (
    echo.
    echo Parley exited with an error.
    pause
)

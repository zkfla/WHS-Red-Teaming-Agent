$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Local virtual environment not found. Run .\scripts\setup.ps1 first."
    exit 1
}

& ".venv\Scripts\python.exe" -m redteaming.cli @args

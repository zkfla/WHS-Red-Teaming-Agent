param([switch]$Launch)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Get-Command py -ErrorAction SilentlyContinue) -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.10+ is required. Install Python from https://www.python.org/downloads/"
}

$pythonLauncher = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
$venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$recreate = -not (Test-Path -LiteralPath $venvPython)
if (-not $recreate) {
    & $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    $recreate = ($LASTEXITCODE -ne 0)
}
if ($recreate) {
    if (Test-Path -LiteralPath ".venv") {
        Write-Host "Existing .venv is stale or invalid; recreating it."
        Remove-Item -LiteralPath ".venv" -Recurse -Force
    }
    if ($pythonLauncher -eq "py") { & py -3 -m venv .venv } else { & python -m venv .venv }
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e ".[llm]"

Write-Host "준비가 끝났습니다."
if ($Launch) { & "$PSScriptRoot\start.ps1" }

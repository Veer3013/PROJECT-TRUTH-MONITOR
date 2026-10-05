$ErrorActionPreference = "Stop"

Write-Host "ProjectTruth Monitor - Windows setup" -ForegroundColor Cyan

$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python Launcher (py.exe) was not found. Install Python 3.13 first."
}

$version = & py -3.13 --version 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.13 is required. Run 'py -0p' to see installed versions."
}
Write-Host $version

if (-not (Test-Path ".venv")) {
    & py -3.13 -m venv .venv
}

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
& .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Warning "Created .env from .env.example. Replace SECRET_KEY before any non-local use."
}

python -m alembic upgrade head
python -m app.seed_demo
python -m pytest -q -p no:cacheprovider

Write-Host "Setup and release tests completed." -ForegroundColor Green
Write-Host "Run: .\\scripts\\run_windows.ps1" -ForegroundColor Yellow

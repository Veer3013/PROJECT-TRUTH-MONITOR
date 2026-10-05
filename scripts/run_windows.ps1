$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
    throw "Virtual environment not found. Run .\\scripts\\setup_windows.ps1 first."
}

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
& .\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

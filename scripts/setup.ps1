# One-time setup: Python venv + dependencies, frontend dependencies, browser check.
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location (Join-Path $root "backend")

if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m pip install pgserver   # optional: embedded PostgreSQL used by the DB tests

if (-not (Test-Path ".env")) {
    Copy-Item (Join-Path $root ".env.example") ".env"
    Write-Host "Created backend\.env - set SAA_SECURITY__API_TOKEN and your LLM key." -ForegroundColor Yellow
}

# Browser automation: use the installed Chrome/Edge when present, otherwise download Chromium.
$chrome = Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe"
$edge = Test-Path "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not ($chrome -or $edge)) { & .\.venv\Scripts\python.exe -m playwright install chromium }

Set-Location (Join-Path $root "frontend")
npm install --no-audit --no-fund

Set-Location (Join-Path $root "backend")
& .\.venv\Scripts\python.exe -m app doctor

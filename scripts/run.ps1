# Production-style run: build the UI, apply migrations, serve UI + API from one process on :8000.
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$py = Join-Path $root "backend\.venv\Scripts\python.exe"

Push-Location (Join-Path $root "frontend"); try { npm run build } finally { Pop-Location }
Push-Location (Join-Path $root "backend")
try {
    & $py -m app migrate
    & $py -m app serve
} finally { Pop-Location }

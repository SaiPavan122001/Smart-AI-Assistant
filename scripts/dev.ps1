# Development: API with auto-reload on :8000 + Vite dev server on :5173 (proxying /api).
param([switch]$Db)   # -Db: start PostgreSQL with docker compose first
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")

if ($Db) { docker compose -f (Join-Path $root "infra/docker/docker-compose.yml") up -d --wait }

$py = Join-Path $root "backend\.venv\Scripts\python.exe"
$api = Start-Process -PassThru -NoNewWindow -WorkingDirectory (Join-Path $root "backend") $py `
    "-m uvicorn app.api.main:app_factory --factory --reload --host 127.0.0.1 --port 8000 --no-access-log"
try {
    Set-Location (Join-Path $root "frontend")
    npm run dev
} finally {
    if (-not $api.HasExited) { Stop-Process -Id $api.Id -Force }
}

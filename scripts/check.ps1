# Everything CI runs, locally. Use -Fast to skip the slow browser/E2E suites.
param([switch]$Fast)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$py = Join-Path $root "backend\.venv\Scripts\python.exe"
function Step($name, [scriptblock]$cmd) {
    Write-Host "`n== $name ==" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit 1 }
}

Push-Location (Join-Path $root "backend")
Step "ruff format --check" { & $py -m ruff format --check app tests evals }
Step "ruff lint" { & $py -m ruff check app tests evals }
Step "mypy (strict)" { & $py -m mypy }
if ($Fast) { Step "pytest (unit + security + api)" { & $py -m pytest tests/unit tests/security tests/integration/test_api.py -q } }
else {
    Step "pytest (all, incl. browser + PostgreSQL + migrations)" { & $py -m pytest -q }
    Step "evaluation suite" { & $py -m evals.run }
}
Pop-Location

Push-Location (Join-Path $root "frontend")
Step "eslint" { npm run lint }
Step "tsc" { npm run typecheck }
Step "vitest" { npm test }
Step "build" { npm run build }
if (-not $Fast) { Step "playwright E2E" { npm run test:e2e } }
Pop-Location
Write-Host "`nAll checks passed." -ForegroundColor Green

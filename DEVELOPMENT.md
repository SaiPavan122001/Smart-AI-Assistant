# Development

The detailed guides live in [`docs/development`](docs/development):

* [Setup and commands](docs/development/setup.md)
* [Testing](docs/development/testing.md) (backend suites, evaluation suite, frontend, E2E)
* [Database migrations](docs/development/migrations.md)
* Extending: [tools](docs/development/adding-tools.md) · [LLM providers](docs/development/adding-llm-providers.md) · [perception providers](docs/development/adding-perception-providers.md) · [desktop adapters](docs/development/adding-desktop-adapters.md)

## Inner loop

```powershell
.\scripts\setup.ps1                 # once
.\scripts\dev.ps1 -Db               # API (reload) + UI (HMR) + PostgreSQL
.\scripts\check.ps1 -Fast           # before every commit: format, lint, strict mypy, unit/security/API tests, frontend lint/types/tests/build
.\scripts\check.ps1                 # before every PR: also browser/PostgreSQL/migration/E2E suites, the evaluation suite and Playwright UI tests
```

## Ground rules

1. Everything that can touch the computer is a **tool** and runs through the **router → permission engine**. No exceptions, no shortcuts "just for tests".
2. Tools implement `assess()` honestly (risk, human-readable action/resource, hard denies) and re-validate in `run()`.
3. External content is untrusted data; mark such outputs `returns_external_content`.
4. Persisted/logged/emitted/prompted data passes redaction.
5. Docs generated from code (`python -m app.docs_gen`) must be current; `.env.example` must match the settings model — tests enforce both.
6. Test adapters (`ScriptedProvider`, `FakeDesktopBackend`, `InMemoryRepository`) are for tests/evals/ephemeral mode and are never presented as production integrations.
7. Keep the evaluation thresholds green (`python -m evals.run`); security violations and permission bypasses must stay at 0.

## Repository map

See [ARCHITECTURE.md](ARCHITECTURE.md) and the [README](README.md#repository-layout).

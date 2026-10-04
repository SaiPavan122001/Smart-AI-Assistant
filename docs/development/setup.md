# Development setup

Requirements: Windows 10/11 (desktop automation), Python 3.11+, Node 20+, Git, Chrome or Edge. Docker is optional (PostgreSQL).

```powershell
.\scripts\setup.ps1          # venv + `pip install -e .[dev]` + pgserver + backend\.env + npm install + `python -m app doctor`
.\scripts\dev.ps1 -Db        # PostgreSQL (docker), API with reload on :8000, Vite on :5173
```

Manual equivalent:

```powershell
cd backend
python -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -e ".[dev]" pgserver
copy ..\.env.example .env       # then edit
.\.venv\Scripts\python.exe -m app doctor
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app_factory --factory --reload --port 8000
cd ..\frontend ; npm install ; npm run dev
```

## Commands

| Task | Command |
|---|---|
| Format / lint / types (backend) | `ruff format app tests evals` · `ruff check app tests evals` · `mypy` (strict) |
| Tests | see [testing.md](testing.md) |
| Eval suite | `python -m evals.run` |
| Regenerate docs | `python -m app.docs_gen` (tool catalogue + OpenAPI) |
| Environment check | `python -m app doctor` |
| Tool list | `python -m app tools` |
| Frontend | `npm run dev | build | lint | typecheck | test | test:e2e` |

## Project conventions

* Python: strict typing (mypy strict, Pydantic everywhere), ruff (E,F,W,I,UP,B,S,ASYNC,RUF,SIM,C4,PL), 100 columns. No hidden global state: collaborators are built in `container.py` and injected.
* Errors are structured (`AgentError` + `ErrorCode`); never raise bare exceptions across module boundaries; never swallow errors silently.
* Anything that can reach the user's computer goes through the tool router. Anything persisted/logged/emitted passes redaction.
* TypeScript: strict + `noUncheckedIndexedAccess`, ESLint with react-hooks, no `any`.
* Commits: small, logical, conventional-style messages; run `scripts\check.ps1 -Fast` before committing.

## Layout map

See the tree in [ARCHITECTURE.md](../../ARCHITECTURE.md#1-shape-a-modular-monolith-with-hard-internal-boundaries) and the extension guides:
[tools](adding-tools.md) · [LLM providers](adding-llm-providers.md) · [perception providers](adding-perception-providers.md) · [desktop adapters](adding-desktop-adapters.md) · [migrations](migrations.md).

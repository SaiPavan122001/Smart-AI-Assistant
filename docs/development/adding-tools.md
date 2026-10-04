# Adding a tool

A tool is the *only* way the agent acts. Keep tools small, typed and honest about risk.

## 1. Define input and the tool

```python
# backend/app/tools/filesystem/tools.py (or a new module under tools/<capability>/)
class FsTouchInput(BaseModel):
    path: str = Field(description="File to create. '@Desktop/..', '@Workspace/..' or absolute inside allowed roots")

class FsTouch(Tool[FsTouchInput]):
    name = "fs_touch"                      # unique, snake_case, capability prefix
    description = "Create an empty file."  # shown to the model: say WHAT it does and its limits
    capability = Capability.FILESYSTEM
    Input = FsTouchInput
    risk = RiskLevel.MEDIUM                # baseline; assess() refines it per call
    idempotent = True                      # safe to retry?  read_only / cacheable for observation tools
    # returns_external_content = True      # if the output contains attacker-controllable text (pages, file contents, names)

    def assess(self, args, ctx) -> RiskAssessment:
        rp = ctx.paths.resolve(args.path, write=True, cwd=ctx.cwd, allow_root_itself=False)   # raises PathViolation → hard deny
        return RiskAssessment(risk=RiskLevel.MEDIUM, action=f"Create empty file {rp.path}",
                              resource=str(rp.path), category="file_create")

    async def run(self, args, ctx) -> ToolOutput:
        rp = ctx.paths.resolve(args.path, write=True, cwd=ctx.cwd, allow_root_itself=False)    # re-validate at execution time
        ...
        return ToolOutput(data={"path": str(rp.path)}, summary=f"Created {rp.path}", artifacts={"path": str(rp.path)})
```

## 2. Register it

Add the class to the capability's `*_TOOLS` list (`FILESYSTEM_TOOLS`, `BROWSER_TOOLS`, …). `registry.py::build_registry` picks it up; the planner prompt, `GET /api/v1/tools`
and the docs are generated from the registry.

## 3. Rules (reviewed in PRs)

* **`assess()` is the security contract.** Return the *real* per-call risk, a human-readable `action` and `resource` (they become the approval text),
  and raise `PolicyViolation` for anything that must never run. Use `scoped_grant_allowed=False` for anything where "approve for this task" would be unsafe,
  `mask_args=[...]` for sensitive arguments.
* **Re-validate in `run()`** with the same policy objects (paths, URLs, commands). Never trust that `assess()` ran against the same filesystem state.
* **No hidden capabilities.** Do not shell out, `eval`, or accept raw coordinates/keystrokes/commands. Add an allowlist or a typed parameter instead.
* **Errors**: raise `AgentError(message, code=ErrorCode.X, retryable=…, details={…})` — codes drive recovery. Use `retryable=True` only for failures that happen *before* any side effect.
* **Untrusted output**: set `returns_external_content=True` if the result includes text you did not generate. The router will scan/wrap it.
* **Results are summary-first**: put a short `summary` for the LLM; keep `data` compact and capped; put machine-checkable evidence in `artifacts` (paths, urls, exit codes) so later steps can reference them.
* **Cancellation**: `await` only cancellable operations; if you spawn processes, kill their trees on `CancelledError`.
* **Read-only tools** set `read_only=True` (and `cacheable=True` if repeated identical calls are pointless).

## 4. Test it

* Unit-test `assess()` (risk, hard denies, masking) and `run()` through the **router** with the `env` fixture (`tests/helpers.py::make_env`), including: policy denial, approval flow
  (`Responder`), cancellation, redaction, and an adversarial input.
* Add a scenario to `evals/tasks.py` if it enables a new workflow.
* Regenerate docs: `python -m app.docs_gen` (a test fails if `docs/tools/catalog.md` or `docs/api/openapi.json` is stale).

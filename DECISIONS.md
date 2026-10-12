# Decisions

Index of the decisions that shape the code. Architecture decisions have full records in [docs/adr](docs/adr); the entries below them
were made during the single-user hardening iterations and are recorded here with a pointer to the document that explains them.
Add new decisions to this file; do not re-open accepted ones without a new entry that says why.

## Architecture decision records

| # | Decision |
|---|---|
| [0001](docs/adr/0001-modular-monolith.md) | Modular monolith, no extra infrastructure |
| [0002](docs/adr/0002-perception-hierarchy.md) | Structured perception first; vision is an explicit, metered fallback |
| [0003](docs/adr/0003-playwright-library-not-mcp.md) | Playwright as a library; no MCP layer |
| [0004](docs/adr/0004-permission-engine-boundary.md) | Router + permission engine as the only path to the computer |
| [0005](docs/adr/0005-untrusted-data-and-taint.md) | External content is data; deterministic detection + taint + provenance |
| [0006](docs/adr/0006-plans-deterministic-steps.md) | Plans of deterministic steps with bounded adaptive steps |
| [0007](docs/adr/0007-event-sourced-ui.md) | Event-sourced UI over WebSocket with sequence replay |
| [0008](docs/adr/0008-postgres-sqlalchemy-alembic.md) | PostgreSQL + SQLAlchemy async + Alembic, behind a repository port |
| [0009](docs/adr/0009-controlled-terminal.md) | No shell: parsed, classified, allowlisted commands |
| [0010](docs/adr/0010-llm-provider-abstraction.md) | Provider-agnostic LLM interface over plain HTTP |
| [0011](docs/adr/0011-verification-contracts.md) | Declarative verification contracts; success is observed, not assumed |

## Decisions from the single-user hardening (v0.1.0)

| # | Decision | Why | Detail |
|---|---|---|---|
| D1 | One task at a time (`max_concurrent_tasks = 1`); task start is idempotent | two tasks would drive the same keyboard/desktop; a double submit must not run twice | [execution-loop-fix](docs/execution-loop-fix.md) |
| D2 | Per-task action ledger: a succeeded state-establishing action is replayed, never re-executed; loops trip `loop_detected` and are final | a verification false-negative must not re-launch an app | [execution-loop-fix](docs/execution-loop-fix.md) |
| D3 | The final answer is decided once, from final state, by code; a non-COMPLETED task never gets a success answer | a FAILED task returned "has been deleted" | [f2-truthful-answers](docs/fixes/f2-truthful-answers.md) |
| D4 | `status`/`outcome`, `verification_status` and `answer_status` are separate fields with validated invariants | "some text exists" was read as "the task succeeded" | [f2-truthful-answers](docs/fixes/f2-truthful-answers.md) |
| D5 | Tool output models (`Tool.Output`) are the single source of truth for the planner catalogue, plan validation and the router contract check | hand-written output descriptions drifted and the model guessed fields | [f1-planner-output-schema](docs/fixes/f1-planner-output-schema.md) |
| D6 | An invalid plan gets exactly one repair round, then `PLAN_INVALID` | no planner loops | [f1-planner-output-schema](docs/fixes/f1-planner-output-schema.md) |
| D7 | A re-plan may add success criteria but cannot drop ones that are still checkable (user-goal preservation) | a re-plan swapped "file renamed" for "another file exists" and ended verified | [f2-truthful-answers](docs/fixes/f2-truthful-answers.md) |
| D8 | Retry classification lives in one place (`utils/errors.py`): retryable / non-retryable / single re-plan | step-level and task-level recovery disagreed | [agent-loop](docs/architecture/agent-loop.md) |
| D9 | A user denial is final; during adaptive recovery of a deterministic step it ends the recovery. A planned adaptive step may continue without the denied action | recovery asked for three more approvals after a denial | [f2-truthful-answers](docs/fixes/f2-truthful-answers.md) |
| D10 | Verification polls with an immediate first check, a 10 s cap (30 s downloads), backoff, and early exits; it is never replaced by LLM judgement | ~30 s per attempt on conditions that could not change | [verification](docs/verification.md) |
| D11 | `PAUSED` is a persisted status entered cooperatively at a gate; status is restored before the gate opens; no PAUSING/RESUMING status | UI must never show running while parked; an in-flight call cannot be interrupted safely | [pause-resume](docs/pause-resume.md) |
| D12 | Search errors are `SEARCH_UNAVAILABLE` / `SEARCH_AUTOMATION_BLOCKED`; provider strings are metadata only; a bot check is never bypassed or retried | behaviour must not depend on provider strings | [search-errors](docs/search-errors.md) |
| D13 | "Official" is a deterministic https-domain check against requested/trusted domains; a model may not assert it | results were called official because they looked authoritative | [search-errors](docs/search-errors.md) |
| D14 | Adaptive loops execute several tool calls per model response in order and stop the batch at the first failure | one model call per click | [agent-efficiency](docs/agent-efficiency.md) |
| D15 | Local persistence for single-user use is SQLite or PostgreSQL through the same repository port; no migration was added for per-row verification metrics (they live in the result JSON) | avoid schema churn at the checkpoint | [verification](docs/verification.md) |

## Decisions from NEXT-A (complex single-user workflows)

Details: [docs/next-a/implementation.md](docs/next-a/implementation.md). None of these changes a decision above; D7 (a re-plan cannot relax
criteria) is extended, not replaced.

| # | Decision | Why |
|---|---|---|
| N1 | The user's explicit goal (named outputs, source counts, prohibitions) is extracted **by code**, stored on the plan, shown to the planner, and evaluated against the real final state. A re-plan cannot change it | criteria the model forgot to write could not be caught; D7 only protected criteria the model had written |
| N2 | A restriction in the request ("do not delete anything") is enforced by the router before assessment and approval, and rejected at plan time. Outcome `DENIED`, code `GOAL_VIOLATION` | a sentence in a prompt is not a control; the user must not be asked to approve what they forbade |
| N3 | A URL is a *source* only if a tool returned it during the task; a saved summary is *grounded* only if its vocabulary comes from text a tool returned | a model can invent a plausible URL or summarise from memory; neither is verifiable by asking the model |
| N4 | `fs_find` requires confirming `context_terms`; a document without them, or with unreadable text, is `ambiguous` and is never acted on | homonyms (Java the island) are indistinguishable by code alone; found when a test classified the island guide as `strong` |
| N5 | `fs_organize` validates the whole batch first, never overwrites or deletes, and is idempotent | a half-moved batch followed by a re-plan must not repeat or destroy anything |
| N6 | `PARTIAL` is an *outcome* (status stays `FAILED`), used only when something changed and a stated requirement holds while another does not; `verified` is never true | keeps the status machine and the release contract; the user still learns what was done |
| N7 | Checkpoints are progress snapshots stored as agent actions, not resume points; no step output or document text is stored | resume after a restart needs durable workers and idempotent re-entry, which is the production-hardening phase |
| N8 | Adaptive recovery sees a failed command's stdout/stderr as fenced untrusted data (not in the goal text); a spent budget (`LIMIT_EXCEEDED`) ends an adaptive step immediately | diagnosis needs evidence; program output is untrusted; a spent budget cannot be negotiated with by asking the model again |

## Decisions from the production-reliability roadmap

Branch `phase/production-reliability-roadmap`, started from `a300c2a` (NEXT-A). Roadmap and status: [PHASE_PLAN.md](PHASE_PLAN.md).

| # | Decision | Why |
|---|---|---|
| R0 | The phases after NEXT-A are renumbered: NEXT-B restart-safe execution, C security hardening, D transactional file operations, E Windows application automation, F evaluation and live validation, G dashboard, H installable product. The earlier labels (B multi-model comparison, C production hardening, D multi-user, E distributed workers, F scalability) are withdrawn; multi-model comparison, multi-user, distributed workers and scalability stay deferred | the single-user product must be reliable first; a provider or model change is an explicit decision, never a way around a failing health check |
| R1 | A task that is unfinished when the process stops becomes `INTERRUPTED` and **never resumes by itself**; the user resumes, restarts or cancels. This replaces "marked `FAILED` at startup" and extends N7 (a checkpoint is still not a resume point: recovery reads the plan, step states, `tasks.durable` and the tool-call records) | the agent acts on the user's own desktop; after a crash only the user knows whether continuing is still wanted. Marking the task FAILED threw away work and hid what had already been done |
| R2 | A side-effecting tool call is recorded **before** it runs (write-ahead). If the record cannot be written the action is not performed | an in-flight action must be visible after a crash; an unrecorded effect cannot be recovered or even reported |
| R3 | An in-flight action is classified from the observable state only (`Tool.reconcile`, read-only): safe to retry, confirmed done, confirmed not done, uncertain. Confirmed-done results are replayed; **uncertain actions always need an approval for that one call**, enforced in the permission engine above policy, trusted operations and grants. No exactly-once claim | a blind retry can move, submit or run something twice; code cannot know whether a click or a command took effect |
| R4 | After a restart: task-scoped grants, pending approvals and approvals not yet acted on are void; refusals, the cancel request and prompt-injection taint are kept | a recovered task must not be more permitted than a new one, and must not forget what the user refused |
| R5 | Cancel and STOP are persisted (`cancel_requested`) before they take effect | a task the user stopped must not come back as resumable because the process died a moment later |
| R6 | A service shutdown *suspends* tasks instead of cancelling them; it uses the same recovery path as a crash | one code path, exercised by every restart, instead of a second "graceful" one that is rarely tested |
| R7 | Durable runner state is one JSON column on the task row, written in the same transaction as the plan and steps; no new tables | the state changes together and must never be half-written; the single-user design has one writer |
| R8 | The browser session is not restored. If remaining steps need an open page, the plan is revised with that fact | a Playwright context cannot be re-attached; pretending the page is still there produces clicks on nothing |

### NEXT-C — security and permission hardening

| # | Decision | Why |
|---|---|---|
| C1 | Every directory traversal uses `walk_confined` / `is_redirect` (`security/paths.py`): a symlink, junction or mount point is never entered and never listed. Cloud placeholder files (OneDrive) are reparse points too but are the file itself and stay visible | `os.path.islink` is False for a junction: `fs_search` walked a junction inside an allowed folder and returned the names of files outside it (and read their text when a content query was given). Found by probing, reproduced by a test |
| C2 | Every `fs_search` match is resolved through `PathPolicy` before it is reported or its content is read | the search could be used as an oracle for the content of credential-looking files that `fs_read` refuses |
| C3 | An approval is bound to the exact call (fingerprint, task, risk, category, resource) and the call is assessed again after the user approves; a changed action is refused with `approval_stale` | the user may decide minutes later; the approved call could by then overwrite a file that did not exist when it was shown |
| C4 | A task grant carries the approved risk and does not cover a riskier call of the same tool and category | "approve for this task" must not grow |
| C5 | Path text is compared in one normal form (`normalize_path_text` collapses repeated separators) | tool results are scanned as JSON, where backslashes are doubled: a Windows path named by injected text never matched the call that used it, so the provenance block did not fire |
| C6 | Event messages, audit reasons and API error bodies are redacted, not only event data and tool results | a failing tool's error text reached the timeline and the `tool_calls` table in clear |
| C7 | The API refuses a non-loopback address unless `SAA_SECURITY__ALLOW_REMOTE_BIND=true` and a configured token; the WebSocket token is accepted in the subprotocol only (`?token=` removed) | the API controls the computer; a URL token ends up in history and logs. The one existing test that used `?token=` now sends the subprotocol and a new test asserts the URL form is refused |
| C8 | An approved command runs inside a Windows Job Object: the tree ends with the call and with the agent; memory and process limits are optional. The runner waits for the process to exit, not for its pipes to close | a child left running by a command kept the output pipe open, so a finished command was reported only when its timeout expired and the child outlived it. A job is lifetime and resource containment, not a sandbox |
| C9 | Each tool is placed in one permission (`permissions/classes.py`), checked against the registry and the documentation by test; decisions stay per call (risk and category from `assess`) | a new tool cannot be added without being placed; "read" tools are proven read-only |

### NEXT-D — transactional file operations

Details: [docs/file-transactions.md](docs/file-transactions.md).

| # | Decision | Why |
|---|---|---|
| T1 | Every change by a filesystem tool is journalled in its own table (`file_journal`, migration 0004) **before** it happens; no journal entry, no change. Without a configured journal the tools change nothing | the write-ahead tool-call record (R2) says an action started, not what it changed or how to take it back. The journal is written by the tools during a call, not with the task row, so it does not belong in `tasks.durable` (R7) |
| T2 | An entry is settled from the observed filesystem (fingerprints), never from what the code intended: `applied`, `failed`, or `pending` with the reason | a tool that returned without error is not proof that the file is as expected; the same rule as verification (D10, ADR 0011) |
| T3 | Nothing an overwrite replaces is destroyed: it is preserved in `<data_dir>/journal/<task>/<entry>/` first. `fs_copy` onto an existing folder replaces it instead of merging; a move or copy onto itself is refused | an overwrite (`unlink`/`rmtree`) could not be undone; a merge cannot be undone exactly; moving a file onto itself with `overwrite=true` deleted it (found while designing NEXT-D, regression test added) |
| T4 | Content is staged next to its target, verified, then put in place with one rename; a cross-volume move is a verified copy before the source is removed | a crash or a short write must never leave a half-written file or lose the source |
| T5 | Undo only from the exact state the agent left (fingerprint match); anything else is left alone and reported. What an undo removes goes to the Recycle Bin. A deletion is not undone (`manual`) | an undo that overwrote the user's later edits would be worse than no undo; restoring from the Recycle Bin programmatically is not reliable |
| T6 | Rollback is started only by the user, for a finished task, and runs each undo as an `fs_undo` call through the router and the permission engine. `fs_undo` is hidden from the planner and refuses to run outside a rollback | "everything that touches the computer goes through the router" holds for undo too; the model must not decide to undo |
| T7 | Rollback is idempotent and resumable: `undoing` is stored before an undo touches anything, and every undo step checks its own precondition. It does not change the task's status or result | a rollback can be stopped (STOP, crash) half-way; continuing must never repeat a step. A rollback is about the files, not a new verdict on the run (D3/D4 stay intact) |

## Explicitly deferred (not decided against, not built)

Multi-user and multi-tenancy, distributed or multi-machine workers, load balancing / autoscaling / Kubernetes, scheduled or recurring
tasks, multi-model benchmarking. See [PHASE_PLAN.md](PHASE_PLAN.md).

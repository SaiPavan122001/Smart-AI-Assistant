# Transactional file operations and rollback (NEXT-D)

What happens when the agent changes files, how a half-finished change is recognised, and how the user can take a finished
task's file changes back. Code: `app/tools/filesystem/journal.py` (journal, staging, verification, undo),
`app/tools/filesystem/tools.py` (`fs_create`, `fs_move`, `fs_copy`, `fs_delete`, `fs_undo`), `app/tools/filesystem/workflow.py`
(`fs_organize`), `app/orchestration/rollback.py`, `TaskRuntime.journal` / `TaskRuntime.rollback`, `app/models/journal.py`.
Migration: `0004_file_journal`.

## What is and is not guaranteed

* Every change the filesystem tools make is **journalled before it happens**. If the journal entry cannot be written, the change is
  not made (same rule as the write-ahead tool-call record, R2).
* **An overwrite destroys nothing.** What `fs_create`, `fs_move` or `fs_copy` replaces is kept in the task's backup folder first.
* New content is **staged and verified** before it takes the place of anything: a file is never half-written, a copy that does not
  match its source is never put in place.
* A finished task's file changes can be **rolled back on the user's request**, newest first. Nothing is ever rolled back by itself.
* A change is only undone while the item is **still exactly as the agent left it**. Anything changed since is left alone and
  reported.
* A rollback is **idempotent and resumable**: running it again does nothing to what is already settled and continues an undo that
  stopped half-way (crash, STOP, error) from the state it finds.
* Not guaranteed: atomicity of a whole task (other programs can change files between the agent's steps), undo of a deletion (it is
  in the Recycle Bin; see below), or undo of files written by anything other than the filesystem tools (terminal commands, browser
  downloads, desktop applications).

## The journal

One row per change in `file_journal` (`app/models/journal.py`), in order (`seq`) within the task:

| Operation | Recorded when | `path` | Kept for undo |
|---|---|---|---|
| `create_file` | `fs_create` writes a new file | the file | its fingerprint |
| `replace_file` | `fs_create` with `overwrite=true` | the file | the previous file (copied to the backup) |
| `create_dir` | any tool creates a folder, one entry per missing folder (`fs_create`, parents of a move/copy target, `fs_organize`) | the folder | nothing |
| `move` | `fs_move`, each file of `fs_organize` (move) | where the item went (`src`: where it came from) | the item's fingerprint; what it replaced, in the backup |
| `copy` | `fs_copy`, each file of `fs_organize` (copy) | the copy | the copy's fingerprint; what it replaced, in the backup |
| `recycle` | `fs_delete` | the deleted item | nothing (it is in the Recycle Bin) |

States: `pending` (written before the change) → `applied` | `failed` | (stays `pending` when the outcome cannot be told);
on rollback `undoing` (written before the undo) → `undone` | `skipped` | `manual`.

**Settled from the files, not from intent.** After the change, `observe` looks at the filesystem: the expected result is there →
`applied`; nothing changed → `failed` (and an unused backup copy is removed); anything else → `pending` with the reason. A tool
that finished without error but whose result cannot be observed fails with `verification_failed`.

**Fingerprints.** A file: `sha256` of its content (above 64 MB: size and modification time). A folder: a hash over its relative paths
and file fingerprints, walked with `walk_confined` (links and junctions are not entered); a folder with more than 5000 files has no
fingerprint and is not undone automatically.

**Backups** are kept under `<data_dir>/journal/<task_id>/<entry_id>/`. They are copies of the user's files: same machine, same
profile, never sent anywhere. They are removed when the change is undone or turns out not to have happened; otherwise they stay
(see Limits).

## Staging and verification

| Operation | How |
|---|---|
| Write a file | written to `<name>.saa-tmp` next to the target, flushed to disk, its hash compared with the content, then one `os.replace`. The target is the old file or the new one, never a partial one |
| Replace a file | the old file is copied to the backup and verified **before** the new content takes its place |
| Copy | copied to a staging name next to the destination, compared with the source (files: size and hash; folders: paths, sizes and fingerprint), then renamed into place. A copy that does not match is removed and nothing is put in place |
| Move | one rename on the same volume. Across volumes: a verified copy is put in place first and only then is the source removed |
| Overwrite by move/copy | what the destination holds is moved to the backup first; then the item is placed |

Behaviour changes from v0.1.0, both intentional:

* `fs_copy` with `overwrite=true` onto an existing **folder** now *replaces* it (the old one goes to the backup). It used to merge into
  it, which could not be undone exactly.
* `fs_move` / `fs_copy` of an item onto **itself** is refused (`invalid_input`). With `overwrite=true` it used to clear the
  destination first, which was the source: the file was lost. Regression test:
  `test_moving_a_file_onto_itself_is_refused_and_destroys_nothing`.

## Undo

`fs_undo` reverses one entry. It is **not available to plans or the model** (`exposed_to_llm=False`, rejected by plan validation)
and refuses to run outside a rollback the user started. It is an ordinary tool otherwise: the router's checks, the permission engine
(permission `undo`, medium risk), the write-ahead record, STOP and the audit all apply. The recorded paths must still be allowed by
the path policy; a path that no longer is stays as it is (`policy_denied`).

| Operation | Undo | Precondition |
|---|---|---|
| `create_file` | the file goes to the Recycle Bin | the file has the agent's fingerprint |
| `replace_file` | the agent's version goes to the Recycle Bin, the backup is put back | the file has the agent's fingerprint |
| `create_dir` | the folder is removed | it is empty (after the entries above it were undone) |
| `move` | the item is moved back; what it replaced is put back | the item has its fingerprint and nothing is at the old place |
| `copy` | the copy goes to the Recycle Bin; what it replaced is put back | the copy has the agent's fingerprint |
| `recycle` | **not undone**: `manual`, "restore it from the Recycle Bin" | — |

Nothing an undo removes is deleted permanently: the agent's own files go to the Recycle Bin.

**Only from the state the agent left.** An `applied` entry is undone only if `observe` shows exactly the post-change state. A file
the user edited afterwards, a folder that now holds the user's files, an item that is no longer there: `skipped`, left as it is,
with the reason. A `pending` entry (the process died during the change) is judged from the files: done → undone; never happened →
`skipped` ("never took effect").

**In steps that check their own precondition.** `undoing` is stored before anything is touched. A move with a backup is undone in
two steps (move back, then restore what it replaced); each runs only if its precondition holds. A rollback that stopped between
them finds the item back at its old place and the backup not yet restored, and does only the second step.

## Rollback

| | API | Effect |
|---|---|---|
| Inspect | `GET /tasks/{id}/journal` | the entries with their states, counts, how many are still open and whether a rollback is possible now. Read-only |
| Roll back | `POST /tasks/{id}/rollback` | undoes every open entry, newest first; returns the journal afterwards, the number of undo calls, their errors and why it stopped early (if it did) |

* Only for a **finished** task (`COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`). An interrupted task is resumed, restarted or
  cancelled first ([restart-recovery](restart-recovery.md)); a cancelled one can then be rolled back.
* Refused while another task runs, while the emergency stop is engaged, during shutdown, or while a rollback is already running.
  It holds the same one-at-a-time slot as a task (D1), so a task submitted meanwhile waits.
* The task's status, result and answer do not change: a rollback is about the files, not a new verdict on the run. Events
  `rollback_started` / `rollback_completed` appear on the task's timeline; each undo is a normal tool call in its history.
* STOP stops it between two undo calls; the call in progress completes or stays `undoing`. Run it again after releasing STOP.
* Calling it again is safe: settled entries are not looked at again; it reports `attempted: 0` when nothing is left.
* After a restart the journal is read from storage; nothing about a rollback is held only in memory.

## Tests

| Requirement | Test |
|---|---|
| Journalled before, settled after | `test_every_change_is_journalled_before_it_happens_and_settled_from_the_files` |
| No journal, no change | `test_a_change_that_cannot_be_journalled_is_not_made` |
| Overwrite keeps the previous content; rollback restores it | `test_an_overwrite_destroys_nothing_and_a_rollback_restores_it` |
| Staging and verification | `test_a_copy_that_does_not_verify_is_not_put_in_place`, `test_a_write_that_fails_half_way_leaves_the_previous_file_intact`, `test_a_move_across_volumes_is_a_verified_copy_then_a_removal`, `test_fingerprints_follow_content_not_names` |
| Same-item regression | `test_moving_a_file_onto_itself_is_refused_and_destroys_nothing` |
| Rollback restores everything | `test_a_rollback_returns_every_item_to_where_it_was`, `test_a_completed_task_is_rolled_back_and_keeps_its_result` |
| Preconditions | `test_a_rollback_leaves_alone_what_changed_after_the_agent_left_it`, `test_undo_is_refused_when_the_path_is_no_longer_allowed` |
| Idempotent | `test_a_rollback_run_twice_does_nothing_the_second_time`, `test_journal_and_rollback_of_a_finished_task` (API) |
| Resumable | `test_a_rollback_that_stopped_half_way_continues_where_it_was`, `test_an_undo_is_recorded_before_it_touches_anything`, `test_stop_during_a_rollback_stops_it_and_a_second_rollback_finishes`, `test_a_rollback_works_after_a_restart` |
| Crash during a change | `test_a_change_left_pending_by_a_crash_is_judged_from_the_files` |
| Partial failure | `test_a_batch_that_failed_half_way_can_be_rolled_back_exactly`, `test_a_failed_task_with_partial_changes_can_be_rolled_back` |
| Deletion | `test_a_deletion_is_left_for_the_user_to_restore_from_the_recycle_bin` |
| Who may undo | `test_undo_runs_only_inside_a_rollback_and_only_on_this_tasks_changes`, `test_a_plan_cannot_use_the_undo_tool`, `test_a_task_that_is_not_finished_cannot_be_rolled_back` |
| Storage | `test_file_journal_is_upserted_and_listed_in_order` (memory, SQLite, PostgreSQL), migration drift tests |
| Dashboard | `frontend/tests/recovery.test.tsx` (rollback timeline) |

Files: `tests/unit/test_file_transactions.py`, `tests/e2e/test_rollback.py`, `tests/integration/test_api_rollback.py`.

Mutation check: switching off the "only from the agent's state" precondition, the backup before an overwrite, the journal-before-change
rule, the `undoing` record before an undo, the copy verification, the rollback-only guard of `fs_undo`, or the same-item refusal each
fails at least one test (7 of 7 caught).

## Validation record (2026-10-12)

Windows 11 (10.0.22631), Python 3.12, Node 22, 8 GB RAM. Run as sequential groups (the all-in-one script runs out of memory on this
machine); each exited 0.

| # | Group | Command | Result |
|---|---|---|---|
| 1 | Format, lint, types | `ruff format` · `ruff check app tests evals validation` · `mypy` | formatted · clean · 0 issues in 151 files |
| 2 | Unit | `pytest tests/unit` | 551 passed, 1 skipped (19 new) |
| 3 | Security | `pytest tests/security` | 240 passed |
| 4 | End to end (scripted model) | `pytest tests/e2e` | 113 passed (6 new) |
| 5a | Storage, migrations, restart, API | `pytest tests/integration/test_restart_durable.py …/test_migrations.py …/test_repository_contract.py …/test_api.py …/test_api_recovery.py …/test_api_rollback.py` | 59 passed, 2 skipped (PostgreSQL included) |
| 5b | Real browser, real server, UI Automation, exposure | `pytest tests/integration/test_browser.py …/test_startup.py …/test_uia_windows.py …/test_api_exposure.py` | 32 passed, 1 skipped |
| 6 | Evaluation suite | `python -m evals.run` | 17/17, 46 model calls (unchanged), 0 security violations, 0 permission bypasses, thresholds met |
| 7 | Frontend | `npm run lint` · `npm run typecheck` · `npm test` · `npm run build` · `npm run test:e2e` | clean · clean · 56 passed (2 new) · OK · 8 passed |

Backend total: **995 passed, 4 skipped, 0 failed** (NEXT-C: 964). Skips as before: symlink privilege, the opt-in visible-Notepad
test, two SQL-only tests on the in-memory variant. No live-model run (the model key's limit still blocks NEXT-F).

## Limits

1. **A deletion is not undone.** `fs_delete` sends the item to the Windows Recycle Bin; a rollback reports it as `manual`. Restoring
   from the Recycle Bin programmatically is not reliable enough to promise.
2. **Only the filesystem tools are journalled.** Files written by an approved terminal command, a browser download or a desktop
   application are not in the journal and are not rolled back.
3. **Backups are not pruned.** Copies of replaced files stay under `<data_dir>/journal/` until the change is undone. They can be
   deleted by hand once a rollback is no longer wanted.
4. **Large items.** Files above 64 MB are fingerprinted by size and modification time, not content. Folders with more than 5000 files
   have no fingerprint: their undo is `manual`.
5. **Check-then-act.** A precondition is checked immediately before each step; another program changing the item in the instant
   between the check and the step is not excluded (the same window as SECURITY.md limitation 15).
6. **A crash can leave a staging file** (`<name>.saa-tmp`) next to a target. The next change or undo of that target removes it.
7. **No dashboard button yet.** The rollback is available through the API; its events show on the task timeline. A control in the
   task view belongs to NEXT-G.

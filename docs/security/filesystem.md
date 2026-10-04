# Filesystem security

All filesystem access — tools, downloads, verification, command arguments and working directories — resolves through one class,
`security/paths.py::PathPolicy`.

## Resolution pipeline

1. **Expand**: aliases `@Desktop`, `@Downloads`, `@Documents`, `@Workspace`, `@Home`; `~`; relative paths are relative to the workspace.
   *Rejected outright*: NUL bytes, `%VAR%` / `$env:` / `${}` syntax, UNC and device paths (`\\server\share`, `\\?\`, `\\.\`),
   NTFS alternate data streams and drive-relative forms (`:` in the path tail), reserved device names (`CON`, `NUL`, `COM1`…),
   trailing dots/spaces.
2. **Canonicalise**: follow symlinks and junctions with `realpath` (for not-yet-existing targets, resolve the deepest existing
   ancestor and re-attach the rest, so a link in the parent chain cannot redirect a new file). Unreadable ancestors are treated as absent;
   containment is then checked.
3. **Contain**: the real path must lie under a configured root (case-insensitive on Windows); the *longest* matching root decides
   whether it is writable or read-only. Operating on a root itself can be forbidden (`delete`/`move` of `@Desktop` is refused).
4. **Block**: protected locations inside/outside roots (`%SystemRoot%`, Program Files, ProgramData, `~\.ssh`, `.aws`, `.azure`, `.gnupg`,
   `.kube`, `.docker`, `AppData`, `.config`, `.claude`, `.git-credentials`) and credential-shaped file names (`.env*`, `id_rsa`,
   `*.pem|key|pfx|p12|kdbx|ppk|jks|keystore`, `credentials*`, `secrets*`, `ntuser.dat*`, browser `Login Data`/`Cookies`, `.netrc`, `.npmrc`, `.pypirc`).
   Explicitly configured roots win over broad default blocks that merely *contain* them (e.g. a workspace under `AppData`).
5. **Writes** additionally reject blocked extensions (`.exe .dll .bat .cmd .ps1 .psm1 .vbs .vbe .msi .scr .reg .lnk .com .hta .sys .cpl .jar`).

## Operation rules

| Tool | Rule |
|---|---|
| `fs_create` | creates parents; refuses to replace unless `overwrite=true`; overwrite of an existing file is **high** risk (approval); atomic write via temp file; size cap |
| `fs_move` / `fs_copy` | destination must exist-check; replacing is **high** risk; cannot move/copy a folder into itself; never touches a root |
| `fs_delete` | always approval; sent to the **Recycle Bin** (recoverable) — there is no permanent-delete fallback; non-empty folders need `recursive=true` and are **critical** (no task-scoped grant) with file count/size shown |
| `fs_read` | text up to a byte cap; PDF text extraction (`pypdf`); binary refused; output is untrusted data |
| `fs_list` / `fs_search` | entry/result/scan caps; skips symlinks, `.git`, `node_modules`, venvs; names are untrusted data |

## Verification is policed too

`verify_*` checks and success criteria resolve paths through the same policy, so a plan cannot use verification to probe outside the roots.

Tests: `unit/test_paths.py` (20 cases incl. junction and symlink escape), `unit/test_fs_tools.py`, `unit/test_vision_and_verification.py`.

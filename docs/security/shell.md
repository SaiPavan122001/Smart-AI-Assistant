# Terminal / shell security

The model has **no shell**. `terminal_execute_safe` runs *one* program invocation, parsed and classified by code
(`security/commands.py`), executed directly as an argv list (`shell=False`), so shell metacharacters have no meaning even if one slipped through.

## Pipeline

```
command text ─▶ tokenize ─▶ deny-pattern scan ─▶ program allowlist + sub-command classification ─▶ executable resolution
             ─▶ argument path validation ─▶ risk/category ─▶ permission engine ─▶ (approval) ─▶ scrubbed run
```

1. **Tokenize.** Quotes group; backslashes are literal (Windows paths). Unquoted `& | ; < > ^ ( ) \`` or newlines, `%…%`, `$(…)`, `${…}`
   and unbalanced quotes ⇒ the command is *unclassifiable* ⇒ denied ("run one simple command at a time").
2. **Deny patterns** (a backstop, not the primary control) over the whole command line: encoded PowerShell (`-enc`), base64 decoding,
   `Invoke-Expression`, `DownloadString/DownloadFile/Invoke-WebRequest/iwr`, `curl`/`wget`, `certutil -urlcache|-decode`,
   `bitsadmin/mshta/rundll32/regsvr32/wmic/cscript/wscript/msbuild/installutil`, recursive deletion (`rm -rf`, `del /s`, `rd /s`,
   `Remove-Item -Recurse`), `format X:`/`diskpart`/`cipher /w`, `bcdedit/vssadmin/wbadmin/dism/sfc/chkdsk`, registry tools and
   `HKLM:`/`HKCU:` writes, persistence (`schtasks`, `sc create`, startup folder, `Run` keys), account/network config (`net user`, `netsh`),
   credential extraction (`mimikatz`, `lsass`, `procdump`, `reg save`, `cmdkey`…), security bypass (`Set-ExecutionPolicy`,
   Defender/firewall tampering, hidden windows), power operations, privilege changes (`takeown/icacls/runas/psexec/sudo`).
   Free-text `git commit -m "…"` messages are excluded from the scan so a message like "fix curl timeout" is not blocked.
3. **Allowlist.** Shell interpreters (`cmd`, `powershell`, `pwsh`, `bash`, `sh`, `wsl`…) are denied (and still pattern-scanned if enabled).
   Known programs get per-sub-command classification; anything else is denied (or *approval-only* if you list it in
   `SAA_TERMINAL__EXTRA_ALLOWED_PROGRAMS`).

| Program | Low (no approval) | Medium | High (approval) |
|---|---|---|---|
| `python`/`py` | `--version` | `-m venv`, `-m ensurepip`, `-m compileall/mypy/ruff/black/…` | `script.py …`, `-m pytest/unittest`, `-c` (once-only), `-m pip install` (once-only, installs) |
| `pip` | `list/show/freeze/check/--version` | | `install/uninstall/download` (once-only); `config/cache` denied |
| `node`/`npm`/`pnpm`/`yarn`/`npx` | `--version`, `ls/list/outdated/view` | `init`, `pkg` | `run/test/start/build` (task-scopable), `install/add/exec/publish`, `npx` (once-only) |
| `git` | `status/log/diff/show/branch/rev-parse/ls-files/blame/describe/remote -v/tag` | `init/add/commit/checkout/switch/stash/merge/mv/apply` | `push/pull/fetch`, `clone`, `reset/clean/rebase/gc`, `restore/rm` (discard) ; global options (`-c`, `--exec-path`), `--upload-pack`, `ext::`, `config` denied |
| `code` | | open files/folders | `--install-extension`, `--user-data-dir`, `--remote`, `--tunnel` denied |
| `pytest`, `ruff`, `mypy`, `black`, `tsc`, `eslint`, `prettier` | | static checks | `pytest` (runs code) |
| `where`, `whoami`, `hostname`, `ver`, `systeminfo`, `tree` | ✔ | | |

4. **Executable resolution.** The program is resolved to an absolute path via a PATH containing only absolute entries and never the
   working directory (also `NoDefaultCurrentDirectoryInExePath=1`). Running executables from the workspace is denied. For `.cmd/.bat`
   launchers (e.g. `npm.cmd`) arguments containing `& | < > ^ % " ! ( )` are denied (batch-argument injection).
5. **Argument validation.** Any argument that looks like a path (also `--flag=path`) must resolve inside the allowed roots; the working
   directory must be inside the allowed roots.
6. **Execution.** `asyncio.create_subprocess_exec`, stdin closed, new process group, **allowlisted environment only** (no API keys/tokens;
   `*KEY*|*TOKEN*|*SECRET*|*PASSWORD*` never inherited), timeout (default 60 s, max 600 s) with process-tree kill, output capped
   (64 KiB), cancellation kills the tree. Non-zero exit is a structured `command_failed` error carrying the stderr/stdout tail.
7. **Risk and approval.** See the table; arbitrary code (`-c`), installs and anything unclassified are *once-only* approvals;
   project code (`python main.py`, `npm test`) can be approved once *for the task*. Output is untrusted data and is injection-scanned.

`terminal_query` is the strictly read-only subset; anything else is rejected with "use terminal_execute_safe".

Tests: `security/test_commands.py` (86 adversarial/classification cases), `unit/test_terminal_tools.py` (approval flow, failure,
timeout kills the process tree, output cap, env scrubbing, cwd policy).

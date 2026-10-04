"""Command parsing, classification and validation for the controlled terminal layer.

Pipeline: tokenize -> deny-pattern scan -> program/sub-command classification ->
argument path validation. Commands are never run through a shell: they are parsed
into an argv list and executed directly, so shell metacharacters have no meaning,
and any command that *uses* them is rejected as unclassifiable.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.models.enums import RiskLevel
from app.security.paths import PathPolicy
from app.utils.errors import CommandViolation, PathViolation

# --------------------------------------------------------------------------- tokenizer
_META = set("&|;<>`^()\r\n")


def tokenize(command: str) -> list[str]:
    """Split into argv. Quotes group; backslashes are literal (Windows paths).

    Raises ``CommandViolation`` on shell metacharacters outside quotes, environment
    expansion, or unbalanced quotes: such commands cannot be classified safely.
    """
    if not isinstance(command, str) or not command.strip():
        raise CommandViolation("Empty command")
    if len(command) > 4000:
        raise CommandViolation("Command is too long")
    if "\x00" in command:
        raise CommandViolation("Command contains a NUL byte")
    tokens: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    has_token = False
    for ch in command.strip():
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            has_token = True
            continue
        if ch in _META:
            raise CommandViolation(
                f"Shell control character {ch!r} is not allowed; run one simple command at a time",
                details={"char": ch},
            )
        if ch == "%" or (ch == "$" and not cur):
            raise CommandViolation("Environment/variable expansion is not allowed in commands")
        if ch.isspace():
            if has_token or cur:
                tokens.append("".join(cur))
                cur, has_token = [], False
            continue
        cur.append(ch)
        has_token = True
    if quote:
        raise CommandViolation("Unbalanced quotes in command")
    if has_token or cur:
        tokens.append("".join(cur))
    if not tokens:
        raise CommandViolation("Empty command")
    for t in tokens:
        if "$(" in t or "${" in t:
            raise CommandViolation("Command substitution is not allowed")
    return tokens


def normalize_program(token: str) -> str:
    name = os.path.basename(token.replace("\\", "/")).lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


# --------------------------------------------------------------------------- deny rules
# Applied to the whole lowercased command line, regardless of program. These are a
# backstop on top of the allowlist classification, not the primary control.
_DENY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.I), why)
    for p, why in (
        (r"(^|\s)-(enc|encodedcommand|ec)\b(\s|$)", "encoded PowerShell payload"),
        (r"frombase64string|\bbase64\s+-d", "base64-decoded payload"),
        (r"\b(iex|invoke-expression)\b", "dynamic code execution (Invoke-Expression)"),
        (
            r"downloadstring|downloadfile|invoke-webrequest|\biwr\b|invoke-restmethod|\birm\b|start-bitstransfer",
            "download-and-execute pattern",
        ),
        (r"\b(curl|wget)\b", "raw network download (use the browser_download tool)"),
        (r"\bcertutil\b.*(-urlcache|-decode|-encode|-verifyctl)", "certutil download/decode"),
        (
            r"\b(bitsadmin|mshta|rundll32|regsvr32|wmic|cscript|wscript|msbuild|installutil)\b",
            "living-off-the-land binary",
        ),
        (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive|-r)\b", "recursive deletion"),
        (r"\b(del|erase)\b.*\s/[sq]\b|\b(rd|rmdir)\b.*\s/s\b", "recursive deletion"),
        (r"remove-item.*-recurse|\bri\b.*-recurse", "recursive deletion"),
        (
            r"\bformat(\.com|\.exe)?\s+[a-z]:|\b(diskpart|mkfs|fdisk)\b|\bformat-volume\b|cipher\s+/w",
            "disk formatting / wiping",
        ),
        (
            r"\bbcdedit\b|\bvssadmin\b|\bwbadmin\b|\bdism\b|\bsfc\b|\bchkdsk\b",
            "system recovery/boot configuration",
        ),
        (
            r"\breg(\.exe)?\s+(add|delete|import|load|save|unload|copy|restore)\b|\bregedit\b|"
            r"set-itemproperty.*hk(lm|cu)|new-itemproperty.*hk(lm|cu)|hklm:|hkcu:",
            "registry manipulation",
        ),
        (
            r"\bschtasks\b|\bsc(\.exe)?\s+(create|config|start)\b|new-service|register-scheduledtask|"
            r"\\startup\b|currentversion\\run|new-scheduledtask",
            "persistence mechanism",
        ),
        (
            r"\bnet(\.exe)?\s+(user|localgroup|accounts)\b|\bnetsh\b|\bnetstat\s+-b",
            "account/network configuration",
        ),
        (
            r"mimikatz|sekurlsa|lsass|procdump|\bntdsutil\b|\bsam\b.*\bsystem\b|reg\s+save|"
            r"get-credential|cmdkey|vaultcmd|dpapi|\bsecretsdump\b|credential\s*manager",
            "credential extraction",
        ),
        (
            r"set-executionpolicy|-executionpolicy\s+bypass|\bset-mppreference\b|\badd-mppreference\b|"
            r"disable-windowsoptionalfeature|\bamsi\b|tamper|-noprofile\s+-windowstyle\s+hidden|-w\s+hidden|"
            r"windowstyle\s+hidden|netsh\s+advfirewall|disable.*(defender|firewall)",
            "security bypass",
        ),
        (r"\b(shutdown|restart-computer|stop-computer|logoff)\b", "system power operation"),
        (r"\b(takeown|icacls|cacls|runas|psexec|sudo)\b", "privilege/ownership change"),
        (r"\bdd\s+if=|>\s*/dev/|\bchmod\s+-r\s+777\b", "destructive system configuration"),
    )
)

_SHELL_INTERPRETERS = {
    "cmd",
    "powershell",
    "pwsh",
    "bash",
    "sh",
    "zsh",
    "wsl",
    "powershell_ise",
    "conhost",
}

_FLAG_RE = re.compile(r"^(--?[A-Za-z][\w\-]*(=.*)?|/[A-Za-z?]{1,3}(:.*)?)$")


@dataclass
class CommandAnalysis:
    command: str
    argv: list[str]
    program: str
    risk: RiskLevel
    category: str
    reasons: list[str] = field(default_factory=list)
    hard_deny: str | None = None
    scoped_grant_allowed: bool = True
    read_only: bool = False
    resolved_executable: str | None = None
    path_args: list[str] = field(default_factory=list)

    @property
    def is_denied(self) -> bool:
        return self.hard_deny is not None


def _deny(command: str, argv: list[str], program: str, why: str) -> CommandAnalysis:
    return CommandAnalysis(
        command=command, argv=argv, program=program, risk=RiskLevel.CRITICAL, category="blocked",
        hard_deny=why, scoped_grant_allowed=False,
    )  # fmt: skip


# --------------------------------------------------------------------------- classification
Rule = tuple[RiskLevel, str, bool, bool]  # risk, category, scoped_grant_allowed, read_only

_VERSION_ARGS = {"--version", "-v", "-V", "version", "--help", "-h"}


def _classify_python(args: list[str]) -> Rule | str:
    if not args:
        return (
            RiskLevel.HIGH,
            "code_execution",
            False,
            False,
        )  # interactive REPL: pointless/unsafe
    first = args[0]
    if first in {"--version", "-V", "-VV"}:
        return (RiskLevel.LOW, "version_query", True, True)
    if first == "-c":
        return (RiskLevel.HIGH, "code_execution", False, False)
    if first == "-m":
        mod = args[1] if len(args) > 1 else ""
        if mod == "pip":
            return _classify_pip(args[2:])
        if mod in {"pytest", "unittest", "doctest"}:
            return (RiskLevel.HIGH, "code_execution", True, False)
        if mod in {"venv", "ensurepip"}:
            return (RiskLevel.MEDIUM, "project_setup", True, False)
        if mod in {"compileall", "py_compile", "json.tool", "mypy", "ruff", "black", "pyflakes"}:
            return (RiskLevel.MEDIUM, "static_check", True, False)
        if mod in {"http.server", "smtpd", "ftplib", "telnetlib", "webbrowser"}:
            return f"python -m {mod} opens a network service/channel"
        return (RiskLevel.HIGH, "code_execution", False, False)
    if first.startswith("-"):
        return (RiskLevel.HIGH, "code_execution", False, False)
    return (RiskLevel.HIGH, "code_execution", True, False)  # python script.py ...


def _classify_pip(args: list[str]) -> Rule | str:
    sub = args[0] if args else ""
    if sub in {"list", "show", "freeze", "check", "--version", "-V", "help"}:
        return (RiskLevel.LOW, "package_query", True, True)
    if sub in {"install", "uninstall", "download", "wheel"}:
        return (RiskLevel.HIGH, "software_install", False, False)
    if sub in {"config", "cache"}:
        return f"pip {sub} modifies global configuration"
    return (RiskLevel.HIGH, "software_install", False, False)


def _classify_node(args: list[str]) -> Rule | str:
    if not args or args[0] in {"-v", "--version"}:
        return (
            (RiskLevel.LOW, "version_query", True, True)
            if args
            else (RiskLevel.HIGH, "code_execution", False, False)
        )
    if args[0] in {"-e", "--eval", "-p", "--print", "--inspect", "--inspect-brk"}:
        return (RiskLevel.HIGH, "code_execution", False, False)
    return (RiskLevel.HIGH, "code_execution", True, False)


def _classify_npm(args: list[str]) -> Rule | str:
    sub = args[0] if args else ""
    if (
        sub in {"--version", "-v", "ls", "list", "outdated", "view", "help", "config"}
        and sub != "config"
    ):
        return (RiskLevel.LOW, "package_query", True, True)
    if sub in {
        "install",
        "i",
        "ci",
        "add",
        "update",
        "uninstall",
        "rm",
        "remove",
        "link",
        "publish",
        "exec",
        "x",
    }:
        return (RiskLevel.HIGH, "software_install", False, False)
    if sub in {"run", "run-script", "test", "t", "start", "build"}:
        return (RiskLevel.HIGH, "code_execution", True, False)
    if sub in {"init", "pkg"}:
        return (RiskLevel.MEDIUM, "project_setup", True, False)
    return f"npm {sub or '(no subcommand)'} cannot be classified safely"


def _classify_npx(args: list[str]) -> Rule | str:
    return (RiskLevel.HIGH, "software_install", False, False)  # downloads + runs arbitrary packages


_GIT_RO = {
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "rev-parse",
    "ls-files",
    "blame",
    "describe",
    "remote",
    "tag",
}
_GIT_WRITE = {
    "init",
    "add",
    "commit",
    "checkout",
    "switch",
    "restore",
    "stash",
    "merge",
    "mv",
    "rm",
    "apply",
}
_GIT_NET = {"push", "pull", "fetch", "clone"}


def _classify_git(args: list[str]) -> Rule | str:
    if not args:
        return (RiskLevel.LOW, "version_query", True, True)
    if args[0].startswith("-"):
        if args[0] in {"--version", "-v", "--help"}:
            return (RiskLevel.LOW, "version_query", True, True)
        return "git global options (e.g. -c/-C/--exec-path) are not allowed"
    sub = args[0]
    rest = args[1:]
    for a in rest:
        if (
            a.startswith(("--upload-pack", "--receive-pack", "--exec", "--config", "-c"))
            or "ext::" in a
        ):
            return f"git option {a.split('=')[0]} can execute arbitrary programs"
    if sub in _GIT_RO:
        if (
            sub in {"branch", "tag", "remote"}
            and any(not a.startswith("-") for a in rest)
            and sub != "remote"
        ):
            return (RiskLevel.MEDIUM, "vcs_write", True, False)
        if sub == "remote" and rest and rest[0] not in {"-v", "show", "get-url"}:
            return (RiskLevel.HIGH, "vcs_remote_config", False, False)
        return (RiskLevel.LOW, "vcs_query", True, True)
    if sub in _GIT_WRITE:
        if sub == "restore" or (sub == "checkout" and "--" in rest) or sub in {"rm"}:
            return (RiskLevel.HIGH, "vcs_discard", True, False)
        return (RiskLevel.MEDIUM, "vcs_write", True, False)
    if sub in {"reset", "clean", "rebase", "filter-branch", "gc", "prune", "reflog"}:
        return (RiskLevel.HIGH, "vcs_discard", False, False)
    if sub in _GIT_NET:
        if sub == "clone":
            return (RiskLevel.HIGH, "network_download", False, False)
        return (RiskLevel.HIGH, "vcs_network", False, False)
    if sub == "config":
        return "git config modifies configuration (including credential helpers)"
    return f"git {sub} cannot be classified safely"


def _classify_code(args: list[str]) -> Rule | str:
    for a in args:
        if a.startswith(
            (
                "--install-extension",
                "--uninstall-extension",
                "--extensions-dir",
                "--user-data-dir",
                "--enable-proposed-api",
                "--inspect",
                "--remote",
                "--tunnel",
                "serve-web",
            )
        ):
            return f"VS Code option {a.split('=')[0]} is not allowed"
    return (RiskLevel.MEDIUM, "open_application", True, False)


def _classify_pytest(args: list[str]) -> Rule | str:
    return (RiskLevel.HIGH, "code_execution", True, False)


def _static_tool(args: list[str]) -> Rule | str:
    return (RiskLevel.MEDIUM, "static_check", True, False)


def _readonly(args: list[str]) -> Rule | str:
    return (RiskLevel.LOW, "system_query", True, True)


_PROGRAM_RULES = {
    "python": _classify_python, "python3": _classify_python, "py": _classify_python, "pythonw": _classify_python,
    "pip": _classify_pip, "pip3": _classify_pip,
    "node": _classify_node, "npm": _classify_npm, "npx": _classify_npx, "pnpm": _classify_npm, "yarn": _classify_npm,
    "git": _classify_git, "code": _classify_code, "code-insiders": _classify_code,
    "pytest": _classify_pytest, "ruff": _static_tool, "mypy": _static_tool, "black": _static_tool,
    "tsc": _static_tool, "eslint": _static_tool, "prettier": _static_tool,
    "where": _readonly, "whoami": _readonly, "hostname": _readonly, "ver": _readonly, "systeminfo": _readonly,
    "tree": _readonly,
}  # fmt: skip

READONLY_QUERY_PROGRAMS = frozenset(
    {
        "where",
        "whoami",
        "hostname",
        "ver",
        "systeminfo",
        "git",
        "pip",
        "pip3",
        "python",
        "python3",
        "py",
        "node",
        "npm",
    }
)


def _strip_message_values(program: str, argv: list[str]) -> list[str]:
    """Free-text commit messages must not trigger content-based deny patterns."""
    if program != "git":
        return argv
    out: list[str] = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in {"-m", "--message", "-am"}:
            skip = True
            continue
        if a.startswith("--message="):
            continue
        out.append(a)
    return out


def _looks_like_path(arg: str) -> bool:
    if _FLAG_RE.match(arg) and "/" not in arg.split("=", 1)[-1] and "\\" not in arg:
        return False
    if arg.startswith(("http://", "https://")):
        return False
    return (
        any(c in arg for c in ("\\", "/"))
        or arg.startswith(".")
        or (len(arg) > 1 and arg[1] == ":")
    )


def analyze_command(
    command: str,
    *,
    path_policy: PathPolicy,
    cwd: Path,
    allow_shell_interpreters: bool = False,
    extra_allowed_programs: list[str] | None = None,
) -> CommandAnalysis:
    """Parse + classify. Never raises for *policy* outcomes: those return a denied analysis."""
    try:
        argv = tokenize(command)
    except CommandViolation as exc:
        return _deny(command, [], "", exc.message)
    program = normalize_program(argv[0])
    args = argv[1:]
    scan_argv = _strip_message_values(program, argv)
    lowered = " ".join(scan_argv).lower()

    for pattern, why in _DENY_PATTERNS:
        if pattern.search(lowered):
            return _deny(command, argv, program, f"Blocked pattern: {why}")

    if program in _SHELL_INTERPRETERS:
        if not allow_shell_interpreters:
            return _deny(
                command, argv, program,
                f"Shell interpreter '{program}' is disabled; run the target program directly",
            )  # fmt: skip
        rule: Rule | str = (RiskLevel.CRITICAL, "shell_interpreter", False, False)
    elif program in _PROGRAM_RULES:
        rule = _PROGRAM_RULES[program](args)
    elif program in {normalize_program(p) for p in (extra_allowed_programs or [])}:
        rule = (RiskLevel.HIGH, "unknown_program", False, False)
    else:
        return _deny(
            command, argv, program,
            f"Program '{program}' is not on the allowlist. Add it to terminal.extra_allowed_programs "
            "to permit it with explicit approval.",
        )  # fmt: skip

    if isinstance(rule, str):
        return _deny(command, argv, program, rule)
    risk, category, scoped, read_only = rule
    analysis = CommandAnalysis(
        command=command, argv=argv, program=program, risk=risk, category=category,
        scoped_grant_allowed=scoped, read_only=read_only,
    )  # fmt: skip

    # --- executable resolution (never let CreateProcess search the cwd) ---
    if any(sep in argv[0] for sep in ("\\", "/")):
        try:
            rp = path_policy.resolve(argv[0], must_exist=True, cwd=cwd)
        except PathViolation as exc:
            analysis.hard_deny = f"Executable path rejected: {exc.message}"
            return analysis
        analysis.hard_deny = (
            f"Running executables from inside the workspace ({rp.path}) is not allowed; "
            "invoke an allowlisted program instead"
        )
        return analysis
    exe = shutil.which(argv[0], path=_safe_path())
    if exe is None:
        analysis.hard_deny = f"Program '{argv[0]}' was not found on PATH"
        return analysis
    analysis.resolved_executable = exe

    # --- argument path validation ---
    for arg in args:
        value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
        if _looks_like_path(value):
            analysis.path_args.append(value)
            try:
                path_policy.resolve(value, cwd=cwd)
            except PathViolation as exc:
                analysis.hard_deny = f"Argument path rejected: {exc.message}"
                return analysis

    if risk >= RiskLevel.HIGH:
        analysis.reasons.append(_REASONS.get(category, "Executes code or changes the system"))
    return analysis


_REASONS = {
    "code_execution": "Runs code on this computer with your user privileges",
    "software_install": "Installs or downloads software/packages that can run code",
    "network_download": "Downloads content from the network into your files",
    "vcs_network": "Communicates with a remote repository",
    "vcs_discard": "May permanently discard uncommitted work",
    "vcs_remote_config": "Changes repository remotes",
    "shell_interpreter": "Runs an unrestricted shell",
    "unknown_program": "Runs a program that is not part of the built-in safe set",
}


def _safe_path() -> str | None:
    """PATH without relative entries and without the current directory."""
    entries = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and os.path.isabs(p)]
    return os.pathsep.join(entries) or None


_ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "LANG",
    "HOME", "TERM", "COMPUTERNAME", "COMMONPROGRAMFILES", "NODE_PATH", "VIRTUAL_ENV",
}  # fmt: skip


def build_environment() -> dict[str, str]:
    """Minimal, secret-free environment for child processes."""
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOW}
    env["PATH"] = _safe_path() or env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    env["CI"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env

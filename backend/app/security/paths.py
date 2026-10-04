"""Filesystem path policy: normalization, allowlists, blocklists, traversal prevention.

All filesystem access in the system resolves paths through ``PathPolicy``. The
check runs at *assessment* time (so the user sees an accurate approval) and again
at *execution* time (defence against races and symlink swaps).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.utils.errors import ErrorCode, PathViolation

_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}  # fmt: skip
_SENSITIVE_NAME_PATTERNS = (
    re.compile(r"^\.env(\..*)?$", re.I),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", re.I),
    re.compile(r".*\.(pem|key|pfx|p12|kdbx|ppk|jks|keystore)$", re.I),
    re.compile(r"^(credentials|secrets?)(\..*)?$", re.I),
    re.compile(r"^(ntuser\.dat.*|sam|security|system|software)$", re.I),
    re.compile(r"^(login data|cookies|web data|local state)$", re.I),  # browser profiles
    re.compile(r"^(\.git-credentials|\.netrc|\.npmrc|\.pypirc|\.aws|\.ssh)$", re.I),
)


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _is_within(child: str, root: str) -> bool:
    child_n, root_n = _norm(child), _norm(root)
    if child_n == root_n:
        return True
    sep = os.sep
    return child_n.startswith(root_n.rstrip(sep) + sep)


@dataclass(frozen=True)
class ResolvedPath:
    path: Path  # fully resolved (symlinks/junctions followed)
    root: Path  # allowed root containing it
    read_only: bool


def default_blocked_paths() -> list[Path]:
    """Protected system locations and credential stores (never accessible)."""
    paths: list[Path] = []
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(var)
        if v:
            paths.append(Path(v))
    home = Path(os.path.expanduser("~"))
    for sub in (
        ".ssh",
        ".aws",
        ".azure",
        ".gnupg",
        ".kube",
        ".docker",
        "AppData",
        ".config",
        ".claude",
    ):
        paths.append(home / sub)
    paths.append(home / ".git-credentials")
    if os.name != "nt":
        paths += [
            Path("/etc"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/var"),
            Path("/proc"),
        ]
    return paths


class PathPolicy:
    def __init__(
        self,
        *,
        allowed_roots: list[Path],
        read_only_roots: list[Path] | None = None,
        blocked_paths: list[Path] | None = None,
        workspace: Path | None = None,
        blocked_write_extensions: tuple[str, ...] = (),
        aliases: dict[str, Path] | None = None,
    ) -> None:
        # Roots are canonicalised once, following symlinks.
        self._rw = [self._canon(r) for r in allowed_roots]
        self._ro = [self._canon(r) for r in (read_only_roots or [])]
        extra = [
            self._canon(b)
            for b in (blocked_paths if blocked_paths is not None else default_blocked_paths())
        ]
        # Explicitly allowed roots always win over broad default blocks they happen to sit under
        # (e.g. a workspace inside AppData); only *deeper* blocks inside a root are enforced.
        self._blocked = [
            b for b in extra if not any(_is_within(str(r), str(b)) for r in self._rw + self._ro)
        ]
        self.workspace = (
            self._canon(workspace) if workspace else (self._rw[0] if self._rw else Path.cwd())
        )
        self._blocked_ext = tuple(e.lower() for e in blocked_write_extensions)
        home = Path(os.path.expanduser("~"))
        self.aliases: dict[str, Path] = {
            "desktop": home / "Desktop",
            "downloads": home / "Downloads",
            "documents": home / "Documents",
            "home": home,
            "workspace": self.workspace,
        }
        # A configured root named Desktop/Downloads/Documents *is* that alias (e.g. OneDrive-redirected folders).
        for root in self._rw + self._ro:
            if root.name.lower() in ("desktop", "downloads", "documents"):
                self.aliases[root.name.lower()] = root
        self.aliases.update(aliases or {})

    @staticmethod
    def _canon(p: Path) -> Path:
        return Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(p)))))

    @property
    def roots(self) -> list[Path]:
        return [*self._rw, *self._ro]

    @property
    def writable_roots(self) -> list[Path]:
        return list(self._rw)

    # ------------------------------------------------------------------ resolve
    def expand(self, raw: str, *, cwd: Path | None = None) -> Path:
        """Turn user/LLM supplied text into an absolute, *unchecked* path."""
        if not isinstance(raw, str) or not raw.strip():
            raise PathViolation("Path must be a non-empty string")
        text = raw.strip().strip('"')
        if "\x00" in text:
            raise PathViolation("Path contains a NUL byte")
        if "%" in text or "$env:" in text.lower() or "${" in text:
            raise PathViolation("Environment-variable syntax is not allowed in paths")
        low = text.replace("\\", "/")
        if low.startswith(("//", "/??/")) or text.startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
            raise PathViolation("UNC / device paths are not allowed")
        if text.startswith("@"):
            name, _, rest = text[1:].replace("\\", "/").partition("/")
            base = self.aliases.get(name.lower())
            if base is None:
                raise PathViolation(f"Unknown path alias '@{name}'")
            return Path(os.path.abspath(os.path.join(str(base), rest))) if rest else Path(base)
        if text.startswith("~"):
            text = os.path.expanduser(text)
        p = Path(text)
        if not p.is_absolute():
            p = (cwd or self.workspace) / p
        # reject NTFS alternate data streams and drive-relative forms like "C:foo"
        tail = str(p)[2:] if os.name == "nt" and len(str(p)) > 2 and str(p)[1] == ":" else str(p)
        if ":" in tail:
            raise PathViolation("Alternate data streams / ':' in paths are not allowed")
        return Path(os.path.abspath(str(p)))

    def resolve(
        self,
        raw: str,
        *,
        write: bool = False,
        must_exist: bool = False,
        cwd: Path | None = None,
        allow_root_itself: bool = True,
    ) -> ResolvedPath:
        expanded = self.expand(raw, cwd=cwd)
        # Follow symlinks/junctions. For paths that do not exist yet, resolve the deepest
        # existing ancestor and re-attach the remainder (so we cannot be tricked via a link
        # in the parent chain).
        real = self._realpath_lenient(expanded)

        for part in expanded.parts[1:]:
            stem = part.rstrip(". ").split(".")[0].lower()
            if stem in _RESERVED_NAMES:
                raise PathViolation(f"Reserved device name '{part}' is not allowed")
            if part != part.rstrip(". ") and os.name == "nt":
                raise PathViolation("Path components may not end with '.' or space")

        root = self._containing_root(real)
        if root is None:
            raise PathViolation(
                f"Path '{expanded}' is outside the allowed folders: "
                + ", ".join(str(r) for r in self.roots)
            )
        resolved_root, is_ro = root
        if not allow_root_itself and _norm(real) == _norm(resolved_root):
            raise PathViolation("Operating on an allowed root folder itself is not permitted")
        for blocked in self._blocked:
            if _is_within(str(real), str(blocked)) or _is_within(str(expanded), str(blocked)):
                raise PathViolation(f"Path is in a protected location: {blocked}")
        name = real.name
        if any(pat.match(name) for pat in _SENSITIVE_NAME_PATTERNS):
            raise PathViolation(f"'{name}' looks like a credential/system file and is blocked")
        if write:
            if is_ro:
                raise PathViolation(f"'{resolved_root}' is read-only")
            if real.suffix.lower() in self._blocked_ext:
                raise PathViolation(
                    f"Writing '{real.suffix}' files is blocked (executable/script type)"
                )
        if must_exist and not real.exists():
            raise PathViolation(f"Path does not exist: {real}", code=ErrorCode.NOT_FOUND)
        return ResolvedPath(path=real, root=resolved_root, read_only=is_ro)

    # ------------------------------------------------------------------ internals
    def _containing_root(self, real: Path) -> tuple[Path, bool] | None:
        best: tuple[Path, bool] | None = None
        for r in self._rw:
            if _is_within(str(real), str(r)) and (best is None or len(str(r)) > len(str(best[0]))):
                best = (r, False)
        for r in self._ro:
            if _is_within(str(real), str(r)) and (best is None or len(str(r)) > len(str(best[0]))):
                best = (r, True)
        return best

    @staticmethod
    def _realpath_lenient(p: Path) -> Path:
        existing = p
        tail: list[str] = []
        while existing != existing.parent:
            try:
                if existing.exists():
                    break
            except OSError:
                pass  # unreadable (e.g. access denied): treat as absent; containment is checked next
            tail.append(existing.name)
            existing = existing.parent
        try:
            real = Path(os.path.realpath(str(existing)))
        except OSError as exc:
            raise PathViolation("Path cannot be resolved") from exc
        for part in reversed(tail):
            real = real / part
        return real

    def is_extension_blocked(self, path: Path) -> bool:
        return path.suffix.lower() in self._blocked_ext

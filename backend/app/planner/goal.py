"""Deterministic extraction of the explicit goal from the user's own words.

No model is involved: the same request always yields the same ``GoalSpec``. It captures the
requirements a plan most often drops (a named output file, a requested number of sources, "save a
summary") and the restrictions a plan must never violate ("do not delete anything"). Anything that
cannot be recognised with confidence is left to the planner's own success criteria; the extractor
prefers to miss a requirement over inventing one.
"""

from __future__ import annotations

import re

from app.models.goal import GoalSpec, Prohibition, Requirement, RequirementKind

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10,
}  # fmt: skip
_COUNT_NOUNS = (
    "resources|sources|links|articles|tutorials|websites|web pages|pages|references|guides|"
    "results|posts|videos|papers"
)
_COUNT = re.compile(
    rf"\b(\d{{1,2}}|{'|'.join(_NUMBER_WORDS)})\s+(?:[A-Za-z-]+\s+){{0,2}}?(?:{_COUNT_NOUNS})\b",
    re.I,
)
_URL_WORD = re.compile(r"\b(urls?|links?|web ?addresses)\b", re.I)
# "the URL of each", "title and URL", "retrieve ... the URL": the answer or file must carry a source URL.
_URL_ASKED = re.compile(
    r"\burls? of (?:each|every|the|it|them)\b|\btitles? and (?:the )?(?:page )?urls?\b"
    r"|\b(?:retrieve|identify|report|return|give me|tell me|list|extract|find|provide)\b[^.!?]{0,40}\burls?\b",
    re.I,
)
_DOMAIN = re.compile(
    r"\b(?:from|on|at|via|within|only)\s+(?:the\s+)?(?:website\s+|site\s+)?"
    r"((?:[a-z0-9-]+\.)+(?:com|org|io|dev|net|edu|gov|ai|co|app|info|tech))\b",
    re.I,
)
_FILE_EXT = "md|markdown|txt|json|csv|html|xml|ya?ml|py|docx?|xlsx|pdf|rtf|log"
_FILE_TOKEN = re.compile(rf"^[^\s\"'<>|*?]+\.(?:{_FILE_EXT})$", re.I)
_QUOTED = re.compile(r"[\"'`]([^\"'`\n]{1,200}?\.(?:" + _FILE_EXT + r"))[\"'`]", re.I)
_WRITE_VERB = re.compile(
    r"\b(save|saved|saving|write|writing|store|storing|export|create|creating|produce|generate|"
    r"output|put|record)\b",
    re.I,
)
_SAVE_CONTENT = re.compile(
    r"\b(save|write|store|export)\b[^.!?]{0,60}?\b(summary|summaries|report|comparison|notes|"
    r"results?|analysis|findings|list|overview|answer)\b"
    r"|\b(save|write|store|export)\b[^.!?]{0,30}?\b(it|them|that|this)\b[^.!?]{0,40}?"
    r"\b(workspace|folder|directory|file|disk|desktop|documents|downloads)\b",
    re.I,
)
_SUMMARISE = re.compile(
    r"\b(summar(?:y|ies|ise|ize|ised|ized|ising|izing)|compar(?:e|ison)|differences?)\b", re.I
)
_NEG = r"(?:do not|don't|dont|never|must not|mustn't|should not|shouldn't|without|no need to)"
# The restriction's reach: stops at punctuation and at a conjunction ("don't delete X, and move Y").
_GAP = r"(?:(?!\b(?:and|but|then|just|only|also|instead)\b)[^.;!?,]){0,40}?"
_PROHIBITIONS: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("delete", r"(delet\w*|remov\w*|eras\w*|trash\w*|discard\w*)", ("fs_delete",), False),
    ("overwrite", r"(overwrit\w*|replac\w*|clobber\w*)", (), True),
    ("move", r"(mov(?:e|ing)\b|reloc\w*)", ("fs_move",), False),
    (
        "run commands",
        r"(run|execut\w*)\s+(?:any\s+)?(?:commands?|code|scripts?|programs?)",
        ("terminal_execute_safe",),
        False,
    ),
)


def _number(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token.lower()]


def _sentence_before(text: str, start: int) -> str:
    head = text[:start]
    cut = max(head.rfind(c) for c in ".!?;\n")
    return head[cut + 1 :][-90:]


def _file_candidates(text: str) -> list[tuple[str, int]]:
    """(file reference as written, position) in order of appearance, quotes honoured."""
    found: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    for m in _QUOTED.finditer(text):
        found.append((m.group(1).strip(), m.start()))
        spans.append((m.start(), m.end()))
    for m in re.finditer(r"\S+", text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        token = m.group(0).strip("\"'`()[]{}<>,;:!?")
        token = token.rstrip(".")
        if _FILE_TOKEN.match(token) and not token.lower().startswith(("http://", "https://")):
            found.append((token, m.start()))
    return sorted(found, key=lambda t: t[1])


def extract_goal(instruction: str) -> GoalSpec:
    text = " ".join(instruction.split())
    goal = GoalSpec(request=instruction)
    n = 0

    def rid(prefix: str) -> str:
        nonlocal n
        n += 1
        return f"{prefix}{n}"

    # ---- restrictions (enforced by the router, listed in the planner prompt)
    for label, pattern, tools, overwrite in _PROHIBITIONS:
        m = re.search(rf"\b{_NEG}\b{_GAP}\b{pattern}", text, re.I)
        if m:
            sentence = text[max(0, m.start() - 10) : m.end() + 40].strip()
            goal.prohibitions.append(
                Prohibition(
                    id=rid("p"),
                    text=f"do not {label}: “{sentence}”",
                    tools=list(tools),
                    forbid_overwrite=overwrite,
                )  # fmt: skip
            )

    # ---- named output files
    artifacts: list[Requirement] = []
    seen: set[str] = set()
    for ref, pos in _file_candidates(text):
        if not _WRITE_VERB.search(_sentence_before(text, pos)):
            continue  # an input file ("read notes.txt"), not something to produce
        key = ref.lower()
        if key in seen:
            continue
        seen.add(key)
        is_path = bool(re.search(r"[\\/]|^@|^[A-Za-z]:", ref))
        artifacts.append(
            Requirement(
                id=rid("r"),
                kind=RequirementKind.ARTIFACT,
                text=f"save the output as {ref}",
                path=ref if is_path else None,
                name=None if is_path else ref,
            )  # fmt: skip
        )
    if not artifacts and _SAVE_CONTENT.search(text):
        artifacts.append(
            Requirement(
                id=rid("r"),
                kind=RequirementKind.ARTIFACT,
                text="save the requested content to a file (not only in the chat reply)",
            )  # fmt: skip
        )
    goal.requirements.extend(artifacts)

    # ---- sources with URLs: how many, and from where
    count = _COUNT.search(text)
    domains = [d.lower() for d in dict.fromkeys(m.group(1) for m in _DOMAIN.finditer(text))]
    wanted = 0
    if count and _URL_WORD.search(text):
        wanted = _number(count.group(1))
    if wanted < 2 and (_URL_ASKED.search(text) or (domains and _URL_WORD.search(text))):
        wanted = 1
    if wanted >= 1:
        goal.requirements.append(
            Requirement(
                id=rid("r"),
                kind=RequirementKind.MIN_URLS,
                count=wanted,
                domains=domains,
                where="artifact" if artifacts else "answer",
                text=f"provide {wanted} distinct source URL{'s' if wanted > 1 else ''}"
                + (f" on {', '.join(domains)}" if domains else ""),
            )  # fmt: skip
        )

    # ---- a saved summary must come from what was actually read
    if artifacts and _SUMMARISE.search(text):
        goal.requirements.append(
            Requirement(
                id=rid("r"),
                kind=RequirementKind.GROUNDED_SUMMARY,
                text="the saved summary must be based on the content that was actually read",
                name=artifacts[0].name,
                path=artifacts[0].path,
            )  # fmt: skip
        )
    return goal


def goal_prompt_block(goal: GoalSpec) -> str:
    """The deterministic goal, for the planner. Empty when there is nothing to say."""
    if goal.is_empty():
        return ""
    lines = [
        "<explicit_goal>",
        "Extracted from the request by code. The task is NOT complete unless every requirement holds in "
        "the final state, and nothing forbidden may be planned. A revised plan cannot change this list.",
    ]
    lines += [f"- requirement {r.id}: {r.text}" for r in goal.requirements]
    for p in goal.prohibitions:
        extra = f" (tools: {', '.join(p.tools)})" if p.tools else ""
        extra += " (no overwrite=true anywhere)" if p.forbid_overwrite else ""
        lines.append(f"- FORBIDDEN {p.id}: {p.text}{extra}")
    lines.append("</explicit_goal>")
    return "\n".join(lines)

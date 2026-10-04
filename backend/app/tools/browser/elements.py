"""DOM interactive-element extraction and deterministic target scoring."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

# Runs in the page. Collects visible interactive elements (piercing open shadow roots),
# stamps each with a data-saa-ref attribute and returns compact descriptors.
EXTRACT_JS = r"""
(startIndex) => {
  const out = [];
  let n = startIndex;
  const SEL = 'a[href],button,input,select,textarea,summary,[role],[tabindex]:not([tabindex="-1"]),[onclick],[contenteditable=""],[contenteditable="true"]';
  const implicitRole = (el) => {
    const t = el.tagName.toLowerCase();
    if (el.getAttribute('role')) return el.getAttribute('role');
    if (t === 'a') return 'link';
    if (t === 'button' || t === 'summary') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button','submit','reset','image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'range') return 'slider';
      if (ty === 'file') return 'filebutton';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  };
  const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => { const e = document.getElementById(id); return e ? textOf(e) : ''; }).join(' ').trim();
      if (t) return t;
    }
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map(textOf).join(' ').trim();
      if (t) return t;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button','submit','reset'].includes(ty)) return (el.value || ty).trim();
      if (ty === 'image') return (el.alt || el.title || 'image button').trim();
    }
    const txt = textOf(el);
    if (txt) return txt.slice(0, 120);
    return (el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('name') || '').trim().slice(0, 120);
  };
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const walk = (root) => {
    root.querySelectorAll(SEL).forEach(el => {
      if (out.length >= 250) return;
      if (!visible(el)) return;
      const tag = el.tagName.toLowerCase();
      const ty = (el.getAttribute('type') || '').toLowerCase();
      const r = el.getBoundingClientRect();
      const ref = 'e' + (n++);
      el.setAttribute('data-saa-ref', ref);
      const sensitive = ty === 'password' || /pass|secret|token|cvv|card|ssn/i.test((el.getAttribute('name')||'') + (el.getAttribute('autocomplete')||''));
      out.push({
        ref, tag, role: implicitRole(el), name: nameOf(el), type: ty || null,
        href: tag === 'a' ? el.href : null,
        value: (sensitive || tag === 'textarea' && el.value.length > 200) ? null : (('value' in el) ? String(el.value).slice(0, 100) : null),
        checked: ('checked' in el && (ty === 'checkbox' || ty === 'radio')) ? !!el.checked : null,
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
        sensitive, placeholder: el.getAttribute('placeholder') || null,
        in_viewport: r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth,
        options: tag === 'select' ? Array.from(el.options).slice(0, 30).map(o => o.label || o.text) : null,
      });
    });
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot); });
  };
  walk(document);
  return out;
}
"""


class ElementInfo(BaseModel):
    ref: str
    tag: str
    role: str
    name: str = ""
    type: str | None = None
    href: str | None = None
    value: str | None = None
    checked: bool | None = None
    disabled: bool = False
    sensitive: bool = False
    placeholder: str | None = None
    in_viewport: bool = True
    options: list[str] | None = None
    score: float = Field(default=0.0, exclude=True)

    def brief(self) -> str:
        extra = []
        if self.href:
            extra.append(f"href={self.href[:80]}")
        if self.type and self.tag == "input":
            extra.append(f"type={self.type}")
        if self.disabled:
            extra.append("disabled")
        if self.checked is not None:
            extra.append("checked" if self.checked else "unchecked")
        name = self.name or self.placeholder or ""
        return f'{self.role} "{name[:80]}" ref={self.ref}' + (
            f" ({', '.join(extra)})" if extra else ""
        )


_WORD = re.compile(r"[a-z0-9]+")
ROLE_ALIASES = {
    "input": "textbox", "text": "textbox", "field": "textbox", "searchbox": "textbox", "textarea": "textbox",
    "dropdown": "combobox", "select": "combobox", "listbox": "combobox", "a": "link", "btn": "button",
    "submit": "button", "tickbox": "checkbox",
}  # fmt: skip


def _tokens(s: str) -> list[str]:
    return _WORD.findall(s.lower())


def normalize_role(role: str | None) -> str | None:
    if not role:
        return None
    r = role.lower().strip()
    return ROLE_ALIASES.get(r, r)


def score_elements(
    elements: list[ElementInfo], query: str, role: str | None = None
) -> list[ElementInfo]:
    """Deterministic ranking of elements for a natural-language query (no LLM involved)."""
    q = query.lower().strip()
    qt = _tokens(q)
    want_role = normalize_role(role)
    scored: list[ElementInfo] = []
    for el in elements:
        name = (el.name or "").lower()
        hay = " ".join(
            x
            for x in (
                name,
                (el.placeholder or "").lower(),
                (el.value or "").lower() if el.role == "button" else "",
                (el.href or "").lower(),
            )
            if x
        )
        s = 0.0
        if q and name == q:
            s = 100.0
        elif q and name.startswith(q):
            s = 85.0
        elif q and q in name:
            s = 70.0 + 10.0 * (len(q) / max(len(name), 1))
        elif qt:
            nt = set(_tokens(hay))
            hits = sum(1 for t in qt if t in nt or any(t in w for w in nt))
            if hits:
                s = 20.0 + 40.0 * hits / len(qt)
        if want_role:
            if el.role == want_role or (
                want_role == "textbox" and el.role in {"textbox", "searchbox"}
            ):
                s += 10.0 if s else 25.0
            elif s:
                s -= 15.0
        if s <= 0:
            continue
        if el.disabled:
            s -= 8.0
        if el.in_viewport:
            s += 2.0
        el = el.model_copy(update={"score": round(s, 1)})
        scored.append(el)
    scored.sort(key=lambda e: -e.score)
    return scored


def is_ambiguous(ranked: list[ElementInfo]) -> bool:
    return len(ranked) > 1 and ranked[0].score >= 50 and (ranked[0].score - ranked[1].score) < 5


SENSITIVE_ACTION = re.compile(
    r"\b(buy|purchase|pay|place\s+order|checkout|confirm\s+(order|purchase|payment)|delete|remove\s+account|"
    r"close\s+account|send|transfer|donate|subscribe|install|authorize|allow\s+access|grant|sign\s+out\s+everywhere|"
    r"unsubscribe|publish|post\s+publicly)\b",
    re.I,
)


def element_to_dict(el: ElementInfo) -> dict[str, Any]:
    d = el.model_dump(exclude_none=True, exclude_defaults=False)
    return {k: v for k, v in d.items() if v not in (False, "", None) or k in ("disabled",)}

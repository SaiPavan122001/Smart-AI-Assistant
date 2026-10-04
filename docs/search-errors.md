# Search error contract and official-source verification

## Normalised error codes

| Code (`ErrorCode`, also the wire value) | Raised by | Meaning |
|---|---|---|
| `SEARCH_UNAVAILABLE` | `SearchRouter`, `web_search` | no configured search provider could answer |
| `SEARCH_AUTOMATION_BLOCKED` | browser tools (`browser_open`, `browser_navigate`, `browser_click`, `browser_type` with submit) | the site showed a bot check / CAPTCHA; never solved, never bypassed |

Mapping from the previous names:

| Before | Now |
|---|---|
| `capability_not_available` (from web search) | `SEARCH_UNAVAILABLE` |
| `blocked_by_site` | `SEARCH_AUTOMATION_BLOCKED` |

`capability_not_available` (`ErrorCode.NOT_AVAILABLE`) still exists for other capabilities
(desktop, vision, browser not enabled). `ErrorCode("blocked_by_site")` still loads from records
stored before the rename and maps to `SEARCH_AUTOMATION_BLOCKED`.

Provider-specific detail is metadata only:

* `SEARCH_UNAVAILABLE.details.attempts` — one `{provider, ok, reason}` per provider, where
  `reason` starts with the provider's own kind (`not_configured`, `auth`, `rate_limited`,
  `blocked`, `unavailable`, `empty`).
* `SEARCH_AUTOMATION_BLOCKED.details` — `{url, reason, provider_error: "blocked_by_site"}`.

No code path branches on those strings
(`test_no_behaviour_depends_on_provider_specific_error_strings`).

## Behaviour (unchanged flow, stricter stopping)

```
web_search -> SearchRouter -> providers in configured order (brave, tavily, duckduckgo)
```

* **`SEARCH_UNAVAILABLE`** — one re-plan is allowed (the plan may use another source). Within the
  task the providers are not asked again: a second `web_search` fails immediately with the same
  code and `details.repeated = true`. A second occurrence after the re-plan is final.
* **`SEARCH_AUTOMATION_BLOCKED`** — no refresh, no retry, no browser restart. One re-plan (the
  planner is told to use `web_search`). The blocked page is remembered for the task; requesting
  the same URL again fails immediately without contacting the site. A second block after the
  re-plan is final.

Both codes are in `SINGLE_REPLAN_CODES` (`app/utils/errors.py`), the list the recovery engine and
the runner share.

## "Official" sources

The agent may not call a result official because it looks authoritative. Officialness is a
deterministic domain check in `web_search` (`app/tools/search/tools.py`):

* `web_search` accepts `official_domains` (the publisher's domain(s), supplied by the planner when
  the user asks for the official source) and the deployment may configure
  `search.trusted_domains`.
* A result is `official: true` only if its URL is `https` and its host equals one of those domains
  or is a subdomain of it. Titles and snippets are never consulted. Lookalike hosts
  (`spring.io.evil.example`), plain `http`, bare suffixes (`io`) and shared-hosting suffixes
  (`github.io`, `co.uk`, …) do not qualify.
* Output: `results[].official`, `best` (the verified result if any, else the top result),
  `official_verified`, and `source_note`:
  * "Verified official source (domain spring.io)."
  * "Found a relevant result, but I could not verify that it is the official source."
* The source URL is kept in `artifacts.url`.

Two guards stop a model from asserting officialness on its own:

* Plan validation rejects an `answer_template` whose literal text contains "official"; the planner
  is told to use `{{steps.<id>.data.source_note}}`.
* `guard_officialness` (`app/agent/answer.py`) is applied to every final answer: if the text claims
  "official" and no succeeded step recorded `official_verified: true`, the claim is removed and the
  "could not verify" sentence is put in front.

### Limits

* The rule proves a result is on the stated domain. It does not prove the stated domain is the
  publisher's: that still comes from the request, from configuration, or from the planner's
  knowledge when it fills `official_domains`.
* Page metadata is not inspected.
* The answer guard works on the word "official"; other phrasings ("the authoritative docs") are
  not detected.

## Tests

`backend/tests/unit/test_runtime_hardening.py` (normalisation, legacy records, no retry loop,
blocked page not re-requested, domain verification, non-official result not claimed, planner cannot
declare officialness, injected "official" snippet has no effect), `tests/unit/test_search.py`.

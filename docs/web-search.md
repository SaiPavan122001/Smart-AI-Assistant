# Web search and blocked-page handling

## Why
Typing into Google through an automated browser gets a bot check ("Why did this happen?" / `/sorry/`). Live Test 2 failed exactly that way.
The agent must never solve or bypass a CAPTCHA, so searching is done through a **structured search provider** instead, and the browser
is only used to open the chosen result.

## Components
| Piece | Where | Notes |
|-------|-------|-------|
| `SearchProvider` adapters | `app/search/providers.py` | Brave Search API, Tavily (both need a key) and DuckDuckGo HTML (keyless, best effort). Vendor wire formats live only here. |
| `SearchRouter` | `app/search/router.py` | Tries providers in `SAA_SEARCH__PROVIDERS` order; keyed providers without a key are skipped; every attempt is recorded (`attempts[]`); when all fail the error says why and that no CAPTCHA will be bypassed. |
| `web_search` tool | `app/tools/search/tools.py` | Capability `api`, read-only, LOW risk. Output (`provider, query, results[{title,url,snippet,domain,official}], attempts, best, official_verified, source_note`) is **untrusted external content**: scanned for injection, never an instruction. Result URLs are plain http(s) only (no `file:`/`javascript:`/credentials). |
| Challenge detection | `app/tools/browser/blocks.py`, `BrowserSession.detect_block` | Pure classifier (Google `/sorry`, Cloudflare "Just a moment", "unusual traffic", "I'm not a robot", ...). Text rules apply only to short pages, so articles that mention the phrase are not flagged. |
| `SEARCH_AUTOMATION_BLOCKED` (was `blocked_by_site`) | `ErrorCode`, browser tools | Raised after `browser_open` / `browser_navigate` / `browser_click` / `browser_type(submit)` land on a challenge. Recovery = one replan (never a retry); the planner is told to use `web_search`. |

Keys are read from the secret chain (`BRAVE_API_KEY`, `TAVILY_API_KEY`: environment or Windows Credential Manager) and never appear in settings dumps.

## Behaviour
* "Search for X" -> planner prefers `web_search`, then `browser_open` on the chosen result.
* If a site is operated in the browser and shows a bot check -> `SEARCH_AUTOMATION_BLOCKED` -> replan -> `web_search` -> `browser_open`.
* No provider available -> `SEARCH_UNAVAILABLE`; the providers are not asked again in that task; the task fails honestly with each provider's reason.
* Error contract and official-source verification: [search-errors.md](search-errors.md).

## Answers
Plans may carry `answer_template` (e.g. `The page title is {{steps.s2.data.title}}`), resolved from recorded tool output with no LLM call. A field that was
never produced gives no answer rather than an invented one.

## Verification notes
`browser_text` now checks the page title together with the visible text (a page title is often absent from the body text, e.g. example.com); a `selector`
restricts it to that element. Use `browser_title` for titles.

## Known limits
* DuckDuckGo's HTML endpoint can itself rate-limit or challenge; that is reported as `blocked`, never bypassed. Configure a Brave or Tavily key for reliability.
* The agent does not verify that a search result is "official"; it opens what the provider ranks first and verifies page state, not authority.

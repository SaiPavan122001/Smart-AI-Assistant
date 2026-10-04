async def test_browser_text_counts_the_page_title_but_selector_checks_stay_strict() -> None:
    """Regression (live Test 3): example.com's title 'Example Domain' is NOT in its body text; the plan checked text."""
    from types import SimpleNamespace

    from app.verification.checks import run_checker

    class Sess:
        has_page = True

        async def read(self, *, mode: str, selector: str | None, max_chars: int) -> dict[str, str]:
            return {
                "title": "Example Domain",
                "text": "This domain is for use in documentation examples.",
            }

    ctx = SimpleNamespace(
        task_id="t", services=SimpleNamespace(browser=SimpleNamespace(peek=lambda _t: Sess()))
    )
    ok = await run_checker("browser_text", {"contains": "Example Domain"}, ctx)  # type: ignore[arg-type]
    assert ok.passed
    assert (await run_checker("browser_text", {"contains": "documentation examples"}, ctx)).passed  # type: ignore[arg-type]
    assert not (
        await run_checker("browser_text", {"contains": "Example Domain", "selector": "p"}, ctx)
    ).passed  # type: ignore[arg-type]
    assert not (await run_checker("browser_text", {"contains": "something else"}, ctx)).passed  # type: ignore[arg-type]

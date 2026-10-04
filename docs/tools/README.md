# Tools

* [Generated catalogue](catalog.md) — every tool with parameters, base risk, approval mode, timeout, idempotency and untrusted-output flag. Regenerate with `python -m app.docs_gen`.
* [Tool architecture](../architecture/tools.md) — contract, router pipeline, capabilities, what is deliberately not exposed.
* [Adding a tool](../development/adding-tools.md).

`python -m app tools` prints the same list for the *current* configuration (tools whose backend is unavailable — no browser, no Windows UIA, no vision provider — are not registered).

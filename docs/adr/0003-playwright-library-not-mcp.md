# ADR-0003: Playwright as a library; no MCP layer

**Context.** The brief asks to evaluate whether Playwright's library APIs suffice or MCP is needed.

**Evaluation.** We need: per-task isolated contexts, our own element extraction with stable refs, request routing for SSRF defense, download control,
tab lifecycle events, and tight integration with the permission router and cancellation. These are in-process concerns. An MCP server would add an IPC hop,
a second tool-description surface the model could call *around* our router, and no capability we lack.

**Decision.** Use `playwright.async_api` directly (`BrowserManager`/`BrowserSession`); expose only our typed `browser_*` tools. Installed Chrome/Edge are
used when the bundled Chromium is not present.

**Consequences.** + One enforcement point, lower latency, full control. − Cannot reuse third-party MCP browser servers unchanged; if MCP is wanted later it should be an
*adapter that registers tools into the same registry/router*, never a bypass.

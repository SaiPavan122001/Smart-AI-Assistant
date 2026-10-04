# Tool catalogue

> Generated from the tool registry by `python -m app.docs_gen`. Do not edit by hand.

Every operation the agent can perform is one of these typed tools; the model never gets a shell or raw OS access.
*Base risk* is the tool's default; each call is re-assessed (a delete of a non-empty folder is `critical`, a command that runs project code is `high`, ...). `approval: policy` means the permission engine decides from the assessed risk; `always` always asks.

## Filesystem

| Tool | Base risk | Approval | Timeout | Read-only | Idempotent | Untrusted output |
|---|---|---|---|---|---|---|
| `fs_copy` | medium | policy | 30s | no | no | no |
| `fs_create` | medium | policy | 30s | no | yes | no |
| `fs_delete` | high | policy | 30s | no | no | no |
| `fs_list` | low | policy | 30s | yes | yes | yes |
| `fs_move` | medium | policy | 30s | no | no | no |
| `fs_read` | low | policy | 30s | yes | yes | yes |
| `fs_search` | low | policy | 60s | yes | yes | yes |

### `fs_copy`

Copy a file/folder within allowed roots. Fails if the destination exists unless overwrite=true.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `src` | string | yes |  |
| `dst` | string | yes | Destination file path, or an existing folder to move into |
| `overwrite` | boolean | no |  |

### `fs_create`

Create a folder (kind='dir') or a text file (kind='file', with content) inside an allowed root. Parent folders are created. Existing files are only replaced when overwrite=true.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |
| `kind` | string | no |  |
| `content` | string | no |  |
| `overwrite` | boolean | no |  |

### `fs_delete`

Delete a file or folder (moved to the Recycle Bin, recoverable). Always requires approval.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |
| `recursive` | boolean | no | Required to delete a non-empty folder |

### `fs_list`

List files and folders in an allowed directory (optionally recursive, with glob filter).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Folder to list. Absolute, '@Desktop/..', '@Downloads/..' or relative to the workspace. |
| `recursive` | boolean | no |  |
| `max_depth` | integer | no |  |
| `pattern` | string | no | Optional glob filter on names, e.g. '*.pdf' |

### `fs_move`

Move or rename a file/folder within allowed roots. Fails if the destination exists unless overwrite=true.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `src` | string | yes |  |
| `dst` | string | yes | Destination file path, or an existing folder to move into |
| `overwrite` | boolean | no |  |

### `fs_read`

Read a text file or extract text from a PDF. Content is returned as untrusted data.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |
| `max_chars` | integer | no |  |
| `pages` | string | no | PDF only: page range like '1-3' |

### `fs_search`

Search for files by name pattern, extension and/or text content under an allowed folder.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `root` | string | yes | Folder to search under |
| `name_pattern` | string | no | Glob on file names, e.g. '*java*.pdf' (case-insensitive) |
| `content_query` | string | no | Case-insensitive text to find inside small text files |
| `extensions` | string[] | no | e.g. ['.pdf', '.docx'] |
| `max_results` | integer | no |  |

## Terminal

| Tool | Base risk | Approval | Timeout | Read-only | Idempotent | Untrusted output |
|---|---|---|---|---|---|---|
| `terminal_environment` | low | policy | 30s | yes | yes | no |
| `terminal_execute_safe` | medium | policy | 660s | no | no | yes |
| `terminal_query` | low | policy | 60s | yes | no | yes |

### `terminal_environment`

Describe the execution environment: OS, workspace folder, allowed folders, and which developer tools are installed. Contains no secrets.

_No parameters._

### `terminal_execute_safe`

Run ONE allowlisted program (python, pip, node, npm, git, code, pytest, ...) with arguments in a folder inside an allowed root. No shell: pipes, redirects, &&, ; and %VAR% are rejected. Risky commands (running code, installing packages) require user approval. Non-zero exit is reported as an error.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `command` | string | yes | A single program invocation, e.g. 'python main.py' or 'git status'. No pipes, redirects, chaining (&&, ;) or variable expansion. |
| `cwd` | string | no | Working directory inside an allowed root (default: workspace) |
| `timeout_s` | number | no |  |

### `terminal_query`

Run a strictly read-only query command (versions, git status/log/diff, pip list, where). Never modifies anything.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `command` | string | yes | A read-only query, e.g. 'git status', 'python --version', 'pip list' |
| `cwd` | string | no |  |

## Browser

| Tool | Base risk | Approval | Timeout | Read-only | Idempotent | Untrusted output |
|---|---|---|---|---|---|---|
| `browser_back` | low | policy | 30s | no | no | yes |
| `browser_click` | low | policy | 45s | no | no | yes |
| `browser_download` | medium | policy | 120s | no | no | yes |
| `browser_find` | low | policy | 30s | yes | yes | yes |
| `browser_forward` | low | policy | 30s | no | no | yes |
| `browser_navigate` | low | policy | 60s | no | no | yes |
| `browser_open` | low | policy | 60s | no | no | yes |
| `browser_read` | low | policy | 30s | yes | yes | yes |
| `browser_select` | low | policy | 30s | no | no | yes |
| `browser_tabs` | low | policy | 45s | no | no | yes |
| `browser_type` | low | policy | 45s | no | no | yes |
| `browser_upload` | high | policy | 60s | no | no | no |

### `browser_back`

Go back in the active tab's history.

_No parameters._

### `browser_click`

Click an element by ref (from browser_find) or by natural-language query. Re-resolves stale refs automatically.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `ref` | string | no | Element ref from browser_find/browser_read, e.g. 'e42' |
| `query` | string | no | Natural-language target, e.g. 'Download', 'Search' |
| `role` | string | no | button / link / textbox / checkbox / combobox / radio |

Retries: up to 2 attempts on `stale_reference, element_not_found`.

### `browser_download`

Download a file to an allowed folder, via direct URL (HTTP API) or by clicking a download control. Executable file types are blocked.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `url` | string | no | Direct file URL (preferred: uses the HTTP API, no UI needed) |
| `ref` | string | no | Element that triggers the download when clicked |
| `query` | string | no | Natural-language description of the download control |
| `dest_dir` | string | no | Folder inside allowed roots; default @Downloads |
| `filename` | string | no |  |

### `browser_find`

Find interactive elements matching a description using the page's DOM/accessibility structure. Returns a few candidates with stable refs (e.g. button "Download" ref=e42) instead of the whole page.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | What to find, e.g. 'Download', 'search box', 'Sign in' |
| `role` | string | no |  |
| `limit` | integer | no |  |

Retries: up to 2 attempts on `element_not_found`.

### `browser_forward`

Go forward in the active tab's history.

_No parameters._

### `browser_navigate`

Navigate the active tab to an http(s) URL.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes |  |

Retries: up to 2 attempts on `navigation_failed`.

### `browser_open`

Open the browser (isolated session for this task), optionally at a URL, optionally in a new tab.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `url` | string | no | Optional http(s) URL to open |
| `new_tab` | boolean | no |  |

Retries: up to 2 attempts on `navigation_failed`.

### `browser_read`

Read the current page in a targeted way (text, accessibility tree, or interactive elements). Prefer browser_find for single targets.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `mode` | string | no | text: visible page text / aria: accessibility tree / elements/links/forms: interactive controls |
| `selector` | string | no | Optional CSS selector to scope text/aria |
| `max_chars` | integer | no |  |
| `force` | boolean | no | Return full content even if unchanged since the last read |

### `browser_select`

Choose an option in a <select>/dropdown by visible label or value.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `ref` | string | no | Element ref from browser_find/browser_read, e.g. 'e42' |
| `query` | string | no | Natural-language target, e.g. 'Download', 'Search' |
| `role` | string | no | button / link / textbox / checkbox / combobox / radio |
| `value` | string | no |  |
| `label` | string | no |  |

Retries: up to 2 attempts on `stale_reference, element_not_found`.

### `browser_tabs`

List, switch, close or open browser tabs.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string | no |  |
| `tab_id` | string | no |  |
| `url` | string | no |  |

### `browser_type`

Type text into an input/textarea by ref or query. Set submit=true to press Enter (e.g. to run a search).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `ref` | string | no | Element ref from browser_find/browser_read, e.g. 'e42' |
| `query` | string | no | Natural-language target, e.g. 'Download', 'Search' |
| `role` | string | no | button / link / textbox / checkbox / combobox / radio |
| `text` | string | yes |  |
| `clear` | boolean | no | Replace existing content (fill) instead of appending |
| `submit` | boolean | no | Press Enter afterwards |

Retries: up to 2 attempts on `stale_reference, element_not_found`.

### `browser_upload`

Upload a local file through a file input. Disabled unless explicitly enabled; always requires approval.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `ref` | string | no | Element ref from browser_find/browser_read, e.g. 'e42' |
| `query` | string | no | Natural-language target, e.g. 'Download', 'Search' |
| `role` | string | no | button / link / textbox / checkbox / combobox / radio |
| `path` | string | yes |  |

## Api

| Tool | Base risk | Approval | Timeout | Read-only | Idempotent | Untrusted output |
|---|---|---|---|---|---|---|
| `web_search` | low | policy | 45s | yes | yes | yes |

### `web_search`

Search the web through a structured search provider (API) and get titles, URLs and snippets. Prefer this over typing into a search engine in the browser: it cannot be blocked by a CAPTCHA. Open a result afterwards with browser_open if you need the page itself.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | What to search the web for |
| `max_results` | integer | no |  |

## Desktop

| Tool | Base risk | Approval | Timeout | Read-only | Idempotent | Untrusted output |
|---|---|---|---|---|---|---|
| `desktop_active_window` | low | policy | 30s | yes | no | yes |
| `desktop_click` | low | policy | 45s | no | no | no |
| `desktop_find_element` | low | policy | 30s | yes | yes | yes |
| `desktop_focus` | low | policy | 20s | no | no | no |
| `desktop_key` | medium | policy | 30s | no | no | no |
| `desktop_launch` | medium | policy | 30s | no | no | no |
| `desktop_read` | low | policy | 45s | yes | yes | yes |
| `desktop_scroll` | low | policy | 20s | no | no | no |
| `desktop_type` | medium | policy | 45s | no | no | no |
| `desktop_windows` | low | policy | 30s | yes | yes | yes |

### `desktop_active_window`

Return the currently focused window.

_No parameters._

### `desktop_click`

Click a control via UI Automation (Invoke/Toggle/Select patterns, then element-relative mouse). vision_ref uses coordinates from a confident vision result as a last resort.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `window_title` | string | no | Window title substring (default: the active window) |
| `window_process` | string | no | Process name such as 'Code.exe' |
| `window_handle` | integer | no |  |
| `ref` | string | no | Element ref from desktop_find_element / desktop_read |
| `query` | string | no | Element name/description, e.g. 'Run', 'Search' |
| `role` | string | no | button / textbox / menuitem / listitem / checkbox / ... |
| `automation_id` | string | no | Stable UI Automation id (most reliable) |
| `vision_ref` | string | no | FALLBACK: element ref from vision_analyze (e.g. 'vis_ab12:e2'); only if UI Automation cannot find the control |

Retries: up to 2 attempts on `stale_reference`.

### `desktop_find_element`

Find UI controls in a window via Windows UI Automation (buttons, text boxes, menus, lists...). Returns refs, roles, names and automation ids.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `window_title` | string | no | Window title substring (default: the active window) |
| `window_process` | string | no | Process name such as 'Code.exe' |
| `window_handle` | integer | no |  |
| `query` | string | no |  |
| `role` | string | no |  |
| `automation_id` | string | no |  |
| `limit` | integer | no |  |

### `desktop_focus`

Bring a window to the foreground (restores it if minimized).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `window_title` | string | no | Window title substring (default: the active window) |
| `window_process` | string | no | Process name such as 'Code.exe' |
| `window_handle` | integer | no |  |

### `desktop_key`

Send ONE validated key combination to the focused window (e.g. Ctrl+S). Terminals and the Windows key are blocked.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `window_title` | string | no | Window title substring (default: the active window) |
| `window_process` | string | no | Process name such as 'Code.exe' |
| `window_handle` | integer | no |  |
| `keys` | string | yes | One combination, e.g. 'Ctrl+S', 'Ctrl+Shift+P', 'Enter', 'F5'. The Windows key is not allowed. |
| `focus_first` | boolean | no | Bring the selected window to the foreground first |

### `desktop_launch`

Launch an allowlisted application (optionally opening a folder/file inside allowed roots).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `app` | string | yes | Allowlisted application name, e.g. 'vscode', 'notepad', 'calculator', 'explorer' |
| `args` | string[] | no | Arguments; file/folder paths must be inside allowed folders |
| `new_instance` | boolean | no | Start another instance even if the app is already open (default: reuse the open window) |

### `desktop_read`

Read desktop state structurally: the control tree of a window, one element's value/state, or visible text.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `window_title` | string | no | Window title substring (default: the active window) |
| `window_process` | string | no | Process name such as 'Code.exe' |
| `window_handle` | integer | no |  |
| `ref` | string | no | Element ref from desktop_find_element / desktop_read |
| `query` | string | no | Element name/description, e.g. 'Run', 'Search' |
| `role` | string | no | button / textbox / menuitem / listitem / checkbox / ... |
| `automation_id` | string | no | Stable UI Automation id (most reliable) |
| `mode` | string | no | tree: structured control tree / element: value/state of one control / text: visible text |
| `max_chars` | integer | no |  |
| `max_elements` | integer | no |  |
| `role_filter` | string[] | no | mode=tree: only these roles, e.g. ['button','textbox'] |

### `desktop_scroll`

Scroll a control or window (UI Automation scroll pattern, mouse wheel as fallback).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `window_title` | string | no | Window title substring (default: the active window) |
| `window_process` | string | no | Process name such as 'Code.exe' |
| `window_handle` | integer | no |  |
| `ref` | string | no | Element ref from desktop_find_element / desktop_read |
| `query` | string | no | Element name/description, e.g. 'Run', 'Search' |
| `role` | string | no | button / textbox / menuitem / listitem / checkbox / ... |
| `automation_id` | string | no | Stable UI Automation id (most reliable) |
| `direction` | string | no |  |
| `amount` | integer | no |  |

### `desktop_type`

Type text into a specific text control (set via UI Automation value pattern; keyboard only if needed). Terminals are blocked.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `window_title` | string | no | Window title substring (default: the active window) |
| `window_process` | string | no | Process name such as 'Code.exe' |
| `window_handle` | integer | no |  |
| `ref` | string | no | Element ref from desktop_find_element / desktop_read |
| `query` | string | no | Element name/description, e.g. 'Run', 'Search' |
| `role` | string | no | button / textbox / menuitem / listitem / checkbox / ... |
| `automation_id` | string | no | Stable UI Automation id (most reliable) |
| `text` | string | yes |  |
| `clear` | boolean | no |  |

Retries: up to 2 attempts on `stale_reference`.

### `desktop_windows`

List visible top-level windows (title, process, active/minimized).

_No parameters._

## Vision

| Tool | Base risk | Approval | Timeout | Read-only | Idempotent | Untrusted output |
|---|---|---|---|---|---|---|
| `vision_analyze` | medium | policy | 90s | yes | no | yes |
| `vision_capture` | medium | policy | 20s | no | no | no |

### `vision_analyze`

FALLBACK ONLY. Analyse a captured screenshot with the vision model. Returns structured elements with confidence. Low-confidence results must not be acted on; re-capture or ask the user.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `capture_id` | string | yes |  |
| `goal` | string | yes | What to locate / read / verify, e.g. 'the green Run button' |
| `mode` | string | no |  |

### `vision_capture`

FALLBACK ONLY. Capture a screenshot (window/screen/browser) for vision_analyze. Use only when DOM/accessibility and UI Automation cannot answer the question; the reason is recorded.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `source` | string | no |  |
| `window_title` | string | no | source=window: title substring (default: active window) |
| `window_process` | string | no |  |
| `full_page` | boolean | no |  |
| `reason` | string | yes | Why structured perception (DOM/UI Automation) is insufficient |

## Verification

| Tool | Base risk | Approval | Timeout | Read-only | Idempotent | Untrusted output |
|---|---|---|---|---|---|---|
| `verify_browser_state` | low | policy | 30s | yes | no | yes |
| `verify_desktop_state` | low | policy | 30s | yes | no | yes |
| `verify_file_state` | low | policy | 30s | yes | yes | yes |
| `verify_task_result` | low | policy | 30s | yes | no | yes |

### `verify_browser_state`

Verify the browser state: URL/title/visible text/element presence (all given conditions must hold).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `url_contains` | string | no |  |
| `title_contains` | string | no |  |
| `text_contains` | string | no |  |
| `element` | string | no | Element description that must be present |
| `wait_s` | number | no |  |

### `verify_desktop_state`

Verify desktop state via UI Automation: window open/closed, control exists, enabled, or contains a value.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `window_title` | string | no |  |
| `window_process` | string | no |  |
| `window_open` | boolean | no | Assert that a matching window is open (true) or closed (false) |
| `element_query` | string | no |  |
| `element_role` | string | no |  |
| `automation_id` | string | no |  |
| `expect_enabled` | boolean | no |  |
| `expect_value_contains` | string | no |  |

### `verify_file_state`

Verify a file/folder state: exists, absent, is a directory, or contains text.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes |  |
| `expect` | string | no |  |
| `text` | string | no | expect=contains: text that must be in the file |
| `min_bytes` | integer | no |  |
| `wait_s` | number | no |  |

### `verify_task_result`

Verify several conditions at once. Kinds: browser_element, browser_text, browser_title, browser_url, dir_exists, download_complete, element_state, file_absent, file_contains, file_exists, file_moved, files_exist, value_check, window_exists.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `criteria` | object[] | yes | List of {kind, params} verification specs, e.g. {kind:'file_exists', params:{path:'...'}} |

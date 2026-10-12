"""The permission each tool exercises, stated once.

Risk and category (what the permission engine decides on) are computed per call by the tool's
``assess``. This table says what *kind* of thing a tool is allowed to do at all, so that a new tool
cannot be added without placing it, and so that "read" tools can be proven to be read-only.
A test checks it against the real registry (``tests/security/test_permission_classes.py``).
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    READ = "read"  # read a file or document
    SEARCH = "search"  # list or search names and contents
    CREATE_EDIT = "create_edit"  # create a file or folder, write content
    MOVE = "move"
    COPY = "copy"
    DELETE = "delete"
    UNDO = "undo"  # reverse a journalled change of the task (rollback the user started only)
    EXECUTE = "execute"  # run a program through the controlled terminal
    QUERY_ENVIRONMENT = "query_environment"  # read-only facts about the machine or a repository
    BROWSER_READ = "browser_read"
    BROWSER_NAVIGATE = "browser_navigate"  # load a page: a read on the network
    BROWSER_INTERACT = "browser_interact"  # click, type, select: may submit or change something
    TRANSFER = "transfer"  # download to disk / upload from disk
    DESKTOP_READ = "desktop_read"
    DESKTOP_INPUT = "desktop_input"  # click, type, keys in a desktop application
    LAUNCH_APPLICATION = "launch_application"
    SCREEN_CAPTURE = "screen_capture"
    WEB_SEARCH = "web_search"
    VERIFY = "verify"  # deterministic checks of state


# Permissions whose tools never change anything. Enforced: such a tool must declare read_only.
OBSERVING = frozenset({
    Permission.READ, Permission.SEARCH, Permission.QUERY_ENVIRONMENT, Permission.BROWSER_READ,
    Permission.DESKTOP_READ, Permission.WEB_SEARCH, Permission.VERIFY,
})  # fmt: skip

# Permissions that can cause an effect outside this machine or one that cannot be taken back.
EXTERNAL_OR_IRREVERSIBLE = frozenset({
    Permission.DELETE, Permission.EXECUTE, Permission.BROWSER_INTERACT, Permission.TRANSFER,
    Permission.DESKTOP_INPUT, Permission.LAUNCH_APPLICATION,
})  # fmt: skip

TOOL_PERMISSIONS: dict[str, Permission] = {
    "fs_read": Permission.READ,
    "fs_read_many": Permission.READ,
    "fs_list": Permission.SEARCH,
    "fs_search": Permission.SEARCH,
    "fs_find": Permission.SEARCH,
    "fs_create": Permission.CREATE_EDIT,
    "fs_move": Permission.MOVE,
    "fs_organize": Permission.MOVE,  # moves or copies a list of files; never overwrites or deletes
    "fs_copy": Permission.COPY,
    "fs_delete": Permission.DELETE,
    "fs_undo": Permission.UNDO,
    "terminal_execute_safe": Permission.EXECUTE,
    "terminal_query": Permission.QUERY_ENVIRONMENT,
    "terminal_environment": Permission.QUERY_ENVIRONMENT,
    "browser_read": Permission.BROWSER_READ,
    "browser_find": Permission.BROWSER_READ,
    "browser_open": Permission.BROWSER_NAVIGATE,
    "browser_navigate": Permission.BROWSER_NAVIGATE,
    "browser_back": Permission.BROWSER_NAVIGATE,
    "browser_forward": Permission.BROWSER_NAVIGATE,
    "browser_tabs": Permission.BROWSER_NAVIGATE,
    "browser_click": Permission.BROWSER_INTERACT,
    "browser_type": Permission.BROWSER_INTERACT,
    "browser_select": Permission.BROWSER_INTERACT,
    "browser_download": Permission.TRANSFER,
    "browser_upload": Permission.TRANSFER,
    "desktop_windows": Permission.DESKTOP_READ,
    "desktop_active_window": Permission.DESKTOP_READ,
    "desktop_find_element": Permission.DESKTOP_READ,
    "desktop_read": Permission.DESKTOP_READ,
    "desktop_click": Permission.DESKTOP_INPUT,
    "desktop_type": Permission.DESKTOP_INPUT,
    "desktop_key": Permission.DESKTOP_INPUT,
    "desktop_focus": Permission.DESKTOP_INPUT,
    "desktop_scroll": Permission.DESKTOP_INPUT,
    "desktop_launch": Permission.LAUNCH_APPLICATION,
    "vision_capture": Permission.SCREEN_CAPTURE,
    "vision_analyze": Permission.SCREEN_CAPTURE,
    "web_search": Permission.WEB_SEARCH,
    "verify_file_state": Permission.VERIFY,
    "verify_browser_state": Permission.VERIFY,
    "verify_desktop_state": Permission.VERIFY,
    "verify_task_result": Permission.VERIFY,
}

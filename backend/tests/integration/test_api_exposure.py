"""NEXT-C over HTTP: what a browser tab on another site, or another machine, must not be able to do."""

from __future__ import annotations

import os
import subprocess
import sys

from starlette.websockets import WebSocketDisconnect

from tests.integration.conftest import BACKEND
from tests.integration.test_api import AUTH, TOKEN, Api, api  # noqa: F401  (fixture)
from tests.integration.test_startup import free_port

EVIL = "https://evil.example"
SECRET = "sk-ant-abcdefghijklmnopqrstuvwx"


def test_a_token_in_the_websocket_url_is_not_accepted(api: Api) -> None:  # noqa: F811
    for url in (f"/api/v1/ws/events?token={TOKEN}", f"/api/v1/ws/tasks/task_x?token={TOKEN}"):
        try:
            with api.c.websocket_connect(url) as ws:
                ws.send_text("ping")
                ws.receive_text()
            accepted = True
        except WebSocketDisconnect:
            accepted = False
        assert not accepted, f"a token in the URL opened {url.split('?')[0]}"
    with api.c.websocket_connect(
        "/api/v1/ws/events", subprotocols=["saa.v1", f"bearer.{TOKEN}"]
    ) as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_a_foreign_origin_gets_no_cors_permission(api: Api) -> None:  # noqa: F811
    preflight = api.c.options(
        "/api/v1/tasks",
        headers={
            "Origin": EVIL,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert "access-control-allow-origin" not in {k.lower() for k in preflight.headers}
    simple = api.c.get("/api/v1/health", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in {k.lower() for k in simple.headers}
    allowed = api.c.get("/api/v1/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-credentials" not in {k.lower() for k in allowed.headers}


def test_state_changing_requests_need_the_bearer_header_not_ambient_credentials(
    api: Api,  # noqa: F811
) -> None:
    """There is no cookie or session to ride on: a cross-site form post has no token and is refused."""
    for method, path in (
        ("post", "/api/v1/tasks"),
        ("post", "/api/v1/agent/stop"),
        ("post", "/api/v1/agent/release"),
        ("put", "/api/v1/permissions"),
        ("post", "/api/v1/approvals/apr_x/approve"),
        ("post", "/api/v1/tasks/task_x/recover"),
        ("post", "/api/v1/tasks/task_x/cancel"),
    ):
        r = getattr(api.c, method)(
            path, json={}, headers={"Origin": EVIL, "Cookie": f"token={TOKEN}"}
        )
        assert r.status_code == 401, (method, path, r.status_code)
    assert (
        api.c.get("/api/v1/tasks", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    )
    assert api.c.get(f"/api/v1/tasks?token={TOKEN}").status_code == 401


def test_error_responses_do_not_echo_secrets_or_internals(api: Api) -> None:  # noqa: F811
    r = api.c.get(f"/api/v1/tasks/{SECRET}", headers=AUTH)
    assert r.status_code == 404 and SECRET not in r.text and "REDACTED" in r.text
    r = api.c.post(
        "/api/v1/tasks", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"}
    )
    assert r.status_code == 422 and "Traceback" not in r.text
    for header in ("x-content-type-options", "cache-control", "referrer-policy"):
        assert header in r.headers


def test_dns_rebinding_is_refused_by_the_host_check(api: Api) -> None:  # noqa: F811
    for host in ("evil.example", "127.0.0.1.evil.example", "localhost.evil.example:8000"):
        r = api.c.get("/api/v1/tasks", headers={**AUTH, "Host": host})
        assert r.status_code == 400, host


def test_the_server_refuses_to_listen_on_the_network_unless_told_to() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAA_")}
    env.update({"SAA_LLM__PROVIDER": "none", "SAA_DATABASE__URL": "memory://"})
    for host in ("0.0.0.0", "::"):
        p = subprocess.run(
            [sys.executable, "-m", "app", "serve", "--host", host, "--port", str(free_port())],
            cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60, check=False,
        )  # noqa: S603  # fmt: skip
        assert p.returncode == 2, (host, p.stderr[-300:])
        assert "refusing to listen" in p.stderr and "Traceback" not in p.stderr
    env["SAA_HOST"] = "0.0.0.0"  # the setting is guarded too, not only the flag
    p = subprocess.run(
        [sys.executable, "-m", "app", "serve", "--port", str(free_port())],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # noqa: S603  # fmt: skip
    assert p.returncode == 2 and "refusing to listen" in p.stderr

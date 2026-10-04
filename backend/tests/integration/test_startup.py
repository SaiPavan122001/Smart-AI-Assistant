"""Boot the REAL server process (python -m app serve) against real PostgreSQL and exercise it over HTTP."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import BACKEND

TOKEN = "startup-test-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Server:
    def __init__(self, db_url: str, tmp: Path, port: int) -> None:
        self.port, self.base = port, f"http://127.0.0.1:{port}"
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("SAA_", "ANTHROPIC", "OPENAI"))
        }
        env.update({
            "SAA_ENV": "prod", "SAA_SECURITY__API_TOKEN": TOKEN, "SAA_DATABASE__URL": db_url, "SAA_DATABASE__AUTO_MIGRATE": "true",
            "SAA_LLM__PROVIDER": "none", "SAA_LOG_JSON": "true", "SAA_PORT": str(port), "SAA_DATA_DIR": str(tmp / "data"),
            "SAA_FILESYSTEM__WORKSPACE": str(tmp / "workspace"), "SAA_FILESYSTEM__ALLOWED_ROOTS": f'["{(tmp / "docs").as_posix()}"]',
            "SAA_SECURITY__ALLOWED_ORIGINS": f'["http://127.0.0.1:{port}"]', "SAA_SECURITY__ALLOWED_HOSTS": '["127.0.0.1","localhost"]',
        })  # fmt: skip
        (tmp / "docs").mkdir(exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app", "serve", "--port", str(port)],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )  # noqa: S603
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def wait_ready(self, timeout: float = 60) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise AssertionError(
                    "server exited early:\n" + (self.proc.stdout.read() if self.proc.stdout else "")
                )
            try:
                if httpx.get(f"{self.base}/api/v1/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.3)
        raise AssertionError("server did not become healthy")

    def stop(self) -> str:
        self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate()
        return out or ""


@pytest.fixture
def pg(pg_url: str | None) -> str:
    if pg_url is None:
        pytest.skip("embedded PostgreSQL unavailable")
    return pg_url


def test_real_server_migrates_serves_persists_and_recovers(pg: str, tmp_path: Path) -> None:
    srv = Server(pg, tmp_path, free_port())
    try:
        srv.wait_ready()
        r = httpx.get(f"{srv.base}/api/v1/ready").json()
        assert (
            r["ready"] is True
            and r["checks"]["database"] is True
            and r["checks"]["llm_configured"] is False
        )
        assert httpx.get(f"{srv.base}/api/v1/tasks").status_code == 401
        t = httpx.post(
            f"{srv.base}/api/v1/tasks", json={"instruction": "hello"}, headers=srv.headers
        ).json()
        for _ in range(100):
            d = httpx.get(f"{srv.base}/api/v1/tasks/{t['id']}", headers=srv.headers).json()
            if d["status"] == "FAILED":
                break
            time.sleep(0.1)
        assert d["status"] == "FAILED" and "No LLM provider" in d["result"]["error"]["message"]
        events = httpx.get(f"{srv.base}/api/v1/tasks/{t['id']}/events", headers=srv.headers).json()
        assert events[0]["type"] == "task_created" and events[-1]["type"] == "task_failed"
        # policy persistence
        pol = httpx.get(f"{srv.base}/api/v1/permissions", headers=srv.headers).json()
        pol["risk_actions"]["2"] = "ask"
        assert (
            httpx.put(f"{srv.base}/api/v1/permissions", json=pol, headers=srv.headers).status_code
            == 200
        )
        metrics = httpx.get(f"{srv.base}/metrics", headers=srv.headers).text
        assert "saa_tasks_total" in metrics
    finally:
        log = srv.stop()
    assert TOKEN not in log, "the API token must never be logged"
    assert "Traceback" not in log

    # restart on the same database: history and policy survive
    srv2 = Server(pg, tmp_path, free_port())
    try:
        srv2.wait_ready()
        assert (
            httpx.get(f"{srv2.base}/api/v1/tasks/{t['id']}", headers=srv2.headers).json()["status"]
            == "FAILED"
        )
        assert (
            httpx.get(f"{srv2.base}/api/v1/permissions", headers=srv2.headers).json()[
                "risk_actions"
            ]["2"]
            == "ask"
        )
        listed = httpx.get(f"{srv2.base}/api/v1/tasks", headers=srv2.headers).json()
        assert t["id"] in [
            i["id"] for i in listed["items"]
        ]  # the DB is shared across the test session
    finally:
        srv2.stop()


def test_built_frontend_is_served_with_spa_fallback(pg: str, tmp_path: Path) -> None:
    dist = BACKEND.parent / "frontend" / "dist" / "index.html"
    if not dist.exists():
        pytest.skip("frontend not built (npm run build)")
    srv = Server(pg, tmp_path, free_port())
    try:
        srv.wait_ready()
        home = httpx.get(f"{srv.base}/")
        assert (
            home.status_code == 200
            and "Smart AI Assistant" in home.text
            and "Content-Security-Policy" in home.text
        )
        deep = httpx.get(f"{srv.base}/history/task_abc")
        assert deep.status_code == 200 and '<div id="root">' in deep.text
        assert (
            httpx.get(f"{srv.base}/api/v1/does-not-exist", headers=srv.headers).status_code == 404
        )
        assert httpx.get(f"{srv.base}/api/docs").status_code == 200
    finally:
        srv.stop()


def test_prod_refuses_to_start_without_an_api_token(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAA_")}
    env.update({"SAA_ENV": "prod", "SAA_DATABASE__URL": "memory://", "SAA_LLM__PROVIDER": "none"})
    p = subprocess.run(
        [sys.executable, "-m", "app", "serve", "--port", str(free_port())],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )  # noqa: S603
    assert p.returncode != 0 and "API_TOKEN" in (p.stdout + p.stderr)


@pytest.fixture(autouse=True)
def _quiet() -> Iterator[None]:
    yield


def _serve_once(env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAA_")}
    env.update({"SAA_LLM__PROVIDER": "none", "SAA_SECURITY__API_TOKEN": "t", **env_extra})
    return subprocess.run(
        [sys.executable, "-m", "app", "serve", "--port", str(free_port())],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )  # noqa: S603


def test_unreachable_database_fails_fast_with_an_actionable_message() -> None:
    p = _serve_once(
        {"SAA_DATABASE__URL": f"postgresql+asyncpg://u:secret-pw@127.0.0.1:{free_port()}/db"}
    )
    assert (
        p.returncode == 2
        and "Cannot connect to the database" in p.stderr
        and "memory://" in p.stderr
    )
    assert "secret-pw" not in p.stdout + p.stderr and "Traceback" not in p.stderr


def test_missing_schema_points_to_migrate(tmp_path: Path) -> None:
    p = _serve_once({"SAA_DATABASE__URL": f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"})
    assert p.returncode == 2 and "python -m app migrate" in p.stderr and "Traceback" not in p.stderr

"""Smoke test: build the FastMCP server, confirm hermes_ask is registered,
and confirm the tool works when the underlying HermesClient is mocked.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock

import pytest
from mcp.shared.auth import InvalidRedirectUriError
from pydantic import AnyUrl

from hermes_mcp.config import Config
from hermes_mcp.hermes_client import HermesError
from hermes_mcp.jobs import JobStore
from hermes_mcp.server import build_app

VALID_ENV: dict[str, str] = {
    "OAUTH_CLIENT_ID": "hermes-mcp-test",
    "OAUTH_CLIENT_SECRET": "x" * 32,
    "OAUTH_ISSUER_URL": "https://hermes.example.com",
    "HERMES_API_KEY": "k" * 32,
}


def _config() -> Config:
    return Config.from_env(VALID_ENV)


def test_build_app_registers_hermes_ask() -> None:
    cfg = _config()
    client = MagicMock()
    mcp = build_app(cfg, client)
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "hermes_ask" in tool_names


def test_hermes_ask_invokes_client() -> None:
    cfg = _config()
    client = MagicMock()
    client.ask.return_value = "the answer"
    mcp = build_app(cfg, client)
    tool = mcp._tool_manager.get_tool("hermes_ask")
    assert tool is not None
    fn = tool.fn
    result = fn(prompt="hi", session_id=None, toolsets=None)
    assert result == "the answer"
    client.ask.assert_called_once_with("hi", session_id=None, toolsets=None)


def test_hermes_ask_propagates_hermes_error() -> None:
    cfg = _config()
    client = MagicMock()
    client.ask.side_effect = HermesError("hermes exited 2: boom")
    mcp = build_app(cfg, client)
    tool = mcp._tool_manager.get_tool("hermes_ask")
    assert tool is not None
    with pytest.raises(HermesError, match="hermes exited 2"):
        tool.fn(prompt="hi", session_id=None, toolsets=None)


def test_oauth_routes_present() -> None:
    """The streamable_http_app should mount /authorize, /token, and metadata."""
    cfg = _config()
    client = MagicMock()
    mcp = build_app(cfg, client)
    app = mcp.streamable_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/authorize" in paths
    assert "/token" in paths
    assert "/.well-known/oauth-authorization-server" in paths


def test_allowed_hosts_propagated_to_transport_security() -> None:
    cfg = Config.from_env({**VALID_ENV, "MCP_ALLOWED_HOSTS": "hermes.example.com"})
    client = MagicMock()
    mcp = build_app(cfg, client)
    hosts = mcp.settings.transport_security.allowed_hosts  # type: ignore[union-attr]
    assert "hermes.example.com" in hosts
    assert "127.0.0.1:*" in hosts


def test_allowed_hosts_origins_handle_scheme_prefix() -> None:
    """An entry in MCP_ALLOWED_HOSTS that already has a scheme should not get
    'https://' prepended; an entry that doesn't should."""
    cfg = Config.from_env(
        {
            **VALID_ENV,
            "MCP_ALLOWED_HOSTS": "hermes.example.com,https://other.example.com",
        }
    )
    client = MagicMock()
    mcp = build_app(cfg, client)
    origins = mcp.settings.transport_security.allowed_origins  # type: ignore[union-attr]
    assert "https://hermes.example.com" in origins
    assert "https://other.example.com" in origins
    assert "https://https://other.example.com" not in origins


def test_oauth_allowed_redirect_uris_propagate_from_env_to_validation() -> None:
    """End-to-end wiring check: `OAUTH_ALLOWED_REDIRECT_URIS` set on the env
    reaches `_StaticClient.validate_redirect_uri` through Config →
    build_app → StaticClientProvider → _StaticClient. The unit tests in
    test_config.py and test_oauth.py cover the parts in isolation; this
    catches a regression that breaks the wiring between them."""
    cfg = Config.from_env(
        {**VALID_ENV, "OAUTH_ALLOWED_REDIRECT_URIS": "https://app.example.com/cb"}
    )
    mcp = build_app(cfg, MagicMock())
    provider = mcp._auth_server_provider  # type: ignore[attr-defined]
    assert provider is not None
    client = provider._client  # type: ignore[attr-defined]
    # Configured URI works end-to-end.
    assert client.validate_redirect_uri(AnyUrl("https://app.example.com/cb")) is not None
    # Anything else — including the Claude default, once replaced — is refused.
    for other in ("https://app.example.com/cb2", "https://claude.ai/api/mcp/auth_callback"):
        with pytest.raises(InvalidRedirectUriError, match="not allowed"):
            client.validate_redirect_uri(AnyUrl(other))


def test_build_app_registers_confidential_client() -> None:
    """The provider build_app wires in must enforce the client_secret."""
    mcp = build_app(_config(), MagicMock())
    client = mcp._auth_server_provider._client  # type: ignore[attr-defined]
    assert client.client_secret == VALID_ENV["OAUTH_CLIENT_SECRET"]
    assert client.token_endpoint_auth_method == "client_secret_post"


def test_oauth_issuer_url_no_double_slash_in_resource_url() -> None:
    """OAUTH_ISSUER_URL with a trailing slash must not produce '//mcp' in the
    derived resource_server_url."""
    cfg = Config.from_env({**VALID_ENV, "OAUTH_ISSUER_URL": "https://hermes.example.com/"})
    client = MagicMock()
    mcp = build_app(cfg, client)
    resource_url = str(mcp.settings.auth.resource_server_url)  # type: ignore[union-attr]
    assert "//mcp" not in resource_url.replace("https://", "")
    assert resource_url.endswith("/mcp")


# --- async_mode + hermes_check ------------------------------------------------


def _await_job(jobs: JobStore, job_id: str, timeout: float = 2.0) -> None:
    """Block until the background worker finishes the given job, or fail.

    Treats all terminal statuses (`completed`, `failed`, `cancelled`) as
    finished. Tests that submit a job and then cancel it can use this to
    confirm the worker has fully exited rather than racing it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job is not None and job.status in {"completed", "failed", "cancelled"}:
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_build_app_registers_hermes_check() -> None:
    cfg = _config()
    client = MagicMock()
    mcp = build_app(cfg, client)
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "hermes_check" in tool_names


def test_async_mode_returns_pending_job_id_immediately() -> None:
    cfg = _config()
    client = MagicMock()

    started = threading.Event()
    release = threading.Event()

    def slow_ask(*_a: object, **_kw: object) -> str:
        started.set()
        release.wait(timeout=2)
        return "eventual answer"

    client.ask.side_effect = slow_ask

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    assert ask_tool is not None

    out = ask_tool.fn(prompt="hi", session_id=None, toolsets=None, async_mode=True)
    payload = json.loads(out)
    assert payload["status"] == "pending"
    assert "job_id" in payload
    # Worker must already be running by the time hermes_ask returns.
    assert started.wait(timeout=1.0)
    release.set()
    _await_job(jobs, payload["job_id"])


def test_hermes_check_returns_completed_result() -> None:
    cfg = _config()
    client = MagicMock()
    client.ask.return_value = "the answer"

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    check_tool = mcp._tool_manager.get_tool("hermes_check")
    assert ask_tool is not None
    assert check_tool is not None

    submit_payload = json.loads(
        ask_tool.fn(prompt="hi", session_id=None, toolsets=None, async_mode=True)
    )
    _await_job(jobs, submit_payload["job_id"])

    result_payload = json.loads(check_tool.fn(job_id=submit_payload["job_id"]))
    assert result_payload["status"] == "completed"
    assert result_payload["result"] == "the answer"
    assert "error" not in result_payload


def test_hermes_check_returns_failed_on_hermes_error() -> None:
    cfg = _config()
    client = MagicMock()
    client.ask.side_effect = HermesError("gateway exploded")

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    check_tool = mcp._tool_manager.get_tool("hermes_check")
    assert ask_tool is not None
    assert check_tool is not None

    submit_payload = json.loads(
        ask_tool.fn(prompt="hi", session_id=None, toolsets=None, async_mode=True)
    )
    _await_job(jobs, submit_payload["job_id"])

    result_payload = json.loads(check_tool.fn(job_id=submit_payload["job_id"]))
    assert result_payload["status"] == "failed"
    assert result_payload["error"] == "gateway exploded"
    assert "result" not in result_payload


def test_hermes_check_redacts_unexpected_exception_message() -> None:
    """If the worker hits a non-HermesError exception, the message is NOT
    echoed in the job record — only the exception type. Matches the existing
    'gateway error bodies are redacted from user-facing errors' invariant."""
    cfg = _config()
    client = MagicMock()
    secret = "SHOULD_NOT_LEAK_THROUGH_ERROR"
    client.ask.side_effect = RuntimeError(secret)

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    check_tool = mcp._tool_manager.get_tool("hermes_check")
    assert ask_tool is not None
    assert check_tool is not None

    submit_payload = json.loads(
        ask_tool.fn(prompt="hi", session_id=None, toolsets=None, async_mode=True)
    )
    _await_job(jobs, submit_payload["job_id"])

    result_payload = json.loads(check_tool.fn(job_id=submit_payload["job_id"]))
    assert result_payload["status"] == "failed"
    assert secret not in result_payload["error"]
    assert "RuntimeError" in result_payload["error"]


def test_hermes_check_unknown_job_id() -> None:
    cfg = _config()
    client = MagicMock()
    mcp = build_app(cfg, client)
    check_tool = mcp._tool_manager.get_tool("hermes_check")
    assert check_tool is not None

    result = json.loads(check_tool.fn(job_id="not-a-real-id"))
    assert result == {"job_id": "not-a-real-id", "status": "unknown"}


def test_sync_mode_unchanged() -> None:
    """async_mode=False (the default) must behave identically to v0.2.0:
    return the gateway response text directly, no job_id involved."""
    cfg = _config()
    client = MagicMock()
    client.ask.return_value = "direct answer"
    mcp = build_app(cfg, client)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    assert ask_tool is not None
    out = ask_tool.fn(prompt="hi", session_id="s1", toolsets=None)
    assert out == "direct answer"
    client.ask.assert_called_once_with("hi", session_id="s1", toolsets=None)


def test_async_mode_forwards_session_id_and_toolsets_to_worker() -> None:
    """Worker thread must call client.ask with the same session_id and
    toolsets the MCP caller supplied — a closure bug here would be silent."""
    cfg = _config()
    client = MagicMock()
    client.ask.return_value = "ok"

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    assert ask_tool is not None

    submit_payload = json.loads(
        ask_tool.fn(
            prompt="hi",
            session_id="sess-async",
            toolsets=["hermes-telegram"],
            async_mode=True,
        )
    )
    _await_job(jobs, submit_payload["job_id"])

    client.ask.assert_called_once_with("hi", session_id="sess-async", toolsets=["hermes-telegram"])


def test_build_app_registers_hermes_cancel() -> None:
    cfg = _config()
    client = MagicMock()
    mcp = build_app(cfg, client)
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "hermes_cancel" in tool_names


def test_hermes_cancel_releases_in_flight_job() -> None:
    """Cancel while running -> status flips to cancelled. The worker is still
    running on a real thread; we let it finish in this test to verify the
    late-arriving result doesn't undo the cancellation (separate test below)."""
    cfg = _config()
    client = MagicMock()

    started = threading.Event()
    release = threading.Event()

    def hold(*_a: object, **_kw: object) -> str:
        started.set()
        release.wait(timeout=2)
        return "would-be-result"

    client.ask.side_effect = hold

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    cancel_tool = mcp._tool_manager.get_tool("hermes_cancel")
    check_tool = mcp._tool_manager.get_tool("hermes_check")
    assert ask_tool is not None and cancel_tool is not None and check_tool is not None

    submit = json.loads(ask_tool.fn(prompt="x", async_mode=True))
    assert started.wait(timeout=1.0)

    cancel_payload = json.loads(cancel_tool.fn(job_id=submit["job_id"]))
    assert cancel_payload["status"] == "cancelled"
    assert "finished_at" in cancel_payload

    # Let the worker thread finish; status must remain cancelled.
    release.set()
    time.sleep(0.05)  # let the worker's mark_completed fire
    after = json.loads(check_tool.fn(job_id=submit["job_id"]))
    assert after["status"] == "cancelled"
    assert "result" not in after


def test_hermes_cancel_is_noop_on_completed_job() -> None:
    cfg = _config()
    client = MagicMock()
    client.ask.return_value = "the answer"

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    cancel_tool = mcp._tool_manager.get_tool("hermes_cancel")
    assert ask_tool is not None and cancel_tool is not None

    submit = json.loads(ask_tool.fn(prompt="x", async_mode=True))
    _await_job(jobs, submit["job_id"])

    payload = json.loads(cancel_tool.fn(job_id=submit["job_id"]))
    assert payload["status"] == "completed"
    assert payload["result"] == "the answer"


def test_hermes_cancel_unknown_job_id() -> None:
    cfg = _config()
    client = MagicMock()
    mcp = build_app(cfg, client)
    cancel_tool = mcp._tool_manager.get_tool("hermes_cancel")
    assert cancel_tool is not None
    payload = json.loads(cancel_tool.fn(job_id="not-a-real-id"))
    assert payload == {"job_id": "not-a-real-id", "status": "unknown"}


def test_build_app_registers_hermes_reset() -> None:
    cfg = _config()
    client = MagicMock()
    mcp = build_app(cfg, client)
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "hermes_reset" in tool_names


def test_hermes_reset_clears_all_jobs_and_reports_counts() -> None:
    cfg = _config()
    client = MagicMock()
    client.ask.return_value = "the answer"

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    reset_tool = mcp._tool_manager.get_tool("hermes_reset")
    check_tool = mcp._tool_manager.get_tool("hermes_check")
    assert ask_tool is not None and reset_tool is not None and check_tool is not None

    # Submit two jobs and let them complete.
    a = json.loads(ask_tool.fn(prompt="a", async_mode=True))
    b = json.loads(ask_tool.fn(prompt="b", async_mode=True))
    _await_job(jobs, a["job_id"])
    _await_job(jobs, b["job_id"])

    payload = json.loads(reset_tool.fn())
    assert payload["cleared"] == 2
    assert payload["by_status"] == {"completed": 2}
    # Post-reset, both ids are unknown.
    for jid in (a["job_id"], b["job_id"]):
        assert json.loads(check_tool.fn(job_id=jid)) == {"job_id": jid, "status": "unknown"}


def test_hermes_reset_on_empty_store() -> None:
    cfg = _config()
    client = MagicMock()
    mcp = build_app(cfg, client)
    reset_tool = mcp._tool_manager.get_tool("hermes_reset")
    assert reset_tool is not None
    assert json.loads(reset_tool.fn()) == {"cleared": 0, "by_status": {}}


def test_submit_works_after_reset_with_fresh_job_id() -> None:
    """After hermes_reset, hermes_ask must succeed and return a brand new
    job_id — no leftover state should pin or collide with the old one."""
    cfg = _config()
    client = MagicMock()
    client.ask.return_value = "fresh answer"

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    reset_tool = mcp._tool_manager.get_tool("hermes_reset")
    check_tool = mcp._tool_manager.get_tool("hermes_check")
    assert ask_tool is not None and reset_tool is not None and check_tool is not None

    first = json.loads(ask_tool.fn(prompt="a", async_mode=True))
    _await_job(jobs, first["job_id"])
    reset_tool.fn()

    second = json.loads(ask_tool.fn(prompt="b", async_mode=True))
    assert second["status"] == "pending"
    assert second["job_id"] != first["job_id"]
    _await_job(jobs, second["job_id"])
    after = json.loads(check_tool.fn(job_id=second["job_id"]))
    assert after["status"] == "completed"
    assert after["result"] == "fresh answer"
    # And the old id is permanently unknown.
    assert json.loads(check_tool.fn(job_id=first["job_id"]))["status"] == "unknown"


def test_hermes_reset_clears_in_flight_job_without_blocking() -> None:
    """Reset while a worker is mid-flight: the job disappears immediately,
    and the worker's eventual mark_completed becomes a safe no-op."""
    cfg = _config()
    client = MagicMock()

    started = threading.Event()
    release = threading.Event()

    def hold(*_a: object, **_kw: object) -> str:
        started.set()
        release.wait(timeout=2)
        return "would-be-result"

    client.ask.side_effect = hold

    jobs = JobStore()
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    reset_tool = mcp._tool_manager.get_tool("hermes_reset")
    check_tool = mcp._tool_manager.get_tool("hermes_check")
    assert ask_tool is not None and reset_tool is not None and check_tool is not None

    submit = json.loads(ask_tool.fn(prompt="x", async_mode=True))
    assert started.wait(timeout=1.0)

    payload = json.loads(reset_tool.fn())
    assert payload["cleared"] == 1
    assert payload["by_status"] == {"running": 1}
    assert json.loads(check_tool.fn(job_id=submit["job_id"]))["status"] == "unknown"

    # Let the worker finish; it must not resurrect the wiped job.
    release.set()
    time.sleep(0.05)
    assert json.loads(check_tool.fn(job_id=submit["job_id"]))["status"] == "unknown"
    assert len(jobs) == 0


def test_async_mode_surfaces_capacity_error() -> None:
    """When the JobStore is at capacity, async submission must surface a
    clear error (not silently drop the request)."""
    cfg = _config()
    client = MagicMock()

    jobs = JobStore(max_jobs=1)
    mcp = build_app(cfg, client, jobs=jobs)
    ask_tool = mcp._tool_manager.get_tool("hermes_ask")
    assert ask_tool is not None

    # First async submission fills the only slot.
    started = threading.Event()
    release = threading.Event()

    def hold(*_a: object, **_kw: object) -> str:
        started.set()
        release.wait(timeout=2)
        return "done"

    client.ask.side_effect = hold

    first = json.loads(ask_tool.fn(prompt="a", async_mode=True))
    assert first["status"] == "pending"
    assert started.wait(timeout=1.0)

    # Second submission must raise — store is at capacity (1) while job #1 runs.
    with pytest.raises(RuntimeError, match="capacity"):
        ask_tool.fn(prompt="b", async_mode=True)

    release.set()
    _await_job(jobs, first["job_id"])

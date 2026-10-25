from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import httpx
import pytest

from hermes_mcp.hermes_client import (
    CONNECT_TIMEOUT_SECONDS,
    HermesClient,
    HermesError,
)


def _client(timeout: int = 60, **kwargs: object) -> HermesClient:
    return HermesClient(
        api_url="http://127.0.0.1:8642",
        api_key="k" * 32,
        model="hermes-agent",
        timeout_seconds=timeout,
        **kwargs,  # type: ignore[arg-type]
    )


def _delta(content: str | None, finish: str | None = None) -> str:
    payload: dict[str, object] = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": content}}],
    }
    if content is None:
        payload["choices"] = [{"index": 0, "delta": {"role": "assistant"}}]
    if finish:
        payload["choices"][0]["finish_reason"] = finish  # type: ignore[index]
    return "data: " + json.dumps(payload)


class _FakeResponse:
    def __init__(self, status_code: int, lines: list[str], body: bytes = b"") -> None:
        self.status_code = status_code
        self._lines = lines
        self._body = body

    def iter_lines(self) -> Iterator[str]:
        yield from self._lines

    def read(self) -> bytes:
        return self._body


class _FakeStream:
    """Stands in for the `httpx.stream(...)` context manager."""

    def __init__(self, response: _FakeResponse, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc

    def __enter__(self) -> _FakeResponse:
        if self._raise is not None:
            raise self._raise
        return self._response

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _install_stream(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse | None = None,
    raise_exc: Exception | None = None,
    captured: dict[str, object] | None = None,
) -> None:
    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStream:
        if captured is not None:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs
        return _FakeStream(response or _FakeResponse(200, []), raise_exc)

    monkeypatch.setattr("hermes_mcp.hermes_client.httpx.stream", fake_stream)


# --- request shape (regression guard: the streamed body is the whole point) ----


def test_ask_posts_streaming_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _install_stream(
        monkeypatch,
        _FakeResponse(200, [_delta("hi from hermes"), "data: [DONE]"]),
        captured=captured,
    )
    out = _client().ask("ping")
    assert out == "hi from hermes"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:8642/v1/chat/completions"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["json"] == {  # type: ignore[index]
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": True,
    }
    assert kwargs["headers"]["Authorization"] == "Bearer " + "k" * 32  # type: ignore[index]
    assert "X-Hermes-Session-Id" not in kwargs["headers"]  # type: ignore[index]
    assert isinstance(kwargs["timeout"], httpx.Timeout)  # type: ignore[index]


def test_ask_forwards_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _install_stream(
        monkeypatch, _FakeResponse(200, [_delta("ok"), "data: [DONE]"]), captured=captured
    )
    _client().ask("ping", session_id="sess-1")
    headers = captured["kwargs"]["headers"]  # type: ignore[index]
    assert headers["X-Hermes-Session-Id"] == "sess-1"


# --- SSE assembly --------------------------------------------------------------


def test_ask_assembles_deltas_and_skips_keepalives(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        ": keepalive",
        _delta(None),
        ": keepalive",
        _delta("Hello"),
        "",
        _delta(" world"),
        ": keepalive",
        _delta(".", finish="stop"),
        "data: [DONE]",
    ]
    _install_stream(monkeypatch, _FakeResponse(200, lines))
    out = _client().ask("ping")
    assert out == "Hello world."
    assert "keepalive" not in out
    assert "data:" not in out


def test_ask_stops_reading_after_done_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [_delta("first"), "data: [DONE]", _delta("LEAK-AFTER-DONE")]
    _install_stream(monkeypatch, _FakeResponse(200, lines))
    assert _client().ask("ping") == "first"


def test_ask_tolerates_finish_reason_length(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [_delta("truncated answer", finish="length"), "data: [DONE]"]
    _install_stream(monkeypatch, _FakeResponse(200, lines))
    assert _client().ask("ping") == "truncated answer"


def test_ask_returns_empty_string_for_empty_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(monkeypatch, _FakeResponse(200, [": keepalive", "data: [DONE]"]))
    assert _client().ask("ping") == ""


def test_ask_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(monkeypatch, _FakeResponse(200, [_delta("  spaced out  \n"), "data: [DONE]"]))
    assert _client().ask("ping") == "spaced out"


def test_ask_ignores_unparsable_data_line(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = ["data: {not json", _delta("ok"), "data: [DONE]"]
    _install_stream(monkeypatch, _FakeResponse(200, lines))
    assert _client().ask("ping") == "ok"


# --- error mapping -------------------------------------------------------------


def test_ask_translates_401_to_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(monkeypatch, _FakeResponse(401, [], b'{"error":"nope"}'))
    with pytest.raises(HermesError, match="rejected the API key"):
        _client().ask("ping")


def test_ask_propagates_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(monkeypatch, _FakeResponse(500, [], b"boom"))
    with pytest.raises(HermesError, match="returned HTTP 500"):
        _client().ask("ping")


def test_ask_does_not_echo_response_body_in_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misbehaving gateway must not be able to inject content into the
    user-visible error string. Body lands in DEBUG only."""
    secret_marker = "GATEWAY_RESPONSE_BODY_SHOULD_NOT_LEAK"
    _install_stream(monkeypatch, _FakeResponse(500, [], secret_marker.encode()))
    try:
        _client().ask("ping")
    except HermesError as exc:
        assert secret_marker not in str(exc)
    else:
        raise AssertionError("expected HermesError")


def test_ask_reports_connect_timeout_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(monkeypatch, raise_exc=httpx.ConnectTimeout("slow handshake"))
    with pytest.raises(HermesError, match="could not be reached"):
        _client().ask("ping")


def test_ask_reports_silence_with_new_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old wording (`timed out after …`) must be gone: the limit is now
    inactivity, and the message has to say so — the roll-out acceptance counts
    the two classes separately."""
    _install_stream(monkeypatch, raise_exc=httpx.ReadTimeout("no data"))
    with pytest.raises(HermesError, match="sent no events for 60s") as excinfo:
        _client().ask("ping")
    assert "timed out after" not in str(excinfo.value)


def test_ask_translates_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(monkeypatch, raise_exc=httpx.ConnectError("refused"))
    with pytest.raises(HermesError, match="request failed"):
        _client().ask("ping")


def test_ask_reports_write_timeout_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write/pool timeouts mean "could not be sent", not "gateway stayed
    silent" — the two need different diagnostics."""
    _install_stream(monkeypatch, raise_exc=httpx.WriteTimeout("stuck"))
    with pytest.raises(HermesError, match="could not be sent"):
        _client().ask("ping")


def test_ask_rejects_truncated_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that ends without `[DONE]` and without `finish_reason` is a
    truncated answer — it must fail, not return a silently shortened reply."""
    _install_stream(monkeypatch, _FakeResponse(200, [_delta("half an ans")]))
    with pytest.raises(HermesError, match="closed the stream before the answer was complete"):
        _client().ask("ping")


# --- timeout semantics ---------------------------------------------------------


def test_timeout_is_an_inactivity_limit_not_a_total(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _install_stream(monkeypatch, _FakeResponse(200, ["data: [DONE]"]), captured=captured)
    client = _client(timeout=900)
    client.ask("ping")
    timeout = captured["kwargs"]["timeout"]  # type: ignore[index]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 900.0
    assert timeout.connect == CONNECT_TIMEOUT_SECONDS
    assert timeout.connect < 900.0
    assert timeout.write is not None and timeout.pool is not None


def test_constructor_rejects_bad_limits() -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        HermesClient(api_url="http://localhost", api_key="k" * 32, model="m", timeout_seconds=0)
    with pytest.raises(ValueError, match="max_seconds"):
        HermesClient(
            api_url="http://localhost",
            api_key="k" * 32,
            model="m",
            timeout_seconds=60,
            max_seconds=-1,
        )


# --- real transport: a slow but live stream must survive ------------------------
#
# These tests use a real local HTTP server so that httpx's timeout machinery is
# genuinely in play. A monkeypatched transport would prove nothing about
# timeouts.


class _SSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write(self, line: str) -> bool:
        try:
            self.wfile.write((line + "\n\n").encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        mode = self.server.mode  # type: ignore[attr-defined]
        self._start_stream()
        if mode == "silent":
            time.sleep(1.8)
            return
        if mode == "slow":
            # 6 events, 0.35s apart → ~2.1s total, every gap well under 1s.
            for i in range(6):
                if not self._write(": keepalive" if i % 2 == 0 else _delta(f"part{i} ")):
                    return
                time.sleep(0.35)
            self._write(_delta("", finish="stop"))
            self._write("data: [DONE]")
            return
        if mode == "burst_then_silent":
            # One event, then a long silence: the watchdog must end the turn at
            # the ceiling, not the (longer) inactivity limit.
            self._write(_delta("start "))
            time.sleep(5.0)
            return
        if mode == "chatty_keepalive":
            # Only comment lines: a chatty gateway that never produces content.
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if not self._write(": keepalive"):
                    return
                time.sleep(0.2)
            return
        # "forever"
        deadline = time.monotonic() + 8.0
        i = 0
        while time.monotonic() < deadline:
            if not self._write(_delta(f"tick{i} ")):
                return
            i += 1
            time.sleep(0.2)


@pytest.fixture()
def sse_server(request: pytest.FixtureRequest) -> Iterator[str]:
    mode = request.param
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    server.daemon_threads = True
    server.mode = mode  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("sse_server", ["slow"], indirect=True)
def test_ask_survives_a_live_stream_longer_than_the_inactivity_limit(
    sse_server: str,
) -> None:
    """The discriminator: total duration (~2.1s) exceeds the 1s limit, but no
    single gap between events does. A request-wide timeout would kill this
    turn — the whole point of the change is that it must not."""
    client = HermesClient(
        api_url=sse_server, api_key="k" * 32, model="hermes-agent", timeout_seconds=1
    )
    started = time.monotonic()
    out = client.ask("ping")
    elapsed = time.monotonic() - started
    assert elapsed > 1.0, f"test did not exceed the limit (took {elapsed:.2f}s)"
    assert out.startswith("part1 part3 part5")
    assert "keepalive" not in out


@pytest.mark.parametrize("sse_server", ["silent"], indirect=True)
def test_ask_reports_silence_from_a_real_socket(sse_server: str) -> None:
    client = HermesClient(
        api_url=sse_server, api_key="k" * 32, model="hermes-agent", timeout_seconds=1
    )
    started = time.monotonic()
    with pytest.raises(HermesError, match="sent no events for 1s"):
        client.ask("ping")
    assert time.monotonic() - started < 1.8


@pytest.mark.parametrize("sse_server", ["forever"], indirect=True)
def test_ask_enforces_optional_max_seconds_ceiling(sse_server: str) -> None:
    client = HermesClient(
        api_url=sse_server,
        api_key="k" * 32,
        model="hermes-agent",
        timeout_seconds=5,
        max_seconds=1,
    )
    with pytest.raises(HermesError, match="exceeded HERMES_REQUEST_MAX_SECONDS"):
        client.ask("ping")


@pytest.mark.parametrize("sse_server", ["silent"], indirect=True)
def test_max_seconds_is_a_hard_wall_on_a_silent_gateway(sse_server: str) -> None:
    """A silent gateway must hit the operator's ceiling within that budget, not
    after the (longer) inactivity limit."""
    client = HermesClient(
        api_url=sse_server,
        api_key="k" * 32,
        model="hermes-agent",
        timeout_seconds=30,
        max_seconds=1,
    )
    started = time.monotonic()
    with pytest.raises(HermesError, match="exceeded HERMES_REQUEST_MAX_SECONDS"):
        client.ask("ping")
    assert time.monotonic() - started < 3.0


@pytest.mark.parametrize("sse_server", ["burst_then_silent"], indirect=True)
def test_max_seconds_survives_a_follow_up_silence(sse_server: str) -> None:
    """After the first event the next read must not be allowed to block for the
    full inactivity limit — the watchdog ends the turn at the ceiling."""
    client = HermesClient(
        api_url=sse_server,
        api_key="k" * 32,
        model="hermes-agent",
        timeout_seconds=6,
        max_seconds=2,
    )
    started = time.monotonic()
    with pytest.raises(HermesError, match="exceeded HERMES_REQUEST_MAX_SECONDS"):
        client.ask("ping")
    assert time.monotonic() - started < 5.0


def test_max_seconds_bounds_the_blocking_read(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _install_stream(monkeypatch, _FakeResponse(200, ["data: [DONE]"]), captured=captured)
    _client(timeout=60, max_seconds=5).ask("ping")
    timeout = captured["kwargs"]["timeout"]  # type: ignore[index]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 5.0
    # The ceiling covers the whole call, setup phases included.
    assert timeout.connect == 5.0
    assert timeout.write == 5.0
    assert timeout.pool == 5.0


def test_max_seconds_does_not_touch_budgets_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _install_stream(monkeypatch, _FakeResponse(200, ["data: [DONE]"]), captured=captured)
    _client(timeout=60, max_seconds=0).ask("ping")
    timeout = captured["kwargs"]["timeout"]  # type: ignore[index]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 60.0
    assert timeout.connect == CONNECT_TIMEOUT_SECONDS


@pytest.mark.parametrize("sse_server", ["chatty_keepalive"], indirect=True)
def test_max_seconds_ceiling_cannot_be_escaped_with_keepalives(sse_server: str) -> None:
    client = HermesClient(
        api_url=sse_server,
        api_key="k" * 32,
        model="hermes-agent",
        timeout_seconds=5,
        max_seconds=1,
    )
    started = time.monotonic()
    with pytest.raises(HermesError, match="exceeded HERMES_REQUEST_MAX_SECONDS"):
        client.ask("ping")
    assert time.monotonic() - started < 3.0


# --- privacy invariant ---------------------------------------------------------


def test_ask_does_not_log_prompt_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Privacy invariant: prompt body never appears in INFO logs."""
    _install_stream(monkeypatch, _FakeResponse(200, [_delta("ok"), "data: [DONE]"]))
    with caplog.at_level("INFO", logger="hermes_mcp.hermes_client"):
        _client().ask("VERY-PRIVATE-PROMPT-CONTENT-XYZ")
    for rec in caplog.records:
        assert "VERY-PRIVATE-PROMPT-CONTENT-XYZ" not in rec.message


# --- profile routing ------------------------------------------------------------


def _profile_client() -> HermesClient:
    return HermesClient(
        api_url="http://127.0.0.1:8642",
        api_key="k" * 32,
        model="hermes-agent",
        timeout_seconds=60,
        profile_routes={
            "mgr-plattform": "http://127.0.0.1:8642/p/mgr-plattform",
            "planer": "http://127.0.0.1:8642/p/planer",
        },
        profile_keys={"mgr-plattform": "p" * 32},
    )


def test_ask_routes_profile_to_profile_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _install_stream(
        monkeypatch, _FakeResponse(200, [_delta("ok"), "data: [DONE]"]), captured=captured
    )
    out = _profile_client().ask("ping", profile="mgr-plattform")
    assert out == "ok"
    assert captured["url"] == "http://127.0.0.1:8642/p/mgr-plattform/v1/chat/completions"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["headers"]["Authorization"] == "Bearer " + "p" * 32  # type: ignore[index]


def test_ask_profile_falls_back_to_base_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """planer has a route but no HERMES_API_KEY_PLANER -> base key is used."""
    captured: dict[str, object] = {}
    _install_stream(
        monkeypatch, _FakeResponse(200, [_delta("ok"), "data: [DONE]"]), captured=captured
    )
    client = HermesClient(
        api_url="http://127.0.0.1:8642",
        api_key="k" * 32,
        model="hermes-agent",
        timeout_seconds=60,
        profile_routes={"planer": "http://127.0.0.1:8642/p/planer"},
    )
    assert client.ask("ping", profile="planer") == "ok"
    headers = captured["kwargs"]["headers"]  # type: ignore[index]
    assert headers["Authorization"] == "Bearer " + "k" * 32


def test_ask_profile_default_and_none_hit_base_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStream:
        urls.append(url)
        return _FakeStream(_FakeResponse(200, [_delta("ok"), "data: [DONE]"]))

    monkeypatch.setattr("hermes_mcp.hermes_client.httpx.stream", fake_stream)
    client = _profile_client()
    client.ask("ping")
    client.ask("ping", profile=None)
    client.ask("ping", profile="default")
    assert urls == ["http://127.0.0.1:8642/v1/chat/completions"] * 3


def test_ask_unknown_profile_rejected_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is the boundary: unknown names fail fast and must not
    silently fall back to the base endpoint."""

    def fail_stream(*args: object, **kwargs: object) -> _FakeStream:
        raise AssertionError("httpx.stream must not be called for unknown profiles")

    monkeypatch.setattr("hermes_mcp.hermes_client.httpx.stream", fail_stream)
    with pytest.raises(HermesError, match="unknown profile 'mgr-voice'"):
        _profile_client().ask("ping", profile="mgr-voice")


def test_ask_profile_without_routes_rejects_everything_but_default() -> None:
    """A bridge configured without HERMES_PROFILE_ROUTES stays default-only:
    any profile name is rejected (fail closed)."""
    with pytest.raises(HermesError, match="unknown profile 'planer'"):
        _client().ask("ping", profile="planer")

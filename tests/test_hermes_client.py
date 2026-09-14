from __future__ import annotations

import httpx
import pytest

from hermes_mcp.hermes_client import HermesClient, HermesError


def _client(timeout: int = 60) -> HermesClient:
    return HermesClient(
        api_url="http://127.0.0.1:8642",
        api_key="k" * 32,
        model="hermes-agent",
        timeout_seconds=timeout,
    )


def _ok_response(content: str = "the answer") -> dict[str, object]:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "hermes",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def test_constructor_validates_required_args() -> None:
    with pytest.raises(ValueError, match="api_url is required"):
        HermesClient(api_url="", api_key="k", model="m", timeout_seconds=10)
    with pytest.raises(ValueError, match="api_key is required"):
        HermesClient(api_url="http://localhost", api_key="", model="m", timeout_seconds=10)


def test_ask_posts_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return httpx.Response(200, json=_ok_response("hi from hermes"))

    monkeypatch.setattr("hermes_mcp.hermes_client.httpx.post", fake_post)
    out = _client().ask("ping")
    assert out == "hi from hermes"
    assert captured["url"] == "http://127.0.0.1:8642/v1/chat/completions"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["json"] == {  # type: ignore[index]
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
    }
    assert kwargs["headers"]["Authorization"] == "Bearer " + "k" * 32  # type: ignore[index]
    assert "X-Hermes-Session-Id" not in kwargs["headers"]  # type: ignore[index]


def test_ask_forwards_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured["kwargs"] = kwargs
        return httpx.Response(200, json=_ok_response())

    monkeypatch.setattr("hermes_mcp.hermes_client.httpx.post", fake_post)
    _client().ask("ping", session_id="sess-1")
    headers = captured["kwargs"]["headers"]  # type: ignore[index]
    assert headers["X-Hermes-Session-Id"] == "sess-1"


def test_ask_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_mcp.hermes_client.httpx.post",
        lambda *a, **kw: httpx.Response(200, json=_ok_response("  spaced out  \n")),
    )
    assert _client().ask("ping") == "spaced out"


def test_ask_translates_401_to_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_mcp.hermes_client.httpx.post",
        lambda *a, **kw: httpx.Response(401, text="unauthorized"),
    )
    with pytest.raises(HermesError, match="rejected the API key"):
        _client().ask("ping")


def test_ask_propagates_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_mcp.hermes_client.httpx.post",
        lambda *a, **kw: httpx.Response(500, text="boom"),
    )
    with pytest.raises(HermesError, match="returned HTTP 500"):
        _client().ask("ping")


def test_ask_translates_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    monkeypatch.setattr("hermes_mcp.hermes_client.httpx.post", boom)
    with pytest.raises(HermesError, match="timed out after"):
        _client().ask("ping")


def test_ask_translates_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("hermes_mcp.hermes_client.httpx.post", boom)
    with pytest.raises(HermesError, match="request failed"):
        _client().ask("ping")


def test_ask_handles_malformed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_mcp.hermes_client.httpx.post",
        lambda *a, **kw: httpx.Response(200, json={"choices": []}),
    )
    with pytest.raises(HermesError, match="malformed"):
        _client().ask("ping")


def test_ask_rejects_non_string_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some OpenAI-compatible servers return content as a list of segments;
    we don't try to flatten — fail fast so the operator notices."""

    def fake_post(*_a: object, **_kw: object) -> httpx.Response:
        body = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                    "finish_reason": "stop",
                }
            ]
        }
        return httpx.Response(200, json=body)

    monkeypatch.setattr("hermes_mcp.hermes_client.httpx.post", fake_post)
    with pytest.raises(HermesError, match="content was list"):
        _client().ask("ping")


def test_ask_does_not_echo_response_body_in_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misbehaving gateway must not be able to inject content into the
    user-visible error string. Body lands in DEBUG only."""
    secret_marker = "GATEWAY_RESPONSE_BODY_SHOULD_NOT_LEAK"
    monkeypatch.setattr(
        "hermes_mcp.hermes_client.httpx.post",
        lambda *a, **kw: httpx.Response(500, text=secret_marker),
    )
    try:
        _client().ask("ping")
    except HermesError as exc:
        assert secret_marker not in str(exc)
    else:
        raise AssertionError("expected HermesError")


def test_ask_does_not_log_prompt_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Privacy invariant: prompt body never appears in INFO logs."""
    monkeypatch.setattr(
        "hermes_mcp.hermes_client.httpx.post",
        lambda *a, **kw: httpx.Response(200, json=_ok_response()),
    )
    with caplog.at_level("INFO", logger="hermes_mcp.hermes_client"):
        _client().ask("VERY-PRIVATE-PROMPT-CONTENT-XYZ")
    for rec in caplog.records:
        assert "VERY-PRIVATE-PROMPT-CONTENT-XYZ" not in rec.message


async def test_async_request_preserves_session_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    actual_client = httpx.AsyncClient
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_response(" async answer "))

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: actual_client(transport=httpx.MockTransport(respond), **kw),
    )
    assert await _client().ask_async("hello", session_id="durable-session") == "async answer"
    assert seen[0].headers["X-Hermes-Session-Id"] == "durable-session"
    assert seen[0].headers["Authorization"] == "Bearer " + "k" * 32
    assert json.loads(seen[0].content)["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize(
    "kind,match",
    [
        ("timeout", "timed out after 60s"),
        ("unauthorized", "rejected the API key"),
        ("server", "HTTP 503"),
        ("malformed", "malformed response"),
        ("invalid_content", "response.content was int"),
    ],
)
async def test_async_error_parity(monkeypatch: pytest.MonkeyPatch, kind: str, match: str) -> None:
    actual_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        if kind == "timeout":
            raise httpx.ReadTimeout("fixture", request=request)
        if kind == "unauthorized":
            return httpx.Response(401)
        if kind == "server":
            return httpx.Response(503, text="private gateway output")
        if kind == "invalid_content":
            return httpx.Response(200, json={"choices": [{"message": {"content": 42}}]})
        return httpx.Response(200, text="private malformed output")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: actual_client(transport=httpx.MockTransport(respond), **kw),
    )
    with pytest.raises(HermesError, match=match) as exc:
        await _client().ask_async("hello")
    assert "private" not in str(exc.value)

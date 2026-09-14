"""HTTP client for the Hermes Agent gateway's OpenAI-compatible chat API.

Each `hermes_ask` call becomes a `POST /v1/chat/completions` to the running
hermes-gateway (default `http://127.0.0.1:8642`). The gateway runs the same
`AIAgent.run_conversation` loop that drives Telegram, so any MCP client
(Claude, Codex, Cursor, ...) — talking through this bridge — gets the same
brain (skills, sessions, tool execution) Hermes already has loaded.

Security:
  - HTTPS-or-localhost; the gateway is bound to 127.0.0.1 by default.
  - Bearer auth via `API_SERVER_KEY` (configured here as `HERMES_API_KEY`).
  - Prompt bodies are NOT logged at INFO level (privacy by default).
  - Gateway response bodies are NOT echoed in user-visible errors.
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)


class HermesError(Exception):
    """Raised when a hermes gateway call fails."""


class HermesClient:
    """Calls the gateway's `/v1/chat/completions` endpoint."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int,
    ) -> None:
        if not api_url:
            raise ValueError("api_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        self._endpoint = api_url.rstrip("/") + "/v1/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    def ask(
        self,
        prompt: str,
        session_id: str | None = None,
        toolsets: list[str] | None = None,
    ) -> str:
        """Send `prompt` to the gateway and return its final response text.

        If `session_id` is provided, it is forwarded as `X-Hermes-Session-Id`
        so the gateway threads the call into an existing session.

        `toolsets` is accepted for backward-compat but ignored — toolset
        selection now lives in Hermes config (`platform_toolsets.api_server`).
        """
        del toolsets
        body, headers = self._request(prompt, session_id)

        try:
            response = httpx.post(
                self._endpoint,
                json=body,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise HermesError(f"hermes gateway timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise HermesError(f"hermes gateway request failed: {exc}") from exc

        return self._response_text(response)

    async def ask_async(
        self,
        prompt: str,
        session_id: str | None = None,
        toolsets: list[str] | None = None,
    ) -> str:
        """Wait for a gateway answer without blocking the MCP event loop."""
        del toolsets
        body, headers = self._request(prompt, session_id)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                response = await client.post(self._endpoint, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise HermesError(f"hermes gateway timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise HermesError(f"hermes gateway request failed: {exc}") from exc
        return self._response_text(response)

    def _request(
        self,
        prompt: str,
        session_id: str | None,
    ) -> tuple[dict[str, object], dict[str, str]]:
        body: dict[str, object] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id

        logger.info(
            "hermes invoke endpoint=%s prompt_chars=%d session=%s timeout=%ds",
            self._endpoint,
            len(prompt),
            "y" if session_id else "n",
            self._timeout,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("hermes request body: %s", body)

        return body, headers

    def _response_text(self, response: httpx.Response) -> str:
        if response.status_code == 401:
            raise HermesError(
                "hermes gateway rejected the API key (401). "
                "Check HERMES_API_KEY matches API_SERVER_KEY in ~/.hermes/.env."
            )
        if response.status_code != 200:
            # Don't echo the body — a misbehaving gateway could put sensitive
            # content (or attacker-controlled bytes) there. Body lands in DEBUG.
            logger.debug("hermes gateway error body: %s", response.text[:1000])
            raise HermesError(
                f"hermes gateway returned HTTP {response.status_code}; "
                "see DEBUG logs for the response body."
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise HermesError(f"hermes gateway returned malformed response: {exc}") from exc

        if not isinstance(content, str):
            raise HermesError(f"hermes gateway response.content was {type(content).__name__}")

        return content.strip()

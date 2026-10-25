"""HTTP client for the Hermes Agent gateway's OpenAI-compatible chat API.

Each `hermes_ask` call becomes a streaming (`stream: true`) `POST
/v1/chat/completions` to the running hermes-gateway (default
`http://127.0.0.1:8642`). The gateway runs the same
`AIAgent.run_conversation` loop that drives Telegram, so any MCP client
(Claude, Codex, Cursor, ...) — talking through this bridge — gets the same
brain (skills, sessions, tool execution) Hermes already has loaded.

Why streaming: an agentic turn can legitimately take many minutes (tool
calls, several model rounds). A single wall-clock timeout on the whole
request kills exactly those long turns, even while the gateway is visibly
working. The gateway therefore emits SSE deltas plus a `: keepalive`
comment line every ~10s of silence; we bound the *silence* between two
events (inactivity) instead of the total duration.

Security:
  - HTTPS-or-localhost; the gateway is bound to 127.0.0.1 by default.
  - Bearer auth via `API_SERVER_KEY` (configured here as `HERMES_API_KEY`).
  - Prompt bodies are NOT logged at INFO level (privacy by default).
  - Gateway response bodies are NOT echoed in user-visible errors.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

# Fixed, deliberately small limits for the parts of the exchange that are NOT
# the agentic turn: the TCP/TLS handshake, writing the (small) request body and
# waiting for a free connection from the pool. They must never be the wall that
# kills a long but healthy turn.
CONNECT_TIMEOUT_SECONDS = 10.0
WRITE_TIMEOUT_SECONDS = 30.0
POOL_TIMEOUT_SECONDS = 30.0


class HermesError(Exception):
    """Raised when a hermes gateway call fails."""


class HermesClient:
    """Calls the gateway's `/v1/chat/completions` endpoint (streaming SSE)."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int,
        profile_routes: dict[str, str] | None = None,
        profile_keys: dict[str, str] | None = None,
        max_seconds: int = 0,
    ) -> None:
        if not api_url:
            raise ValueError("api_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_seconds < 0:
            raise ValueError("max_seconds must be zero (off) or positive")
        self._endpoint = api_url.rstrip("/") + "/v1/chat/completions"
        self._api_key = api_key
        self._model = model
        # `timeout_seconds` is the INACTIVITY limit: the maximum time we wait
        # for the next byte/event from the gateway. It is handed to httpx as
        # `read=`, never as a request-wide timeout.
        self._timeout = timeout_seconds
        # Optional hard ceiling for the whole turn (0 = off). It has to cover the
        # entire call — connection setup included — so every individual budget is
        # capped at it and a watchdog closes the stream when the deadline passes.
        self._max_seconds = max_seconds
        read_limit = float(timeout_seconds)
        self._connect_timeout = CONNECT_TIMEOUT_SECONDS
        self._write_timeout = WRITE_TIMEOUT_SECONDS
        self._pool_timeout = POOL_TIMEOUT_SECONDS
        if max_seconds:
            ceiling = float(max_seconds)
            read_limit = min(read_limit, ceiling)
            self._connect_timeout = min(self._connect_timeout, ceiling)
            self._write_timeout = min(self._write_timeout, ceiling)
            self._pool_timeout = min(self._pool_timeout, ceiling)
        self._timeout_obj = httpx.Timeout(
            connect=self._connect_timeout,
            read=read_limit,
            write=self._write_timeout,
            pool=self._pool_timeout,
        )
        # Multi-profile routing (optional). Each configured profile name is
        # allowed in `ask(profile=...)` and maps to its own endpoint; a
        # profile without its own key falls back to the base key.
        self._profile_endpoints = {
            name: url.rstrip("/") + "/v1/chat/completions"
            for name, url in (profile_routes or {}).items()
        }
        self._profile_keys = dict(profile_keys or {})

    def ask(
        self,
        prompt: str,
        session_id: str | None = None,
        toolsets: list[str] | None = None,
        profile: str | None = None,
    ) -> str:
        """Send `prompt` to the gateway and return its final response text.

        If `session_id` is provided, it is forwarded as `X-Hermes-Session-Id`
        so the gateway threads the call into an existing session.

        If `profile` is provided, the request is routed to that profile's
        endpoint (from the HERMES_PROFILE_ROUTES allowlist) with its own key
        if one is configured. `None` and `"default"` both mean the base
        gateway URL. Unknown profile names are rejected — the allowlist is
        the security boundary, not a convention.

        `toolsets` is accepted for backward-compat but ignored — toolset
        selection now lives in Hermes config (`platform_toolsets.api_server`).
        """
        del toolsets

        if profile in (None, "default"):
            endpoint = self._endpoint
            api_key = self._api_key
        else:
            endpoint = self._profile_endpoints.get(profile)
            if endpoint is None:
                raise HermesError(
                    f"unknown profile {profile!r}: not in the HERMES_PROFILE_ROUTES "
                    "allowlist configured on this server"
                )
            api_key = self._profile_keys.get(profile) or self._api_key

        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id

        logger.info(
            "hermes invoke endpoint=%s prompt_chars=%d session=%s "
            "inactivity_timeout=%ds max_seconds=%s",
            endpoint,
            len(prompt),
            "y" if session_id else "n",
            self._timeout,
            self._max_seconds or "off",
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("hermes request body: %s", body)

        started = time.monotonic()
        chunks: list[str] = []
        finish_reason: str | None = None
        saw_done = False
        ceiling_hit = threading.Event()
        watchdog: threading.Timer | None = None
        holder: dict[str, httpx.Response] = {}

        def ceiling_error() -> HermesError:
            return HermesError(
                "hermes gateway turn exceeded "
                f"HERMES_REQUEST_MAX_SECONDS ({self._max_seconds}s)"
            )

        if self._max_seconds:
            # The ceiling has to cover the whole call, so the watchdog starts
            # before the request. It closes the response as soon as one exists;
            # the phase budgets are capped at the same value, and the flag is
            # re-checked right after the response arrives.
            def _on_ceiling() -> None:
                ceiling_hit.set()
                pending = holder.get("response")
                if pending is not None:
                    try:
                        pending.close()
                    except Exception:  # noqa: BLE001 - best effort
                        pass

            watchdog = threading.Timer(float(self._max_seconds), _on_ceiling)
            watchdog.daemon = True
            watchdog.start()

        try:
            with httpx.stream(
                "POST",
                endpoint,
                json=body,
                headers=headers,
                timeout=self._timeout_obj,
                follow_redirects=False,
            ) as response:
                holder["response"] = response
                if ceiling_hit.is_set():
                    raise ceiling_error()
                if response.status_code == 401:
                    raise HermesError(
                        "hermes gateway rejected the API key (401). "
                        "Check HERMES_API_KEY matches API_SERVER_KEY in ~/.hermes/.env."
                    )
                if response.status_code != 200:
                    # Don't echo the body — a misbehaving gateway could put
                    # sensitive content (or attacker-controlled bytes) there.
                    # Body lands in DEBUG only.
                    if logger.isEnabledFor(logging.DEBUG):
                        try:
                            raw = response.read()[:1000]
                        except Exception:  # noqa: BLE001 - diagnostics only
                            raw = b""
                        logger.debug("hermes gateway error body: %s", raw)
                    raise HermesError(
                        f"hermes gateway returned HTTP {response.status_code}; "
                        "see DEBUG logs for the response body."
                    )

                for line in response.iter_lines():
                    # Checked on EVERY line, before any parsing: keepalives and
                    # unparsable frames are traffic too, and an operator's hard
                    # ceiling must not be escaped by a chatty gateway.
                    if ceiling_hit.is_set() or (
                        self._max_seconds
                        and (time.monotonic() - started) > self._max_seconds
                    ):
                        raise ceiling_error()
                    if not line:
                        continue
                    if line.startswith(":"):
                        # SSE comment line — the gateway's `: keepalive` during
                        # silence. It proves liveness and must never become
                        # part of the answer text.
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        saw_done = True
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.debug("hermes gateway sent unparsable SSE data: %r", payload[:200])
                        continue
                    if not isinstance(event, dict):
                        continue
                    for choice in event.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta")
                        if isinstance(delta, dict):
                            piece = delta.get("content")
                            if isinstance(piece, str):
                                chunks.append(piece)
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
        except httpx.ConnectTimeout as exc:
            raise HermesError(
                "hermes gateway could not be reached within "
                f"{self._connect_timeout:g}s"
            ) from exc
        except (httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            raise HermesError(
                "hermes gateway request could not be sent within "
                f"{self._write_timeout:g}s (write/pool timeout)"
            ) from exc
        except httpx.TimeoutException as exc:
            if ceiling_hit.is_set() or (
                self._max_seconds and (time.monotonic() - started) >= self._max_seconds
            ):
                # The read budget was cut down to the ceiling and/or the watchdog
                # already closed the stream: this is the operator's wall, not
                # merely a silence.
                raise ceiling_error() from exc
            raise HermesError(
                f"hermes gateway sent no events for {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            if ceiling_hit.is_set():
                raise ceiling_error() from exc
            raise HermesError(f"hermes gateway request failed: {exc}") from exc
        finally:
            if watchdog is not None:
                watchdog.cancel()

        if ceiling_hit.is_set():
            raise ceiling_error()

        if not saw_done and finish_reason is None:
            # The gateway closed the stream without a terminator: the answer may
            # be truncated. Fail like the old non-streaming path did instead of
            # handing back a silently shortened reply.
            raise HermesError(
                "hermes gateway closed the stream before the answer was complete "
                f"(no [DONE], no finish_reason; {len(''.join(chunks))} chars received)"
            )

        content = "".join(chunks)
        if not saw_done:
            logger.debug(
                "hermes gateway stream ended without [DONE] (finish_reason=%s, chars=%d)",
                finish_reason,
                len(content),
            )
        return content.strip()

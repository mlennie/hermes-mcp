"""Environment-variable configuration for hermes-mcp.

All knobs documented in `.env.example`. The server refuses to start if any
of OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, OAUTH_ISSUER_URL, or HERMES_API_KEY
is missing.

Secrets (OAUTH_CLIENT_SECRET, HERMES_API_KEY, MCP_BEARER_TOKEN) are excluded
from `Config.__repr__` so a stray `logger.debug(config)` cannot leak them.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from hermes_mcp.oauth import DEFAULT_ALLOWED_REDIRECT_URIS as _DEFAULT_URIS
from hermes_mcp.oauth import normalize_redirect_uri

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_VALID_LOG_LEVELS: frozenset[str] = frozenset(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))

# Re-export the canonical default from oauth.py as a sorted tuple. The
# constant lives in oauth.py because it is fundamentally an OAuth concern;
# the re-export here gives `test_config` a stable name to assert against
# and guarantees the env-var default and `StaticClientProvider` default
# cannot drift apart.
DEFAULT_OAUTH_ALLOWED_REDIRECT_URIS: tuple[str, ...] = tuple(sorted(_DEFAULT_URIS))

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_allowed_redirect_uri_form(uri: str) -> bool:
    """`https://...`, or `http://` to a loopback host (for local testing)."""
    try:
        parts = urlsplit(uri)
        normalize_redirect_uri(uri)  # must also parse the way the provider will
    except ValueError:  # includes pydantic.ValidationError
        return False
    if not parts.netloc:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    oauth_client_id: str
    oauth_client_secret: str = field(repr=False)
    oauth_issuer_url: str
    hermes_api_url: str
    hermes_api_key: str = field(repr=False)
    hermes_model: str
    hermes_request_timeout_seconds: int
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    # Exact OAuth redirect URIs `/authorize` may send a code to.
    allowed_redirect_uris: tuple[str, ...]
    # Optional static bearer token. When set, the server accepts
    # `Authorization: Bearer <token>` directly, in addition to OAuth-issued
    # access tokens. Necessary for MCP clients that have no OAuth flow or
    # only support public (PKCE-only) OAuth clients (Codex, Cursor's
    # `headers` config), since the OAuth client here is confidential.
    mcp_bearer_token: str | None = field(repr=False)
    log_level: LogLevel

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = env if env is not None else os.environ

        client_id = (e.get("OAUTH_CLIENT_ID") or "").strip()
        if not client_id:
            raise ConfigError(
                "OAUTH_CLIENT_ID is required. Generate one with: hermes-mcp mint-client"
            )

        client_secret = (e.get("OAUTH_CLIENT_SECRET") or "").strip()
        if not client_secret:
            raise ConfigError(
                "OAUTH_CLIENT_SECRET is required. Generate one with: hermes-mcp mint-client"
            )
        if len(client_secret) < 32:
            raise ConfigError("OAUTH_CLIENT_SECRET must be at least 32 characters")

        issuer_url = (e.get("OAUTH_ISSUER_URL") or "").strip().rstrip("/")
        if not issuer_url:
            raise ConfigError(
                "OAUTH_ISSUER_URL is required (your public tunnel URL, "
                "e.g. https://hermes.example.com)"
            )
        if not (issuer_url.startswith("https://") or issuer_url.startswith("http://localhost")):
            raise ConfigError(
                f"OAUTH_ISSUER_URL must be HTTPS (or http://localhost for testing), got {issuer_url}"
            )

        hermes_api_url = (e.get("HERMES_API_URL") or "http://127.0.0.1:8642").strip().rstrip("/")
        if not (hermes_api_url.startswith("http://") or hermes_api_url.startswith("https://")):
            raise ConfigError(f"HERMES_API_URL must be http:// or https://, got {hermes_api_url}")

        hermes_api_key = (e.get("HERMES_API_KEY") or "").strip()
        if not hermes_api_key:
            raise ConfigError(
                "HERMES_API_KEY is required (the bearer token for the Hermes gateway "
                "OpenAI-compatible API; check ~/.hermes/.env for API_SERVER_KEY)."
            )

        hermes_model = (e.get("HERMES_MODEL") or "hermes-agent").strip()

        try:
            port = int(e.get("BIND_PORT", "8765"))
        except ValueError as exc:
            raise ConfigError(f"BIND_PORT must be an integer, got {e.get('BIND_PORT')!r}") from exc
        if not 1 <= port <= 65535:
            raise ConfigError(f"BIND_PORT must be in 1..65535, got {port}")

        try:
            request_timeout = int(e.get("HERMES_REQUEST_TIMEOUT_SECONDS", "300"))
        except ValueError as exc:
            raise ConfigError(
                "HERMES_REQUEST_TIMEOUT_SECONDS must be an integer, got "
                f"{e.get('HERMES_REQUEST_TIMEOUT_SECONDS')!r}"
            ) from exc
        if request_timeout <= 0:
            raise ConfigError(
                f"HERMES_REQUEST_TIMEOUT_SECONDS must be positive, got {request_timeout}"
            )

        allowed_hosts_raw = (e.get("MCP_ALLOWED_HOSTS") or "").strip()
        allowed_hosts = tuple(h.strip() for h in allowed_hosts_raw.split(",") if h.strip())

        # Exact OAuth redirect URIs a code may be sent to. Default: Claude's
        # connector callbacks. Each entry must be https:// (or http:// to a
        # loopback host, for testing). An input that parses to an empty list
        # (whitespace-only, comma-only, ...) falls back to the default so a
        # typo can't lock everyone out.
        uris_raw = e.get("OAUTH_ALLOWED_REDIRECT_URIS") or ""
        uris = tuple(u.strip() for u in uris_raw.split(",") if u.strip())
        for u in uris:
            if not _is_allowed_redirect_uri_form(u):
                raise ConfigError(
                    "OAUTH_ALLOWED_REDIRECT_URIS entries must be https:// URLs "
                    f"(or http://localhost for testing), got {u!r}"
                )
        allowed_redirect_uris = uris or DEFAULT_OAUTH_ALLOWED_REDIRECT_URIS

        # Deprecated: the scheme-based allowlist was replaced by exact
        # redirect-URI pinning. Still accepted (so an existing env file does
        # not stop the server from starting) but ignored.
        if (e.get("OAUTH_ALLOWED_REDIRECT_SCHEMES") or "").strip():
            logging.getLogger(__name__).warning(
                "OAUTH_ALLOWED_REDIRECT_SCHEMES is deprecated and ignored: redirect "
                "URIs are now pinned exactly. Use OAUTH_ALLOWED_REDIRECT_URIS "
                "(comma-separated https:// URLs) if you need callbacks other than "
                "Claude's."
            )

        # Static bearer token (optional). Coexists with OAuth: both auth
        # methods are accepted at /mcp. Mostly useful for clients that have
        # no OAuth flow in their UI (Codex desktop's custom-MCP form, Cursor's
        # `headers` block). Min 32 chars to enforce real entropy — bearer
        # tokens are long-lived shared secrets and the only gate behind the
        # tunnel URL, so weak ones are catastrophic.
        bearer_token_raw = (e.get("MCP_BEARER_TOKEN") or "").strip()
        mcp_bearer_token: str | None
        if bearer_token_raw:
            if len(bearer_token_raw) < 32:
                raise ConfigError(
                    "MCP_BEARER_TOKEN must be at least 32 characters when set "
                    "(generate one with: hermes-mcp mint-bearer-token)"
                )
            mcp_bearer_token = bearer_token_raw
        else:
            mcp_bearer_token = None

        log_level_raw = (e.get("LOG_LEVEL") or "INFO").upper()
        if log_level_raw not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL, got {log_level_raw}"
            )
        log_level: LogLevel = log_level_raw  # type: ignore[assignment]

        bind_host = (e.get("BIND_HOST") or "127.0.0.1").strip()
        if bind_host not in ("127.0.0.1", "::1", "localhost"):
            logger = logging.getLogger(__name__)
            logger.warning(
                "BIND_HOST=%r is not loopback. The bridge expects a tunnel "
                "to reach it on localhost; binding elsewhere exposes the "
                "OAuth and tool endpoints to anyone who can reach this host.",
                bind_host,
            )

        return cls(
            oauth_client_id=client_id,
            oauth_client_secret=client_secret,
            oauth_issuer_url=issuer_url,
            hermes_api_url=hermes_api_url,
            hermes_api_key=hermes_api_key,
            hermes_model=hermes_model,
            hermes_request_timeout_seconds=request_timeout,
            bind_host=bind_host,
            bind_port=port,
            allowed_hosts=allowed_hosts,
            allowed_redirect_uris=allowed_redirect_uris,
            mcp_bearer_token=mcp_bearer_token,
            log_level=log_level,
        )


def configure_logging(level: LogLevel) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

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
    # Hard ceiling for one agentic turn in seconds; 0 means "no ceiling".
    # `hermes_request_timeout_seconds` above bounds the SILENCE between two
    # gateway events, this bounds the whole turn for operators who want a wall.
    hermes_request_max_seconds: int
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
    # Optional per-profile routing table for multi-profile gateways
    # (HERMES_PROFILE_ROUTES): maps a profile name to that profile's base
    # gateway URL. `default` is always allowed and means the base
    # HERMES_API_URL, so it does not need an entry. Mutable on purpose —
    # Config instances are not hashed.
    profile_routes: dict[str, str] = field(default_factory=dict)
    # Optional per-profile API keys (HERMES_API_KEY_<PROFILE>, `-` in the
    # profile name becomes `_` in the env var name). Falls back to
    # HERMES_API_KEY for profiles without their own key.
    profile_keys: dict[str, str] = field(default_factory=dict)

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

        try:
            request_max = int(e.get("HERMES_REQUEST_MAX_SECONDS", "0"))
        except ValueError as exc:
            raise ConfigError(
                "HERMES_REQUEST_MAX_SECONDS must be an integer, got "
                f"{e.get('HERMES_REQUEST_MAX_SECONDS')!r}"
            ) from exc
        if request_max < 0:
            raise ConfigError(
                "HERMES_REQUEST_MAX_SECONDS must be 0 (no ceiling) or positive, "
                f"got {request_max}"
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

        # Optional multi-profile routing (HERMES_PROFILE_ROUTES): a
        # comma-separated list of `name=url` entries. Each name becomes an
        # allowed `profile` value for `hermes_ask`, and its requests are
        # routed to `<url>/v1/chat/completions` — this is how a single
        # bridge reaches per-profile gateways (e.g. the /p/<name>/ routes
        # of a Hermes multi-profile gateway). `default` is always allowed
        # and means the base HERMES_API_URL; listing it is unnecessary.
        profile_routes: dict[str, str] = {}
        routes_raw = (e.get("HERMES_PROFILE_ROUTES") or "").strip()
        if routes_raw:
            for part in routes_raw.split(","):
                part = part.strip()
                if not part:
                    continue
                if "=" not in part:
                    raise ConfigError(
                        "HERMES_PROFILE_ROUTES entries must be name=url "
                        f"(comma-separated), got {part!r}"
                    )
                name, url = part.split("=", 1)
                name = name.strip().lower()
                url = url.strip().rstrip("/")
                if not name or not url:
                    raise ConfigError(
                        f"HERMES_PROFILE_ROUTES entry {part!r} has an empty name or url"
                    )
                if not (url.startswith("http://") or url.startswith("https://")):
                    raise ConfigError(
                        f"HERMES_PROFILE_ROUTES url for profile {name!r} must be "
                        f"http:// or https://, got {url!r}"
                    )
                if name == "default":
                    logging.getLogger(__name__).warning(
                        "HERMES_PROFILE_ROUTES contains a 'default' entry; 'default' "
                        "always means the base HERMES_API_URL and the entry is ignored."
                    )
                    continue
                if name in profile_routes:
                    raise ConfigError(
                        f"HERMES_PROFILE_ROUTES has duplicate profile name {name!r}"
                    )
                profile_routes[name] = url

        # Optional per-profile API keys (HERMES_API_KEY_<PROFILE>): the
        # profile name maps into the env var name with `-` replaced by `_`
        # and upper-cased (mgr-plattform -> HERMES_API_KEY_MGR_PLATTFORM).
        # A profile without its own key falls back to HERMES_API_KEY.
        profile_keys: dict[str, str] = {}
        key_prefix = "HERMES_API_KEY_"
        for env_name, value in e.items():
            if not env_name.startswith(key_prefix):
                continue
            profile_name = env_name[len(key_prefix):].lower().replace("_", "-")
            if not profile_name or not value.strip():
                continue
            if profile_name not in profile_routes:
                raise ConfigError(
                    f"{env_name} defines a key for profile {profile_name!r}, but that "
                    "profile is not listed in HERMES_PROFILE_ROUTES"
                )
            profile_keys[profile_name] = value.strip()
        for profile_name in profile_routes:
            if profile_name not in profile_keys:
                logging.getLogger(__name__).warning(
                    "profile %r has no HERMES_API_KEY_%s; requests fall back to the "
                    "base HERMES_API_KEY",
                    profile_name,
                    profile_name.upper().replace("-", "_"),
                )

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
            hermes_request_max_seconds=request_max,
            bind_host=bind_host,
            bind_port=port,
            allowed_hosts=allowed_hosts,
            allowed_redirect_uris=allowed_redirect_uris,
            mcp_bearer_token=mcp_bearer_token,
            log_level=log_level,
            profile_routes=profile_routes,
            profile_keys=profile_keys,
        )


def configure_logging(level: LogLevel) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

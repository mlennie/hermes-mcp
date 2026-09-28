from __future__ import annotations

import pytest

from hermes_mcp.config import (
    DEFAULT_OAUTH_ALLOWED_REDIRECT_URIS,
    Config,
    ConfigError,
)

CLAUDE_CALLBACKS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)

VALID_BASE: dict[str, str] = {
    "OAUTH_CLIENT_ID": "hermes-mcp-test",
    "OAUTH_CLIENT_SECRET": "x" * 32,
    "OAUTH_ISSUER_URL": "https://hermes.example.com",
    "HERMES_API_KEY": "k" * 32,
}


def test_requires_oauth_client_id() -> None:
    env = {**VALID_BASE}
    env.pop("OAUTH_CLIENT_ID")
    with pytest.raises(ConfigError, match="OAUTH_CLIENT_ID is required"):
        Config.from_env(env)


def test_requires_oauth_client_secret() -> None:
    env = {**VALID_BASE}
    env.pop("OAUTH_CLIENT_SECRET")
    with pytest.raises(ConfigError, match="OAUTH_CLIENT_SECRET is required"):
        Config.from_env(env)


def test_short_client_secret_rejected() -> None:
    env = {**VALID_BASE, "OAUTH_CLIENT_SECRET": "short"}
    with pytest.raises(ConfigError, match="at least 32 characters"):
        Config.from_env(env)


def test_requires_issuer_url() -> None:
    env = {**VALID_BASE}
    env.pop("OAUTH_ISSUER_URL")
    with pytest.raises(ConfigError, match="OAUTH_ISSUER_URL is required"):
        Config.from_env(env)


def test_requires_hermes_api_key() -> None:
    env = {**VALID_BASE}
    env.pop("HERMES_API_KEY")
    with pytest.raises(ConfigError, match="HERMES_API_KEY is required"):
        Config.from_env(env)


def test_issuer_url_must_be_https_or_localhost() -> None:
    env = {**VALID_BASE, "OAUTH_ISSUER_URL": "http://example.com"}
    with pytest.raises(ConfigError, match="must be HTTPS"):
        Config.from_env(env)


def test_localhost_http_issuer_allowed() -> None:
    cfg = Config.from_env({**VALID_BASE, "OAUTH_ISSUER_URL": "http://localhost:8765"})
    assert cfg.oauth_issuer_url == "http://localhost:8765"


def test_issuer_url_trailing_slash_stripped() -> None:
    cfg = Config.from_env({**VALID_BASE, "OAUTH_ISSUER_URL": "https://hermes.example.com/"})
    assert cfg.oauth_issuer_url == "https://hermes.example.com"


def test_minimal_valid_config() -> None:
    cfg = Config.from_env(VALID_BASE)
    assert cfg.oauth_client_id == "hermes-mcp-test"
    assert cfg.oauth_client_secret == "x" * 32
    assert cfg.oauth_issuer_url == "https://hermes.example.com"
    assert cfg.hermes_api_url == "http://127.0.0.1:8642"
    assert cfg.hermes_api_key == "k" * 32
    assert cfg.hermes_model == "hermes-agent"
    assert cfg.hermes_request_timeout_seconds == 300
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.bind_port == 8765
    assert cfg.allowed_hosts == ()
    assert cfg.allowed_redirect_uris == DEFAULT_OAUTH_ALLOWED_REDIRECT_URIS
    assert cfg.log_level == "INFO"


def test_hermes_api_url_validated() -> None:
    with pytest.raises(ConfigError, match="must be http:// or https://"):
        Config.from_env({**VALID_BASE, "HERMES_API_URL": "ftp://nope"})


def test_hermes_api_url_trailing_slash_stripped() -> None:
    cfg = Config.from_env({**VALID_BASE, "HERMES_API_URL": "http://127.0.0.1:8642/"})
    assert cfg.hermes_api_url == "http://127.0.0.1:8642"


def test_hermes_model_override() -> None:
    cfg = Config.from_env({**VALID_BASE, "HERMES_MODEL": "hermes"})
    assert cfg.hermes_model == "hermes"


def test_port_range_validated() -> None:
    with pytest.raises(ConfigError, match=r"BIND_PORT must be in 1\.\.65535"):
        Config.from_env({**VALID_BASE, "BIND_PORT": "0"})
    with pytest.raises(ConfigError, match=r"BIND_PORT must be in 1\.\.65535"):
        Config.from_env({**VALID_BASE, "BIND_PORT": "70000"})


def test_port_must_be_integer() -> None:
    with pytest.raises(ConfigError, match="BIND_PORT must be an integer"):
        Config.from_env({**VALID_BASE, "BIND_PORT": "abc"})


def test_request_timeout_validated() -> None:
    with pytest.raises(ConfigError, match="HERMES_REQUEST_TIMEOUT_SECONDS must be positive"):
        Config.from_env({**VALID_BASE, "HERMES_REQUEST_TIMEOUT_SECONDS": "0"})


def test_allowed_hosts_parsed() -> None:
    cfg = Config.from_env(
        {**VALID_BASE, "MCP_ALLOWED_HOSTS": "hermes.example.com,foo.trycloudflare.com"}
    )
    assert cfg.allowed_hosts == ("hermes.example.com", "foo.trycloudflare.com")


def test_log_level_validated() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL must be one of"):
        Config.from_env({**VALID_BASE, "LOG_LEVEL": "VERBOSE"})


def test_log_level_normalized_to_upper() -> None:
    cfg = Config.from_env({**VALID_BASE, "LOG_LEVEL": "debug"})
    assert cfg.log_level == "DEBUG"


# --- OAUTH_ALLOWED_REDIRECT_URIS -----------------------------------------------


def test_allowed_redirect_uris_default_to_claude_callbacks() -> None:
    cfg = Config.from_env(VALID_BASE)
    assert cfg.allowed_redirect_uris == CLAUDE_CALLBACKS


def test_allowed_redirect_uris_parsed_with_whitespace() -> None:
    cfg = Config.from_env(
        {
            **VALID_BASE,
            "OAUTH_ALLOWED_REDIRECT_URIS": (
                " https://claude.ai/api/mcp/auth_callback , http://localhost:6274/cb "
            ),
        }
    )
    assert cfg.allowed_redirect_uris == (
        "https://claude.ai/api/mcp/auth_callback",
        "http://localhost:6274/cb",
    )


def test_allowed_redirect_uris_replaces_default_when_set() -> None:
    """Explicit env var fully replaces the default — it's not additive."""
    cfg = Config.from_env(
        {**VALID_BASE, "OAUTH_ALLOWED_REDIRECT_URIS": "https://app.example.com/cb"}
    )
    assert cfg.allowed_redirect_uris == ("https://app.example.com/cb",)


def test_allowed_redirect_uris_empty_falls_back_to_default() -> None:
    """A typo like `,` or whitespace would otherwise parse to an empty tuple
    and lock every client out. Treat an empty parse result as 'unset'."""
    for raw in ("", "  ", ",", ",,,", " , , ,"):
        cfg = Config.from_env({**VALID_BASE, "OAUTH_ALLOWED_REDIRECT_URIS": raw})
        assert cfg.allowed_redirect_uris == CLAUDE_CALLBACKS, raw


def test_allowed_redirect_uris_loopback_http_allowed() -> None:
    for uri in ("http://localhost:6274/cb", "http://127.0.0.1:9999/cb", "http://[::1]:9999/cb"):
        cfg = Config.from_env({**VALID_BASE, "OAUTH_ALLOWED_REDIRECT_URIS": uri})
        assert cfg.allowed_redirect_uris == (uri,)


def test_allowed_redirect_uris_reject_non_https() -> None:
    for bad in (
        "http://example.com/cb",
        "http://localhost.attacker.example/cb",
        "claude://oauth/callback",
        "cursor://anysphere.cursor-mcp/cb",
        "javascript:alert(1)",
        "https://",
        "not a url",
    ):
        with pytest.raises(ConfigError, match="OAUTH_ALLOWED_REDIRECT_URIS"):
            Config.from_env({**VALID_BASE, "OAUTH_ALLOWED_REDIRECT_URIS": bad})


def test_one_bad_redirect_uri_rejects_whole_list() -> None:
    with pytest.raises(ConfigError, match="OAUTH_ALLOWED_REDIRECT_URIS"):
        Config.from_env(
            {
                **VALID_BASE,
                "OAUTH_ALLOWED_REDIRECT_URIS": "https://claude.ai/api/mcp/auth_callback,http://x.example/cb",
            }
        )


def test_deprecated_redirect_schemes_var_warns_but_does_not_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deployments that still set OAUTH_ALLOWED_REDIRECT_SCHEMES must keep
    starting; the value is ignored (it no longer widens the allowlist) and a
    deprecation warning is logged."""
    with caplog.at_level("WARNING", logger="hermes_mcp.config"):
        cfg = Config.from_env({**VALID_BASE, "OAUTH_ALLOWED_REDIRECT_SCHEMES": "claude,cursor"})
    assert cfg.allowed_redirect_uris == CLAUDE_CALLBACKS
    msgs = [r.message for r in caplog.records if "OAUTH_ALLOWED_REDIRECT_SCHEMES" in r.message]
    assert len(msgs) == 1
    assert "deprecated" in msgs[0]


def test_deprecated_redirect_schemes_var_unset_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="hermes_mcp.config"):
        Config.from_env({**VALID_BASE, "OAUTH_ALLOWED_REDIRECT_SCHEMES": "  "})
    assert not [r for r in caplog.records if "OAUTH_ALLOWED_REDIRECT_SCHEMES" in r.message]


# --- repr ----------------------------------------------------------------------


def test_repr_hides_secrets() -> None:
    secret, api_key, bearer = "S" * 40, "K" * 40, "B" * 40
    cfg = Config.from_env(
        {
            **VALID_BASE,
            "OAUTH_CLIENT_SECRET": secret,
            "HERMES_API_KEY": api_key,
            "MCP_BEARER_TOKEN": bearer,
        }
    )
    r = repr(cfg)
    assert "hermes-mcp-test" in r  # non-secret fields still shown
    for value in (secret, api_key, bearer):
        assert value not in r
    for name in ("oauth_client_secret", "hermes_api_key", "mcp_bearer_token"):
        assert name not in r


# --- MCP_BEARER_TOKEN --------------------------------------------


def test_bearer_token_optional_defaults_to_none() -> None:
    cfg = Config.from_env(VALID_BASE)
    assert cfg.mcp_bearer_token is None


def test_bearer_token_parsed_when_set() -> None:
    cfg = Config.from_env({**VALID_BASE, "MCP_BEARER_TOKEN": "z" * 32})
    assert cfg.mcp_bearer_token == "z" * 32


def test_bearer_token_too_short_rejected() -> None:
    """Bearer is a long-lived shared secret; reject weak ones at startup
    rather than letting an operator paste in `password123` and call it done."""
    with pytest.raises(ConfigError, match="at least 32 characters"):
        Config.from_env({**VALID_BASE, "MCP_BEARER_TOKEN": "short"})


def test_bearer_token_whitespace_only_treated_as_unset() -> None:
    cfg = Config.from_env({**VALID_BASE, "MCP_BEARER_TOKEN": "   "})
    assert cfg.mcp_bearer_token is None

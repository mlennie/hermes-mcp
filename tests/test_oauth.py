from __future__ import annotations

import asyncio
import time
from typing import cast

import pytest
from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull
from pydantic import AnyUrl

from hermes_mcp.oauth import (
    DEFAULT_ALLOWED_REDIRECT_URIS,
    MAX_OUTSTANDING_AUTH_CODES,
    StaticClientProvider,
    mint_bearer_token,
    mint_client_credentials,
)

CLIENT_ID = "hermes-mcp-test"
CLIENT_SECRET = "s" * 48
BEARER_TOKEN = "b" * 48
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def _provider(**kwargs: int) -> StaticClientProvider:
    return StaticClientProvider(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, **kwargs)


def _params(redirect_uri: str = CLAUDE_CALLBACK, state: str | None = "st-1") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=[],
        code_challenge="dummy-challenge",
        redirect_uri=AnyUrl(redirect_uri),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def test_mint_client_credentials_unique_and_strong() -> None:
    a_id, a_secret = mint_client_credentials()
    b_id, b_secret = mint_client_credentials()
    assert a_id != b_id
    assert a_secret != b_secret
    assert a_id.startswith("hermes-mcp-")
    # token_urlsafe(32) -> >= 40 char strings
    assert len(a_secret) >= 40


def test_constructor_rejects_empty_credentials() -> None:
    with pytest.raises(ValueError, match="required"):
        StaticClientProvider(client_id="", client_secret=CLIENT_SECRET)
    with pytest.raises(ValueError, match="required"):
        StaticClientProvider(client_id=CLIENT_ID, client_secret="")


def test_get_client_returns_confidential_client() -> None:
    """The registered client is confidential: it carries the configured
    `client_secret` and `token_endpoint_auth_method="client_secret_post"`, so
    the SDK's ClientAuthenticator enforces the secret on every /token
    request. PKCE alone must NOT be enough to mint tokens: /authorize
    auto-approves, so a public client would let anyone who knows the
    client_id mint a token."""
    p = _provider()
    client = asyncio.run(p.get_client(CLIENT_ID))
    assert client is not None
    assert client.client_id == CLIENT_ID
    assert client.client_secret == CLIENT_SECRET
    assert client.token_endpoint_auth_method == "client_secret_post"


def test_get_client_unknown_returns_none() -> None:
    p = _provider()
    assert asyncio.run(p.get_client("not-the-client")) is None


def test_register_client_disabled() -> None:
    p = _provider()
    metadata = OAuthClientInformationFull(redirect_uris=[AnyUrl("https://x/cb")])
    with pytest.raises(NotImplementedError, match="Dynamic client registration"):
        asyncio.run(p.register_client(metadata))


def test_authorize_returns_redirect_with_code_and_state() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    assert redirect.startswith(CLAUDE_CALLBACK)
    assert "code=" in redirect
    assert "state=st-1" in redirect


def test_default_redirect_allowlist_is_claude_callbacks() -> None:
    assert (
        frozenset(
            {
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
            }
        )
        == DEFAULT_ALLOWED_REDIRECT_URIS
    )


def test_validate_redirect_uri_accepts_default_claude_callbacks() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    for uri in DEFAULT_ALLOWED_REDIRECT_URIS:
        assert str(client.validate_redirect_uri(AnyUrl(uri))) == uri


def test_validate_redirect_uri_rejects_unpinned_uris() -> None:
    """Only exact, pre-configured URIs are accepted. Any other https URL
    (the old scheme allowlist accepted all of them), look-alike hosts,
    extra path/query, custom schemes, and dangerous schemes are rejected, so
    /authorize can't be used as an open redirector."""
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    for evil in (
        "https://attacker.example/cb",
        "https://app.example.com/cb",
        "https://claude.ai.attacker.example/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback/extra",
        "https://claude.ai/api/mcp/auth_callback?next=https://attacker.example",
        "https://claude.ai/api/mcp/other_callback",
        "http://claude.ai/api/mcp/auth_callback",
        "http://localhost:9999/cb",
        "claude://oauth/callback",
        "cursor://anysphere.cursor-mcp/cb",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
    ):
        with pytest.raises(InvalidRedirectUriError, match="not allowed"):
            client.validate_redirect_uri(AnyUrl(evil))


def test_validate_redirect_uri_configured_list_replaces_default() -> None:
    p = StaticClientProvider(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        allowed_redirect_uris=frozenset({"https://app.example.com/cb", "http://localhost:9999/cb"}),
    )
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    assert client.validate_redirect_uri(AnyUrl("https://app.example.com/cb")) is not None
    assert client.validate_redirect_uri(AnyUrl("http://localhost:9999/cb")) is not None
    with pytest.raises(InvalidRedirectUriError, match="not allowed"):
        client.validate_redirect_uri(AnyUrl(CLAUDE_CALLBACK))


def test_configured_redirect_uris_are_normalized_like_the_request() -> None:
    """The SDK hands us the requested redirect_uri as a pydantic AnyUrl, which
    lower-cases the host and adds a `/` to an empty path. Configured entries
    get the same normalization so an operator's spelling can't silently
    fail to match."""
    p = StaticClientProvider(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        allowed_redirect_uris=frozenset({"https://App.Example.com"}),
    )
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    assert client.validate_redirect_uri(AnyUrl("https://app.example.com/")) is not None


def test_constructor_rejects_empty_redirect_allowlist() -> None:
    with pytest.raises(ValueError, match="redirect URI"):
        StaticClientProvider(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, allowed_redirect_uris=frozenset()
        )


def test_validate_redirect_uri_rejects_none() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(None)


def test_authorization_code_round_trip() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = asyncio.run(p.load_authorization_code(client, code))
    assert auth_code is not None
    assert auth_code.code == code
    assert auth_code.client_id == CLIENT_ID

    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))
    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.token_type == "Bearer"
    assert tokens.expires_in == 3600

    # Code is single-use after exchange.
    assert asyncio.run(p.load_authorization_code(client, code)) is None


def test_load_unknown_authorization_code() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    assert asyncio.run(p.load_authorization_code(client, "no-such-code")) is None


def test_access_token_verifiable_via_load_access_token() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))

    loaded = asyncio.run(p.load_access_token(tokens.access_token))
    assert loaded is not None
    assert loaded.client_id == CLIENT_ID

    assert asyncio.run(p.load_access_token("not-a-real-token")) is None


def test_expired_access_token_rejected() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))

    # Backdate the stored expiry past now without sleeping (fast + deterministic).
    stored = p._access_tokens[tokens.access_token]
    p._access_tokens[tokens.access_token] = stored.model_copy(
        update={"expires_at": int(time.time()) - 1}
    )
    assert asyncio.run(p.load_access_token(tokens.access_token)) is None


def test_refresh_token_rotates_pair() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))

    rt = asyncio.run(p.load_refresh_token(client, tokens.refresh_token or ""))
    assert rt is not None

    new_tokens = asyncio.run(p.exchange_refresh_token(client, rt, []))
    assert new_tokens.access_token != tokens.access_token
    assert new_tokens.refresh_token != tokens.refresh_token

    # Old access and refresh tokens are invalidated.
    assert asyncio.run(p.load_access_token(tokens.access_token)) is None
    assert asyncio.run(p.load_refresh_token(client, tokens.refresh_token or "")) is None
    # New tokens work.
    assert asyncio.run(p.load_access_token(new_tokens.access_token)) is not None


def test_refresh_token_belonging_to_different_client_rejected() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))

    # Forge a different client and try to load the refresh token through it.
    other_client = OAuthClientInformationFull(
        client_id="someone-else",
        client_secret=CLIENT_SECRET,
        redirect_uris=[AnyUrl("https://x/cb")],
    )
    assert asyncio.run(p.load_refresh_token(other_client, tokens.refresh_token or "")) is None


def test_revoke_token_clears_storage() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))

    access = cast(object, asyncio.run(p.load_access_token(tokens.access_token)))
    assert access is not None
    asyncio.run(p.revoke_token(access))  # type: ignore[arg-type]
    assert asyncio.run(p.load_access_token(tokens.access_token)) is None


def test_authorization_code_reuse_rejected() -> None:
    """A second exchange of the same code must fail. The fix is atomic
    pop-then-mint, not the prior post-mint pop."""
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    asyncio.run(p.exchange_authorization_code(client, auth_code))

    # Replay attempt — code is gone from storage.
    from mcp.server.auth.provider import TokenError as _TokenError

    with pytest.raises(_TokenError, match="already used"):
        asyncio.run(p.exchange_authorization_code(client, auth_code))


def test_refresh_token_reuse_rejected() -> None:
    """Concurrent /token requests with the same refresh token: only one wins."""
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))
    rt = cast(object, asyncio.run(p.load_refresh_token(client, tokens.refresh_token or "")))
    assert rt is not None

    # First refresh succeeds, mints new pair.
    asyncio.run(p.exchange_refresh_token(client, rt, []))  # type: ignore[arg-type]

    # Second refresh with the same RT must fail (atomic pop already removed it).
    from mcp.server.auth.provider import TokenError as _TokenError

    with pytest.raises(_TokenError, match="already used"):
        asyncio.run(p.exchange_refresh_token(client, rt, []))  # type: ignore[arg-type]


def test_expired_refresh_token_rejected() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))

    # Backdate the refresh-token expiry past now without sleeping.
    rt_token = tokens.refresh_token or ""
    stored = p._refresh_tokens[rt_token]
    p._refresh_tokens[rt_token] = stored.model_copy(update={"expires_at": int(time.time()) - 1})

    assert asyncio.run(p.load_refresh_token(client, rt_token)) is None


def _fill_codes(p: StaticClientProvider, n: int, expires_at: float) -> None:
    for i in range(n):
        p._auth_codes[f"code-{i}"] = AuthorizationCode(
            code=f"code-{i}",
            scopes=[],
            expires_at=expires_at,
            client_id=CLIENT_ID,
            code_challenge="x",
            redirect_uri=AnyUrl(CLAUDE_CALLBACK),
            redirect_uri_provided_explicitly=True,
            resource=None,
        )


def test_authorize_flood_evicts_oldest_code(caplog: pytest.LogCaptureFixture) -> None:
    """A drive-by flood of /authorize can't grow _auth_codes unboundedly, AND
    can't lock the real user out: at the cap the oldest code is evicted and
    the new request still succeeds."""
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    # Pre-fill to the cap with codes that won't reap (expires_at far in future).
    _fill_codes(p, MAX_OUTSTANDING_AUTH_CODES, time.time() + 10_000)
    with caplog.at_level("WARNING", logger="hermes_mcp.oauth"):
        redirect = asyncio.run(p.authorize(client, _params()))
        asyncio.run(p.authorize(client, _params()))
    new_code = redirect.split("code=")[1].split("&")[0]
    assert len(p._auth_codes) == MAX_OUTSTANDING_AUTH_CODES
    assert "code-0" not in p._auth_codes  # oldest evicted first
    assert "code-1" not in p._auth_codes  # then the next-oldest
    assert "code-2" in p._auth_codes
    assert new_code in p._auth_codes
    # The newly issued code is still redeemable.
    auth_code = asyncio.run(p.load_authorization_code(client, new_code))
    assert auth_code is not None
    assert asyncio.run(p.exchange_authorization_code(client, auth_code)).access_token
    cap_logs = [r for r in caplog.records if "cap" in r.message]
    assert len(cap_logs) == 1, "cap warning should be logged once, not per request"


def test_repr_hides_secrets() -> None:
    p = StaticClientProvider(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, bearer_token=BEARER_TOKEN
    )
    r = repr(p)
    assert CLIENT_ID in r
    assert CLIENT_SECRET not in r
    assert BEARER_TOKEN not in r
    assert "client_secret" not in r
    assert "bearer_token" not in r


def test_authorize_reaps_expired_codes_before_capping() -> None:
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    # Pre-fill with already-expired codes; the next authorize() should reap and accept.
    long_ago = time.time() - 120
    for i in range(100):
        p._auth_codes[f"old-{i}"] = AuthorizationCode(
            code=f"old-{i}",
            scopes=[],
            expires_at=long_ago,
            client_id=CLIENT_ID,
            code_challenge="x",
            redirect_uri=AnyUrl(CLAUDE_CALLBACK),
            redirect_uri_provided_explicitly=True,
            resource=None,
        )
    redirect = asyncio.run(p.authorize(client, _params()))
    assert "code=" in redirect
    # After authorize: the 100 expired entries are gone, only the new code remains.
    remaining_old = [c for c in p._auth_codes if c.startswith("old-")]
    assert remaining_old == []


def test_state_with_newline_is_sanitized_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Attacker-controlled `state` must not inject newlines into log lines."""
    p = _provider()
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    with caplog.at_level("INFO", logger="hermes_mcp.oauth"):
        asyncio.run(p.authorize(client, _params(state="evil\nFAKE LOG LINE: pwned")))
    msgs = [r.message for r in caplog.records if "issued authorization code" in r.message]
    assert msgs, "expected an issued-authorization-code log line"
    for m in msgs:
        # Newlines must be escaped before logging; the raw \n must not appear.
        assert "\n" not in m
        assert "FAKE LOG LINE" not in m or "\\n" in m


def test_mint_bearer_token_unique_and_strong() -> None:
    a, b = mint_bearer_token(), mint_bearer_token()
    assert a != b
    # token_urlsafe(32) yields >= 40 char strings
    assert len(a) >= 40


def test_load_access_token_accepts_bearer_when_configured() -> None:
    """When MCP_BEARER_TOKEN is set, presenting it as a bearer token at /mcp
    succeeds: load_access_token returns a synthetic AccessToken attributed to
    the configured client_id. Lets Codex desktop / Cursor (no OAuth UI) work."""
    p = StaticClientProvider(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, bearer_token=BEARER_TOKEN
    )
    at = asyncio.run(p.load_access_token(BEARER_TOKEN))
    assert at is not None
    assert at.client_id == CLIENT_ID
    assert at.expires_at is None  # bearer never expires; operator-rotated


def test_load_access_token_returns_cached_bearer_object() -> None:
    """Repeat presentations of the bearer return the same AccessToken instance,
    so we're not allocating per request on a hot path."""
    p = StaticClientProvider(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, bearer_token=BEARER_TOKEN
    )
    a = asyncio.run(p.load_access_token(BEARER_TOKEN))
    b = asyncio.run(p.load_access_token(BEARER_TOKEN))
    assert a is b


def test_load_access_token_rejects_wrong_bearer() -> None:
    p = StaticClientProvider(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, bearer_token=BEARER_TOKEN
    )
    assert asyncio.run(p.load_access_token("not-the-bearer-token")) is None


def test_load_access_token_no_bearer_when_unconfigured() -> None:
    """Default behavior (no MCP_BEARER_TOKEN) — no token validates against
    the bearer path; only OAuth-issued tokens work."""
    p = _provider()  # no bearer_token kwarg
    assert asyncio.run(p.load_access_token(BEARER_TOKEN)) is None
    assert asyncio.run(p.load_access_token("any-string")) is None


def test_load_access_token_oauth_path_still_works_when_bearer_configured() -> None:
    """Configuring a bearer token must not regress the OAuth-issued-token path.
    Both auth methods coexist."""
    p = StaticClientProvider(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, bearer_token=BEARER_TOKEN
    )
    client = cast(OAuthClientInformationFull, asyncio.run(p.get_client(CLIENT_ID)))
    redirect = asyncio.run(p.authorize(client, _params()))
    code = redirect.split("code=")[1].split("&")[0]
    auth_code = cast(AuthorizationCode, asyncio.run(p.load_authorization_code(client, code)))
    tokens = asyncio.run(p.exchange_authorization_code(client, auth_code))

    loaded = asyncio.run(p.load_access_token(tokens.access_token))
    assert loaded is not None
    assert loaded.client_id == CLIENT_ID
    # And the bearer still works.
    loaded_bearer = asyncio.run(p.load_access_token(BEARER_TOKEN))
    assert loaded_bearer is not None


def test_bearer_first_use_logs_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """First successful bearer-auth event surfaces at INFO. Subsequent uses
    don't spam the log."""
    p = StaticClientProvider(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, bearer_token=BEARER_TOKEN
    )
    with caplog.at_level("INFO", logger="hermes_mcp.oauth"):
        asyncio.run(p.load_access_token(BEARER_TOKEN))
        asyncio.run(p.load_access_token(BEARER_TOKEN))
        asyncio.run(p.load_access_token(BEARER_TOKEN))
    bearer_logs = [r for r in caplog.records if "static bearer token" in r.message]
    assert len(bearer_logs) == 1, "expected exactly one first-use log entry"


def test_ask_doc_is_not_lost_to_del() -> None:
    """Regression test: a prior version of hermes_client.ask put `del toolsets`
    above the docstring, which silently turned __doc__ into None."""
    from hermes_mcp.hermes_client import HermesClient as _HC

    assert _HC.ask.__doc__ is not None
    assert "session_id" in _HC.ask.__doc__

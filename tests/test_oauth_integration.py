"""End-to-end OAuth + MCP integration tests against a TestClient-driven app.

Exercises the security-critical path that unit tests can't reach:
  - /mcp rejects unauthenticated requests
  - /mcp rejects forged bearer tokens
  - the full round-trip (authorize -> token -> /mcp) succeeds only with the
    client_secret AND a valid PKCE verifier
  - /token rejects client_id-only / wrong-secret requests for every grant
  - /authorize refuses (400, no redirect) any redirect_uri not pinned

Catches regressions in FastMCP / SDK wiring (e.g., if RequireAuthMiddleware
gets unwired or our auth_server_provider path stops registering).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from hermes_mcp.config import Config
from hermes_mcp.server import build_app, build_http_app

VALID_ENV: dict[str, str] = {
    "OAUTH_CLIENT_ID": "hermes-mcp-itest",
    "OAUTH_CLIENT_SECRET": "x" * 48,
    "OAUTH_ISSUER_URL": "http://localhost:8765",
    "HERMES_API_KEY": "k" * 32,
}
BEARER_TOKEN = "b" * 48
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


def _build_client(env: dict[str, str] | None = None) -> TestClient:
    cfg = Config.from_env(env or VALID_ENV)
    hermes = MagicMock()
    hermes.ask.return_value = "alive"
    mcp = build_app(cfg, hermes)
    return TestClient(build_http_app(mcp, cfg), base_url="http://localhost:8765")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def test_mcp_rejects_unauthenticated_request() -> None:
    with _build_client() as c:
        r = c.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0.0.1"},
                },
            },
        )
    assert r.status_code == 401, r.text


def test_mcp_rejects_forged_bearer() -> None:
    with _build_client() as c:
        r = c.post(
            "/mcp",
            headers={
                "Authorization": "Bearer not-a-real-access-token",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0.0.1"},
                },
            },
        )
    assert r.status_code == 401, r.text


def _authorize(c: TestClient, challenge: str) -> str:
    r = c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": VALID_ENV["OAUTH_CLIENT_ID"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": REDIRECT_URI,
            "state": "s",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    return r.headers["location"].split("code=")[1].split("&")[0]


def _initialize(c: TestClient, access_token: str) -> int:
    return c.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0.0.1"},
            },
        },
    ).status_code


def _exchange_code(
    c: TestClient, code: str, verifier: str, client_secret: str | None
) -> tuple[int, dict[str, object]]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": VALID_ENV["OAUTH_CLIENT_ID"],
        "code_verifier": verifier,
    }
    if client_secret is not None:
        data["client_secret"] = client_secret
    r = c.post("/token", data=data)
    return r.status_code, r.json()


def _refresh(
    c: TestClient, refresh_token: str, client_secret: str | None
) -> tuple[int, dict[str, object]]:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": VALID_ENV["OAUTH_CLIENT_ID"],
    }
    if client_secret is not None:
        data["client_secret"] = client_secret
    r = c.post("/token", data=data)
    return r.status_code, r.json()


def test_full_oauth_round_trip_then_mcp_initialize_claude_style() -> None:
    """Claude's flow: client_id + client_secret pasted in the connector UI,
    sent as `client_secret` in the /token form (client_secret_post), plus
    PKCE. The server accepts it and the minted token works at /mcp."""
    verifier, challenge = _pkce_pair()
    with _build_client() as c:
        code = _authorize(c, challenge)
        status, body = _exchange_code(c, code, verifier, VALID_ENV["OAUTH_CLIENT_SECRET"])
        assert status == 200, body
        assert _initialize(c, str(body["access_token"])) == 200


def test_token_exchange_with_client_id_only_rejected() -> None:
    """The verified hole: anyone who knows the tunnel URL and the (non-secret)
    client_id could run /authorize (auto-approved) with their own PKCE pair
    and exchange the code without a client_secret. The confidential client
    must now refuse that with 401 and mint nothing — PKCE alone is not
    authentication."""
    verifier, challenge = _pkce_pair()
    with _build_client() as c:
        code = _authorize(c, challenge)
        status, body = _exchange_code(c, code, verifier, client_secret=None)
        assert status == 401, body
        assert body["error"] == "unauthorized_client"
        assert "access_token" not in body


def test_token_exchange_with_wrong_client_secret_rejected() -> None:
    verifier, challenge = _pkce_pair()
    with _build_client() as c:
        code = _authorize(c, challenge)
        status, body = _exchange_code(c, code, verifier, client_secret="wrong-" + "x" * 42)
        assert status == 401, body
        assert "access_token" not in body


def test_token_exchange_with_empty_client_secret_rejected() -> None:
    verifier, challenge = _pkce_pair()
    with _build_client() as c:
        code = _authorize(c, challenge)
        status, body = _exchange_code(c, code, verifier, client_secret="")
        assert status == 401, body


def test_token_exchange_with_secret_only_in_basic_header_rejected() -> None:
    """Only client_secret_post is supported (and advertised). A secret sent
    solely via HTTP Basic is not read, so the request is unauthenticated."""
    verifier, challenge = _pkce_pair()
    with _build_client() as c:
        code = _authorize(c, challenge)
        creds = f"{VALID_ENV['OAUTH_CLIENT_ID']}:{VALID_ENV['OAUTH_CLIENT_SECRET']}"
        r = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": VALID_ENV["OAUTH_CLIENT_ID"],
                "code_verifier": verifier,
            },
            headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()},
        )
        assert r.status_code == 401, r.text


def test_refresh_requires_client_secret() -> None:
    """The secret is enforced on the refresh_token grant too, so a leaked
    refresh token alone can't be used to mint new access tokens."""
    verifier, challenge = _pkce_pair()
    with _build_client() as c:
        code = _authorize(c, challenge)
        status, body = _exchange_code(c, code, verifier, VALID_ENV["OAUTH_CLIENT_SECRET"])
        assert status == 200, body
        refresh_token = str(body["refresh_token"])

        status, body = _refresh(c, refresh_token, client_secret=None)
        assert status == 401, body
        assert "access_token" not in body

        status, body = _refresh(c, refresh_token, client_secret="wrong-" + "x" * 42)
        assert status == 401, body

        # The rejected attempts didn't consume the refresh token; the real
        # client can still rotate it.
        status, body = _refresh(c, refresh_token, VALID_ENV["OAUTH_CLIENT_SECRET"])
        assert status == 200, body
        assert _initialize(c, str(body["access_token"])) == 200


def test_authorize_rejects_unpinned_redirect_uri_without_redirecting() -> None:
    """/authorize must not be an open redirector: an unpinned redirect_uri
    gets a direct 400 and no Location header, so no code (and no browser)
    is ever sent to it."""
    _verifier, challenge = _pkce_pair()
    with _build_client() as c:
        for evil in (
            "https://attacker.example/cb",
            "https://claude.ai.attacker.example/api/mcp/auth_callback",
            "https://claude.ai/api/mcp/auth_callback/x",
            "javascript:alert(1)",
        ):
            r = c.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": VALID_ENV["OAUTH_CLIENT_ID"],
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "redirect_uri": evil,
                    "state": "s",
                },
                follow_redirects=False,
            )
            assert r.status_code == 400, (evil, r.status_code, r.text)
            assert "location" not in r.headers, evil
            assert "code=" not in r.text


def test_authorize_accepts_configured_redirect_uri() -> None:
    """OAUTH_ALLOWED_REDIRECT_URIS replaces the default list end-to-end."""
    _verifier, challenge = _pkce_pair()
    env = {**VALID_ENV, "OAUTH_ALLOWED_REDIRECT_URIS": "https://app.example.com/cb"}
    with _build_client(env) as c:
        params = {
            "response_type": "code",
            "client_id": VALID_ENV["OAUTH_CLIENT_ID"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
        }
        r = c.get(
            "/authorize",
            params={**params, "redirect_uri": "https://app.example.com/cb"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text
        assert r.headers["location"].startswith("https://app.example.com/cb?")
        r = c.get(
            "/authorize",
            params={**params, "redirect_uri": REDIRECT_URI},
            follow_redirects=False,
        )
        assert r.status_code == 400


def test_metadata_advertises_only_client_secret_post() -> None:
    with _build_client() as c:
        r = c.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        meta = r.json()
        assert meta["token_endpoint_auth_methods_supported"] == ["client_secret_post"]
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert meta["token_endpoint"] == "http://localhost:8765/token"
        assert "registration_endpoint" not in meta


def test_token_endpoint_rejects_wrong_pkce_verifier() -> None:
    _verifier, challenge = _pkce_pair()
    with _build_client() as c:
        r = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": VALID_ENV["OAUTH_CLIENT_ID"],
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
                "state": "s",
            },
            follow_redirects=False,
        )
        code = r.headers["location"].split("code=")[1].split("&")[0]

        r = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": VALID_ENV["OAUTH_CLIENT_ID"],
                "client_secret": VALID_ENV["OAUTH_CLIENT_SECRET"],
                "code_verifier": "wrong-verifier-doesnt-match-challenge",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


def test_mcp_accepts_static_bearer_token_when_configured() -> None:
    """When MCP_BEARER_TOKEN is set, Codex desktop / Cursor style clients —
    which send `Authorization: Bearer <fixed-token>` with no prior /token
    exchange — can /mcp directly. This is the headline bearer-auth contract."""
    with _build_client({**VALID_ENV, "MCP_BEARER_TOKEN": BEARER_TOKEN}) as c:
        assert _initialize(c, BEARER_TOKEN) == 200


def test_mcp_rejects_wrong_bearer_when_configured() -> None:
    """A wrong bearer must not succeed even when bearer auth is enabled —
    it falls through to the OAuth path's normal 401."""
    with _build_client({**VALID_ENV, "MCP_BEARER_TOKEN": BEARER_TOKEN}) as c:
        assert _initialize(c, "wrong-bearer-token") == 401


def test_mcp_rejects_bearer_when_unconfigured() -> None:
    """Default deployment (no MCP_BEARER_TOKEN) — even a guess at a bearer
    string returns 401. Bearer auth is opt-in."""
    with _build_client() as c:  # default env, no bearer
        assert _initialize(c, BEARER_TOKEN) == 401


def test_oauth_flow_still_works_when_bearer_configured() -> None:
    """Configuring a bearer token must not regress the OAuth path. Both
    auth methods coexist on the same server instance."""
    verifier, challenge = _pkce_pair()
    with _build_client({**VALID_ENV, "MCP_BEARER_TOKEN": BEARER_TOKEN}) as c:
        code = _authorize(c, challenge)
        status, body = _exchange_code(c, code, verifier, VALID_ENV["OAUTH_CLIENT_SECRET"])
        assert status == 200, body
        assert _initialize(c, str(body["access_token"])) == 200


def test_bearer_token_is_not_accepted_as_oauth_client_secret() -> None:
    """MCP_BEARER_TOKEN and OAUTH_CLIENT_SECRET are separate credentials."""
    verifier, challenge = _pkce_pair()
    with _build_client({**VALID_ENV, "MCP_BEARER_TOKEN": BEARER_TOKEN}) as c:
        code = _authorize(c, challenge)
        status, _body = _exchange_code(c, code, verifier, client_secret=BEARER_TOKEN)
        assert status == 401


def test_register_endpoint_not_mounted_when_dcr_disabled() -> None:
    """We pass ClientRegistrationOptions(enabled=False); /register must 404."""
    with _build_client() as c:
        r = c.post(
            "/register",
            json={
                "redirect_uris": ["https://attacker.example.com/cb"],
                "client_name": "rogue",
            },
        )
        assert r.status_code == 404

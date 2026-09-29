"""Single-user OAuth 2.1 authorization server provider.

The MCP transport spec (and Claude Desktop / Claude.ai's Custom Connector
UI) requires an OAuth 2.1 authorization server in front of the MCP
endpoint. For a personal bridge there is exactly one client and exactly one
user, so this provider:

  - holds a single static `client_id` / `client_secret`, configured via env
  - registers the client as a **confidential client** with
    `token_endpoint_auth_method="client_secret_post"`. The SDK's
    `ClientAuthenticator` rejects any /token request (authorization-code
    AND refresh-token grants) whose `client_secret` form field is missing
    or does not match (constant-time compare). PKCE-S256 is also mandatory.
  - pins redirect URIs to an exact allowlist (default: Claude's connector
    callbacks), so `/authorize` is not an open redirector and a code can
    only ever be delivered to a configured client
  - auto-approves the /authorize step
  - mints opaque random access and refresh tokens, stored in memory
  - has no persistence: tokens evaporate on restart, the client just re-auths

Why confidential and not PKCE-only (as the unreleased public-client change
after v0.4.0 briefly was): PKCE binds a code to the client that started the
flow, but it does not *authenticate* that client. With an auto-approving
/authorize, a PKCE-only public client lets anyone who knows the tunnel URL
and the (non-secret) `client_id` run the flow with their own verifier and
mint a working token with curl. The 256-bit `client_secret` has to be the
gate. MCP clients that only support public PKCE clients (Codex, Cursor) use
the static bearer-token path (`MCP_BEARER_TOKEN`) instead.

Dynamic Client Registration is intentionally disabled. Anyone hitting
/register is told it is unsupported.

Concurrency: hermes-mcp is single-process and the dict mutations below are
guarded by Python's GIL on the basic operations we use (dict set/get/pop).
We do not need an explicit lock for single-user traffic.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Final

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import (
    InvalidRedirectUriError,
    OAuthClientInformationFull,
    OAuthToken,
)
from pydantic import AnyUrl, PrivateAttr

logger = logging.getLogger(__name__)

DEFAULT_ACCESS_TOKEN_TTL = 3600  # 1 hour
DEFAULT_REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
AUTHORIZATION_CODE_TTL = 60  # 1 minute (RFC 6749 §4.1.2 recommends short)

# Caps to prevent unbounded growth from drive-by /authorize calls.
MAX_OUTSTANDING_AUTH_CODES = 1024
MAX_OUTSTANDING_ACCESS_TOKENS = 4096

# The only /token client-authentication method the static client accepts.
# `server.build_http_app` advertises exactly this in the authorization-server
# metadata so compliant clients don't pick `client_secret_basic`.
TOKEN_ENDPOINT_AUTH_METHOD: Final = "client_secret_post"  # noqa: S105 — RFC 7591 method name, not a secret

# Exact redirect URIs `/authorize` will send a code to. Pinning them (rather
# than allowing any https URL) means /authorize can't be used as an open
# redirector, and a code can only ever be delivered to Claude. Operators add
# URIs for other clients via OAUTH_ALLOWED_REDIRECT_URIS. Re-exported from
# config.py so the env default and the provider default can't drift.
DEFAULT_ALLOWED_REDIRECT_URIS: frozenset[str] = frozenset(
    {
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    }
)


def normalize_redirect_uri(uri: str) -> str:
    """Canonical string form used for exact-match comparison.

    Pydantic's `AnyUrl` normalizes (lower-cases the host, adds a trailing
    `/` to an empty path, ...). The SDK hands us the requested redirect_uri
    as an `AnyUrl`, so configured entries are run through the same
    normalization before comparing.
    """
    return str(AnyUrl(uri))


def _check_redirect_uri(redirect_uri: AnyUrl, allowed: frozenset[str]) -> None:
    """Accept only an exact, pre-configured redirect URI."""
    if str(redirect_uri) not in allowed:
        raise InvalidRedirectUriError("redirect_uri is not allowed for this client")


def _safe_state(state: str | None) -> str:
    """Sanitize the OAuth `state` value before logging. The client controls
    state — without sanitization, an attacker hitting `/authorize` could
    inject log lines via newlines.
    """
    if not state:
        return "(none)"
    return state.replace("\n", "\\n").replace("\r", "\\r")[:64]


class _StaticClient(OAuthClientInformationFull):
    """Single static client whose redirect URIs are an exact allowlist
    (`_allowed_redirect_uris`, a Pydantic `PrivateAttr` so it never appears
    in the serialized model).

    A request whose redirect_uri is not on the list is rejected by the SDK's
    /authorize handler with a direct 400 — it never redirects to it.
    """

    _allowed_redirect_uris: frozenset[str] = PrivateAttr(default_factory=frozenset)

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            raise InvalidRedirectUriError("redirect_uri is required")
        _check_redirect_uri(redirect_uri, self._allowed_redirect_uris)
        return redirect_uri


@dataclass
class StaticClientProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """In-memory OAuth provider for a single pre-shared confidential client.

    Optionally also accepts a static `bearer_token` as an alternative auth
    method, for MCP clients (Codex, Cursor's `headers` block) that only
    support public OAuth clients or have no OAuth flow at all. Both auth
    paths coexist: each /mcp request is checked against (a) OAuth-issued
    access tokens, then (b) the configured bearer token. Constant-time
    comparison via `hmac.compare_digest`.

    `client_secret` and `bearer_token` are excluded from `repr()` so a stray
    `logger.debug(provider)` cannot leak them.
    """

    client_id: str
    client_secret: str = field(repr=False)
    bearer_token: str | None = field(default=None, repr=False)
    allowed_redirect_uris: frozenset[str] = DEFAULT_ALLOWED_REDIRECT_URIS
    access_token_ttl: int = DEFAULT_ACCESS_TOKEN_TTL
    refresh_token_ttl: int = DEFAULT_REFRESH_TOKEN_TTL

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("client_id and client_secret are required")
        allowed = frozenset(normalize_redirect_uri(u) for u in self.allowed_redirect_uris)
        if not allowed:
            raise ValueError("at least one allowed redirect URI is required")
        self.allowed_redirect_uris = allowed
        # Confidential client: the SDK's ClientAuthenticator
        # (`mcp/server/auth/middleware/client_auth.py`) enforces the secret on
        # every /token request, on top of mandatory PKCE.
        self._client = _StaticClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uris=[AnyUrl(u) for u in sorted(allowed)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method=TOKEN_ENDPOINT_AUTH_METHOD,
        )
        # PrivateAttr can't be set via constructor in Pydantic v2; assign here.
        self._client._allowed_redirect_uris = allowed
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._refresh_to_access: dict[str, str] = {}
        # Synthetic AccessToken returned on bearer-token auth. Lazily built
        # the first time the bearer is presented and cached so we're not
        # re-allocating per request. No expiry — bearer tokens are
        # operator-rotated, not time-rotated.
        self._bearer_access_token: AccessToken | None = None
        # One-time audit log marker so the first bearer-auth event surfaces
        # at INFO without spamming every subsequent request.
        self._bearer_logged: bool = False
        # One-time marker for the outstanding-code-cap warning.
        self._cap_warned: bool = False

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if hmac.compare_digest(client_id.encode(), self.client_id.encode()):
            return self._client
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError(
            "Dynamic client registration is disabled. Configure OAUTH_CLIENT_ID "
            "and OAUTH_CLIENT_SECRET on the server and paste both into your MCP "
            "client's connector config (clients without client_secret support "
            "should use MCP_BEARER_TOKEN instead)."
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # Reap expired codes opportunistically so `/authorize` is the only
        # write path that grows the dict.
        self._reap_expired_codes()
        while len(self._auth_codes) >= MAX_OUTSTANDING_AUTH_CODES:
            # Evict the oldest instead of refusing: anyone who knows the
            # client_id can call /authorize, and refusing would let a flood
            # lock the real user out. A legitimate code is exchanged within
            # about a second, so it's always among the newest. Dicts preserve
            # insertion order, so the first key is the oldest.
            oldest = next(iter(self._auth_codes))
            del self._auth_codes[oldest]
            if not self._cap_warned:
                logger.warning(
                    "oauth: outstanding-code cap (%d) reached; evicting oldest codes",
                    MAX_OUTSTANDING_AUTH_CODES,
                )
                self._cap_warned = True

        # Auto-approve: mint a code and immediately redirect back to the client.
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + AUTHORIZATION_CODE_TTL,
            client_id=str(client.client_id),
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        logger.info("oauth: issued authorization code (state=%s)", _safe_state(params.state))
        return construct_redirect_uri(
            str(params.redirect_uri),
            code=code,
            state=params.state,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Codes are single-use: pop *atomically* before minting so concurrent
        # exchanges of the same code can't both succeed.
        if self._auth_codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code was already used")
        return self._mint_token_pair(client, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        rt = self._refresh_tokens.get(refresh_token)
        if rt is None or rt.client_id != str(client.client_id):
            return None
        if rt.expires_at and rt.expires_at < int(time.time()):
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Atomic pop *before* minting so concurrent /token exchanges of the
        # same refresh token can't both produce valid pairs. RFC 6819 §5.2.2.3
        # recommends rotation; this also approximates reuse detection
        # (second arrival sees the token gone and is rejected as invalid_grant).
        if self._refresh_tokens.pop(refresh_token.token, None) is None:
            raise TokenError("invalid_grant", "refresh token was already used")
        old_access = self._refresh_to_access.pop(refresh_token.token, None)
        if old_access:
            self._access_tokens.pop(old_access, None)
        return self._mint_token_pair(client, scopes or refresh_token.scopes, None)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Static bearer-token path: for clients with no OAuth UI. Compared
        # in constant time. We check OAuth-issued tokens FIRST so that the
        # bearer comparison only runs on a miss — minor optimization, but
        # also keeps the OAuth path's behavior identical when no bearer is
        # configured.
        at = self._access_tokens.get(token)
        if at is not None:
            if at.expires_at and at.expires_at < int(time.time()):
                self._access_tokens.pop(token, None)
                return None
            return at
        if self.bearer_token and hmac.compare_digest(token.encode(), self.bearer_token.encode()):
            if not self._bearer_logged:
                logger.info("oauth: static bearer token accepted (first use this process)")
                self._bearer_logged = True
            if self._bearer_access_token is None:
                self._bearer_access_token = AccessToken(
                    token=token,
                    client_id=self.client_id,
                    scopes=[],
                    expires_at=None,
                    resource=None,
                )
            return self._bearer_access_token
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Revocation endpoint is not exposed; FastMCP only calls this if
        # RevocationOptions().enabled. Provided for protocol completeness.
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        else:
            self._refresh_tokens.pop(token.token, None)
            old_access = self._refresh_to_access.pop(token.token, None)
            if old_access:
                self._access_tokens.pop(old_access, None)

    def _reap_expired_codes(self) -> None:
        now = time.time()
        expired = [c for c, ac in self._auth_codes.items() if ac.expires_at < now]
        for c in expired:
            self._auth_codes.pop(c, None)

    def _mint_token_pair(
        self,
        client: OAuthClientInformationFull,
        scopes: list[str],
        resource: str | None,
    ) -> OAuthToken:
        # Cap outstanding tokens. Under normal operation every token here is
        # live (MCP clients refresh well before expiry); hitting this means
        # something is wrong (a runaway client) and refusing is the right call.
        if len(self._access_tokens) >= MAX_OUTSTANDING_ACCESS_TOKENS:
            self._reap_expired_access_tokens()
            if len(self._access_tokens) >= MAX_OUTSTANDING_ACCESS_TOKENS:
                # No perfect TokenErrorCode for "we're out of capacity"; the
                # closest is invalid_request — RFC 6749 doesn't model rate-limit
                # in the token-error set.
                raise TokenError("invalid_request", "Too many outstanding access tokens")

        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=str(client.client_id),
            scopes=scopes,
            expires_at=now + self.access_token_ttl,
            resource=resource,
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=str(client.client_id),
            scopes=scopes,
            expires_at=now + self.refresh_token_ttl,
        )
        self._refresh_to_access[refresh] = access
        logger.info("oauth: minted token pair (expires_in=%ds)", self.access_token_ttl)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106 — OAuth token-type literal, not a secret
            expires_in=self.access_token_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    def _reap_expired_access_tokens(self) -> None:
        now = int(time.time())
        expired = [
            t for t, at in self._access_tokens.items() if at.expires_at and at.expires_at < now
        ]
        for t in expired:
            self._access_tokens.pop(t, None)


def mint_client_credentials() -> tuple[str, str]:
    """Generate a fresh client_id / client_secret pair for static registration."""
    return (
        f"hermes-mcp-{secrets.token_urlsafe(8)}",
        secrets.token_urlsafe(32),
    )


def mint_bearer_token() -> str:
    """Generate a fresh static bearer token (256 bits of entropy) for MCP
    clients whose UI has no OAuth flow (Codex desktop, Cursor headers)."""
    return secrets.token_urlsafe(32)

"""Single-user OAuth 2.1 authorization server provider.

The MCP transport spec (and most MCP client UIs that add a remote server,
including Claude Desktop's Custom Connector, Codex CLI, and Cursor) require
an OAuth 2.1 authorization server in front of the MCP endpoint. For a
personal bridge there is exactly one client and exactly one user, so this
provider:

  - holds a single static `client_id`, configured via env
  - registers the client as a **public client** with
    `token_endpoint_auth_method="none"` (no `client_secret`). PKCE is
    mandatory; the SDK enforces it on every authorization-code exchange
    (`mcp/server/auth/handlers/token.py:174-185`). Without PKCE the request
    is rejected with `invalid_grant`, so the dynamic per-exchange
    `code_verifier` is what actually protects token issuance.
  - auto-approves the /authorize step
  - mints opaque random access and refresh tokens, stored in memory and
    persisted to disk so they survive process restarts
  - reloads non-expired tokens from disk on startup

`OAUTH_CLIENT_SECRET` is still required at startup for backward
compatibility (Claude Desktop / Claude.ai have it pasted in their connector
UI and will keep sending it). The server simply ignores the value at the
/token exchange — PKCE is the real gate. Codex CLI and Cursor, which only
support PKCE-style public clients (`McpServerOAuthConfig` has no
`client_secret` field), now work without changes.

Dynamic Client Registration is intentionally disabled. Anyone hitting
/register is told it is unsupported.

Concurrency: hermes-mcp is single-process and the dict mutations below are
guarded by Python's GIL on the basic operations we use (dict set/get/pop).
We do not need an explicit lock for single-user traffic. Disk writes are
low-frequency (one per token mint/rotation); the real reason no lock is
needed is the single-user, single-process deployment model — not GIL
atomicity across the multi-step serialize-then-replace sequence.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
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

# Redirect-URI schemes the bridge ALWAYS accepts as a security baseline.
# `https` is the standard for hosted MCP clients; `http` is only honored
# for localhost (enforced below) so testing and the doctor flow work.
# These are the schemes that cannot turn `/authorize` into an open
# redirector to dangerous targets.
_BASELINE_SCHEMES: frozenset[str] = frozenset({"https", "http"})

# Default custom URI schemes the bridge accepts on top of the baseline. Each
# entry corresponds to an MCP client's OAuth redirect-URI scheme. Operators
# extend or override this via `OAUTH_ALLOWED_REDIRECT_SCHEMES` for additional
# clients (e.g. `vscode` for Continue). Re-exported from `config.py` so the
# server default and the env-var default cannot drift apart.
DEFAULT_ALLOWED_REDIRECT_SCHEMES: frozenset[str] = frozenset({"claude", "claudeai", "cursor"})

# Default path for the token store. Re-exported so config.py can reference it
# without duplicating the default.
DEFAULT_TOKEN_STORE_PATH: Path = Path.home() / ".config" / "hermes-mcp" / "tokens.json"


def _check_redirect_uri(redirect_uri: AnyUrl, allowed_schemes: frozenset[str]) -> None:
    """Reject schemes/hosts that would turn the OAuth flow into an open
    redirector to dangerous targets.

    `allowed_schemes` is the union of the baseline (`https`, `http`-on-
    localhost) and any custom URI schemes the operator configured via
    `OAUTH_ALLOWED_REDIRECT_SCHEMES`. Each MCP client uses its own custom
    scheme (`claude`, `claudeai`, `cursor`, `vscode`, ...); the operator
    adds to the configured list as needed for new clients.

    Permissive within sane bounds — we do not pin specific callback URIs
    because they are subject to change without notice across client
    versions. PKCE protects the actual token exchange (mandatory
    code_verifier check at /token); this scheme check just prevents
    `javascript:` / `data:` / `file:` style open-redirector abuse.
    """
    scheme = (redirect_uri.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise InvalidRedirectUriError(f"redirect_uri scheme {scheme!r} is not allowed")
    if scheme == "http" and redirect_uri.host not in ("localhost", "127.0.0.1", "::1"):
        raise InvalidRedirectUriError("redirect_uri scheme 'http' is only allowed for localhost")


def _safe_state(state: str | None) -> str:
    """Sanitize the OAuth `state` value before logging. The client controls
    state — without sanitization, an attacker hitting `/authorize` could
    inject log lines via newlines.
    """
    if not state:
        return "(none)"
    return state.replace("\n", "\\n").replace("\r", "\\r")[:64]


class _StaticClient(OAuthClientInformationFull):
    """Single static client. Accepts any redirect_uri sent by the client,
    subject to a scheme allowlist enforced by `_check_redirect_uri`.

    Validating the redirect_uri against a *pre-registered list* is not
    useful here: PKCE binds the authorization code to the original
    code_challenge, so an attacker who substitutes a redirect_uri cannot
    exchange the code without the matching code_verifier (which never
    leaves the legitimate client).

    Validating the redirect_uri's *scheme* is, however, useful: without it
    `/authorize` would happily redirect to `javascript:` or `data:` URIs
    on request, turning the endpoint into an open redirector.

    `_allowed_redirect_schemes` is the union of the baseline (`https`,
    `http`-on-localhost) and the operator-configured custom schemes
    (defaulting to `claude`, `claudeai`, `cursor`). Operators extend the
    list via `OAUTH_ALLOWED_REDIRECT_SCHEMES` for new MCP clients.
    Stored as a Pydantic `PrivateAttr` so it does not appear in the
    serialized model and isn't passed across the wire.
    """

    _allowed_redirect_schemes: frozenset[str] = PrivateAttr(default_factory=frozenset)

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            raise InvalidRedirectUriError("redirect_uri is required")
        _check_redirect_uri(redirect_uri, self._allowed_redirect_schemes)
        return redirect_uri


class TokenStore:
    """Handles persistence of OAuth tokens to/from disk.

    Tokens are written atomically (temp file + os.replace) to prevent
    corrupt state on crash mid-write. The file is created with mode 0o600
    so only the owning user can read it.

    Auth codes are intentionally excluded: their 60-second TTL makes
    persistence useless (they'd always be expired on reload).
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def save(
        self,
        access_tokens: dict[str, AccessToken],
        refresh_tokens: dict[str, RefreshToken],
        refresh_to_access: dict[str, str],
    ) -> None:
        """Serialize current token state to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self._path.parent, 0o700)
            except OSError:
                pass
            data = {
                "access_tokens": {
                    k: v.model_dump() for k, v in access_tokens.items()
                },
                "refresh_tokens": {
                    k: v.model_dump() for k, v in refresh_tokens.items()
                },
                "refresh_to_access": dict(refresh_to_access),
            }
            # Atomic write: temp file in same directory, then os.replace.
            fd, tmp_path = tempfile.mkstemp(
                dir=self._path.parent, prefix=".tokens.", suffix=".tmp"
            )
            try:
                os.chmod(tmp_path, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._path)
                # Ensure final file also has mode 0o600 (os.replace preserves
                # destination permissions on some platforms; set explicitly).
                os.chmod(self._path, 0o600)
                # Fsync the parent directory so the rename is durable on-disk.
                parent_fd = os.open(str(self._path.parent), os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                except OSError:
                    pass
                finally:
                    os.close(parent_fd)
            except Exception:
                # Clean up temp file on failure.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning("token-store: failed to save tokens: %s", exc)

    def load(
        self,
    ) -> tuple[dict[str, AccessToken], dict[str, RefreshToken], dict[str, str]]:
        """Load and return non-expired tokens from disk.

        Returns three dicts: access_tokens, refresh_tokens, refresh_to_access.
        Any token whose `expires_at` is in the past is silently dropped.
        Returns empty dicts if the file does not exist or cannot be parsed.
        """
        if not self._path.exists():
            return {}, {}, {}
        try:
            with self._path.open() as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("token-store: failed to read tokens file: %s", exc)
            return {}, {}, {}

        if not isinstance(data, dict):
            logger.warning("token-store: unexpected shape in tokens file (got %s), discarding", type(data).__name__)
            return {}, {}, {}

        now = int(time.time())
        access_tokens: dict[str, AccessToken] = {}
        refresh_tokens: dict[str, RefreshToken] = {}
        raw_r2a = data.get("refresh_to_access")
        refresh_to_access: dict[str, str] = dict(raw_r2a) if isinstance(raw_r2a, dict) else {}

        raw_at = data.get("access_tokens")
        for key, val in (raw_at.items() if isinstance(raw_at, dict) else []):
            try:
                at = AccessToken.model_validate(val)
                if at.expires_at is None or at.expires_at > now:
                    access_tokens[key] = at
            except Exception as exc:
                logger.debug("token-store: skipping invalid access token entry: %s", exc)

        raw_rt = data.get("refresh_tokens")
        for key, val in (raw_rt.items() if isinstance(raw_rt, dict) else []):
            try:
                rt = RefreshToken.model_validate(val)
                if rt.expires_at is None or rt.expires_at > now:
                    refresh_tokens[key] = rt
                else:
                    # Also drop the corresponding refresh_to_access mapping.
                    refresh_to_access.pop(key, None)
            except Exception as exc:
                logger.debug("token-store: skipping invalid refresh token entry: %s", exc)

        # Remove refresh_to_access entries where either the refresh token or the
        # mapped access token did not survive expiry filtering.
        refresh_to_access = {
            k: v
            for k, v in refresh_to_access.items()
            if k in refresh_tokens and v in access_tokens
        }

        loaded = len(access_tokens) + len(refresh_tokens)
        if loaded:
            logger.info("token-store: loaded %d token(s) from %s", loaded, self._path)
        return access_tokens, refresh_tokens, refresh_to_access


@dataclass
class StaticClientProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """In-memory + disk-persisted OAuth provider for a single pre-shared client.

    Tokens are persisted to disk (token_store_path) and reloaded on startup
    so that restarts do not require the MCP client to re-authenticate.

    Optionally also accepts a static `bearer_token` as an alternative auth
    method, for MCP clients (Codex desktop's custom-MCP form, Cursor's
    `headers` block) whose UI has no OAuth flow at all. Both auth paths
    coexist: each /mcp request is checked against (a) OAuth-issued access
    tokens, then (b) the configured bearer token. Constant-time comparison
    via `hmac.compare_digest`.
    """

    client_id: str
    client_secret: str
    bearer_token: str | None = None
    allowed_redirect_schemes: frozenset[str] = DEFAULT_ALLOWED_REDIRECT_SCHEMES
    access_token_ttl: int = DEFAULT_ACCESS_TOKEN_TTL
    refresh_token_ttl: int = DEFAULT_REFRESH_TOKEN_TTL
    token_store_path: Path = field(default_factory=lambda: DEFAULT_TOKEN_STORE_PATH)

    def __post_init__(self) -> None:
        # `self.client_secret` is kept required at construction time for
        # backward-compat with deployments that already have OAUTH_CLIENT_SECRET
        # set, and because Claude Desktop's UI still requires a value to paste.
        # It is NOT used for auth — see the module docstring; PKCE is the gate.
        if not self.client_id or not self.client_secret:
            raise ValueError("client_id and client_secret are required")
        # Baseline (`https`, `http`-on-localhost) is always allowed alongside
        # the operator-configured custom schemes — see _check_redirect_uri.
        effective_schemes = _BASELINE_SCHEMES | frozenset(
            s.lower() for s in self.allowed_redirect_schemes
        )
        # Public client (PKCE-only). `client_secret=None` + auth method `"none"`
        # makes the SDK skip the secret check at /token (`client_auth.py:93-104`),
        # while PKCE remains mandatory (`token.py:26, 174-185`). This is what
        # lets Codex CLI / Cursor — which only ship `client_id` in their MCP
        # config — complete the OAuth flow. Claude Desktop still pastes a
        # client_secret in its UI; the server reads it but doesn't enforce it.
        self._client = _StaticClient(
            client_id=self.client_id,
            client_secret=None,
            redirect_uris=[AnyUrl("http://localhost/")],  # placeholder; we override validation
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # noqa: S106 — RFC 7591 method name, not a secret
        )
        # PrivateAttr can't be set via constructor in Pydantic v2; assign here.
        self._client._allowed_redirect_schemes = effective_schemes
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

        # Load persisted tokens from disk.
        self._token_store = TokenStore(Path(self.token_store_path))
        at, rt, r2a = self._token_store.load()
        self._access_tokens.update(at)
        self._refresh_tokens.update(rt)
        self._refresh_to_access.update(r2a)

    def _save_tokens(self) -> None:
        """Persist current token state to disk."""
        self._token_store.save(
            self._access_tokens,
            self._refresh_tokens,
            self._refresh_to_access,
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if hmac.compare_digest(client_id.encode(), self.client_id.encode()):
            return self._client
        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError(
            "Dynamic client registration is disabled. Configure OAUTH_CLIENT_ID "
            "on the server and paste it into your MCP client's OAuth config. "
            "Claude Desktop also requires OAUTH_CLIENT_SECRET in its connector UI "
            "(the server accepts but does not enforce it; PKCE is the real gate)."
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # Reap expired codes opportunistically so `/authorize` is the only
        # write path that grows the dict.
        self._reap_expired_codes()
        if len(self._auth_codes) >= MAX_OUTSTANDING_AUTH_CODES:
            logger.warning(
                "oauth: refusing /authorize — outstanding-code cap reached (%d)",
                MAX_OUTSTANDING_AUTH_CODES,
            )
            raise AuthorizeError("server_error", "Too many outstanding authorization codes")

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
        result = self._mint_token_pair(client, scopes or refresh_token.scopes, None)
        # _mint_token_pair already saves; nothing extra needed here.
        # Limitation: if the save inside _mint_token_pair fails, a later restart
        # will reload the pre-rotation state from disk (old tokens gone in memory,
        # new tokens not on disk). The client will need to re-authenticate.
        # Accepted for the single-user, best-effort-persistence threat model.
        return result

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
        # Persist so revoked tokens don't reappear after a restart.
        self._save_tokens()

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
        # Persist after every mint so tokens survive an immediate restart.
        self._save_tokens()
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
        # Persist after reaping so the disk copy doesn't grow stale entries.
        if expired:
            self._save_tokens()


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

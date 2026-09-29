# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
uv venv .venv --python 3.11 && source .venv/bin/activate && uv pip install -e ".[dev]"

# Full CI suite (must all pass)
ruff check . && ruff format --check . && mypy src/ && pytest

# Individual checks
ruff check .                # lint
ruff format .               # auto-format
mypy src/                   # type-check (strict mode, src/ only — mcp module excluded)
pytest                      # all tests
pytest tests/test_oauth.py  # single test file
pytest -k "test_name"       # single test by name

# Run / inspect the server
hermes-mcp serve            # or: python -m hermes_mcp serve
hermes-mcp doctor           # startup self-check (probes the gateway)
hermes-mcp mint-client      # generate a fresh OAuth client_id / client_secret
```

## Architecture

**hermes-mcp** is an MCP bridge that lets MCP clients (today: Claude Desktop / Claude.ai; future: Codex CLI / Cursor / Continue once DCR ships) delegate tasks to a locally running **Hermes Agent**. The client calls MCP tools (`hermes_ask`, `hermes_check`, `hermes_cancel`, `hermes_reset`) over an HTTPS tunnel; the bridge gates that with OAuth 2.1 and forwards each call to the Hermes gateway's OpenAI-compatible HTTP API.

**Static-client-only constraint.** The OAuth provider currently disables Dynamic Client Registration (`ClientRegistrationOptions(enabled=False)` in `build_app`; `StaticClientProvider.register_client` raises `NotImplementedError`). This means a client can only use the OAuth path if it supports pasting in a static pre-shared `client_id` **and** `client_secret` (sent as `client_secret_post`). Claude Desktop's Custom Connector UI does. Codex and Cursor only support public (PKCE-only) clients, so they use the static bearer-token path (`MCP_BEARER_TOKEN`) instead. **Do not make the static client public (`client_secret=None` / `token_endpoint_auth_method="none"`)**: `/authorize` auto-approves, so that lets anyone with the tunnel URL and the non-secret `client_id` mint tokens — an unreleased build did exactly this and it was reverted.

```
MCP client (Claude Desktop / Claude.ai / Codex CLI / Cursor / ...)
  │  HTTPS via cloudflared tunnel
  ▼
hermes-mcp (this project, listening on 127.0.0.1:8765)
  ├─ OAuth 2.1 (authorization code + PKCE), single static confidential client_id/secret
  ├─ or static MCP_BEARER_TOKEN (for Codex / Cursor)
  └─ HTTP POST to the gateway
     │
     ▼
hermes-gateway (127.0.0.1:8642, OpenAI-compatible /v1/chat/completions)
  └─ same AIAgent loop that drives Telegram (skills, tools, sessions)
```

The gateway is a **separate, long-running process** owned by the user (typically a `systemd --user` service). hermes-mcp does not spawn it; it just sends HTTP requests.

The six source modules in `src/hermes_mcp/` have clean single responsibilities:

- **`config.py`** — frozen `Config` dataclass parsed from env vars. Required: `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_ISSUER_URL`, `HERMES_API_KEY`. Validates the issuer URL is HTTPS (or `http://localhost`), the client_secret is ≥32 chars, each `OAUTH_ALLOWED_REDIRECT_URIS` entry is `https://` or loopback `http://`, and warns if `BIND_HOST` is non-loopback or the deprecated `OAUTH_ALLOWED_REDIRECT_SCHEMES` is set. Secret fields are `field(repr=False)`.
- **`oauth.py`** — `StaticClientProvider` implements the MCP SDK's `OAuthAuthorizationServerProvider` protocol with one pre-shared **confidential** client (`client_secret_post`; the SDK's `ClientAuthenticator` enforces the secret on every `/token` grant). Mints opaque 256-bit access tokens (1h TTL) and refresh tokens (30d, rotated atomically on use). PKCE-S256 enforced by the SDK. DCR is disabled. `_StaticClient.validate_redirect_uri` accepts only an exact match against `OAUTH_ALLOWED_REDIRECT_URIS` (default Claude's two callbacks), so `/authorize` is not an open redirector. At the outstanding-auth-code cap the oldest code is evicted (never refuse — a flood must not lock out the real user). Also accepts the optional static `MCP_BEARER_TOKEN` in `load_access_token`.
- **`hermes_client.py`** — `HermesClient.ask()` does `httpx.post` to the gateway's `/v1/chat/completions` with `Authorization: Bearer $HERMES_API_KEY`. `session_id` is forwarded as `X-Hermes-Session-Id`. `toolsets` is accepted for backward-compat but ignored — toolset selection now lives in the Hermes config (`platform_toolsets.api_server`). Gateway error bodies are NOT echoed in user-visible errors (DEBUG only).
- **`jobs.py`** — `JobStore` is a thread-safe in-memory dict of `Job` records, used by `hermes_ask(..., async_mode=True)`, `hermes_check`, `hermes_cancel`, and `hermes_reset`. Lazy TTL reap (24h) on every access, 1000-job cap. In-memory only by design; restart drops everything. `mark_completed`/`mark_failed` are terminal-state-aware so a late-finishing worker cannot overwrite a cancellation. `reset_all()` reaps first, then wipes the store and returns `(cleared, by_status)`. Times use `time.time()` (wall clock, epoch seconds) so they round-trip cleanly through JSON to the caller; small risk of confusion if the system clock jumps backwards, accepted in exchange for code simplicity.
- **`server.py`** — `build_http_app()` wraps `mcp.streamable_http_app()` and shadows `/.well-known/oauth-authorization-server` so it advertises only `client_secret_post` (the SDK default also lists `client_secret_basic`, which the static client rejects). `build_app()` constructs a `FastMCP` instance with `auth_server_provider`, `AuthSettings`, and `transport_security`. Registers four tools: `hermes_ask` (sync default; `async_mode=True` spawns a daemon thread and returns a `job_id`), `hermes_check(job_id)`, `hermes_cancel(job_id)`, and `hermes_reset()`. FastMCP itself adds `/authorize`, `/token`, `/.well-known/oauth-authorization-server`, and the `RequireAuthMiddleware` that gates `/mcp`. `serve()` runs uvicorn.
- **`doctor.py`** — `run_checks()` probes the gateway's `/v1/health` (no auth) and `/v1/models` (with `HERMES_API_KEY`); warns if `HERMES_MODEL` isn't in the returned model list.

**Four-tool design.** The tools form a tight lifecycle: submit (`hermes_ask`), poll (`hermes_check`), abandon a single job (`hermes_cancel`), wipe the store (`hermes_reset`). Do not add tools for *new* use cases (different actions, different domains) without discussing in an issue first.

**`hermes_reset` is a global operation.** The job store is shared across every MCP caller (multiple Claude sessions, background Hermes-agent workflows, etc.). Resetting wipes them all. The tool description warns the LLM to confirm with the user before calling it when other work might be in flight.

**Cancellation is a tombstone, not a kill switch.** `hermes_cancel` updates this server's bookkeeping; the worker thread keeps running and the gateway keeps doing whatever it was doing. There is no way around this in CPython — you cannot safely kill a thread blocked on `httpx.post`. The tool's description spells this out loudly so the LLM relays the caveat to the user. If we ever want real cancellation, the path is to rewrite `HermesClient` against `httpx.AsyncClient` with cancellation tokens and run the whole server on asyncio — large refactor, scoped for a future major version.

## Key constraints

- All four required env vars must be set or the server refuses to start.
- `client_secret` is enforced at `/token` and compared with `hmac.compare_digest()` (delegated to the MCP SDK's `ClientAuthenticator`).
- `mcp` is pinned `<2`: mcp 2.x renamed `FastMCP` and changed the auth APIs, and an unpinned install resolves to it.
- Access tokens are in-memory only — by design. Restart invalidates all sessions. **Claude Desktop does NOT re-auth transparently** in practice: it surfaces "Error occurred during tool execution" on the next call and the user has to manually Disconnect / Reconnect the connector once. The `client_id` / `client_secret` are saved on the connector, so the reconnect doesn't require re-pasting credentials. (Persisting tokens — and async-mode jobs — to disk is on the v0.4.0 roadmap.)
- Async-mode jobs are also in-memory only (`jobs.py`). A server restart drops every job, in-flight or completed; if a user is mid-poll they will see `status: unknown`. The same Disconnect/Reconnect dance applies after a restart.
- Refresh-token rotation is **atomic-pop-then-mint** in `oauth.py` — concurrent `/token` requests with the same refresh token cannot both succeed.
- Prompt content must only be logged at DEBUG level, not INFO (privacy by default). The `state` query parameter is sanitized before logging. Async-job records intentionally store only `prompt_chars` (not the prompt itself).
- Unexpected (non-`HermesError`) exceptions in the async worker thread surface as `error: "unexpected error: <ExceptionType>"` — never `str(exc)` — to preserve the existing invariant that gateway and library error bodies are not echoed in user-facing errors. Full traceback lands in the server log at ERROR.
- `BIND_HOST` defaults to `127.0.0.1`; binding elsewhere gets a startup warning.
- mypy is run on `src/` only — the `mcp` package lacks stubs and is excluded.
- Python ≥ 3.11 required; CI tests 3.11 and 3.12.
- Test count is 160 as of the confidential-client fix (112 as of v0.3.0); a sudden drop is a regression smell.

## Deployment shape

This project ships with `deploy/hermes-mcp.service` and `deploy/cloudflared.service` as **systemd user units** (matching the `hermes-gateway` / `mcp-proxy` services it sits next to). Env file lives at `~/.config/hermes-mcp/env` mode 0600. `loginctl enable-linger` is required so user services start at boot.

`deploy/hermes-mcp.service` ships with non-trivial hardening flags: `ProtectSystem=strict`, `ProtectHome=read-only` + `ReadWritePaths=%h/.config/hermes-mcp`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `LockPersonality=true`, `MemoryDenyWriteExecute=true`, empty `CapabilityBoundingSet=`, and `SystemCallFilter=@system-service` (excluding `@privileged @resources`). They are verified to start cleanly with the current Python deps; **do not strip them without intent** and re-test the service start. If a future dependency needs JIT or syscalls outside `@system-service`, narrow the rule rather than removing it.

## Release process

Per-release steps:
1. Bump version in **both** `src/hermes_mcp/__init__.py` and `pyproject.toml`. The release workflow checks they match the pushed tag and fails the build if they don't.
2. Move the `Unreleased` section in `CHANGELOG.md` to the new version heading with today's date. Keep the `[Unreleased]` heading empty above it. The release workflow's `github-release` job extracts this section verbatim as the release notes.
3. Commit, tag `vX.Y.Z`, `git push origin main vX.Y.Z`. The tag push fires `.github/workflows/release.yml`, which builds the wheel + sdist, publishes to PyPI via OIDC trusted publishing (no API tokens stored anywhere), and creates a GitHub Release with the CHANGELOG section and built artifacts attached.

One-time setup (already done for this project, listed here so a maintainer rotating the secret doesn't re-do it from scratch): a trusted publisher is configured at https://pypi.org/manage/project/hermes-mcp/settings/publishing/ pointing at this repo, workflow filename `release.yml`, no environment. If the workflow is renamed or moved, the PyPI trusted-publisher entry must be updated to match or the publish step will fail.

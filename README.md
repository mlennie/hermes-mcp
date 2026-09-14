# hermes-mcp

> An MCP server that lets **Claude Desktop**, **Claude.ai (web + mobile)**, **OpenAI Codex desktop**, **Cursor**, and any other MCP client (via OAuth 2.1 or static bearer token) delegate tasks to a local **[Hermes Agent](https://github.com/hermes-agent/hermes-agent)** running on your own hardware. (See [Client compatibility](#client-compatibility) for the current matrix.)

Use Claude (or another supported MCP client) as your daily chat. When you ask for something Hermes is built for — scheduling cron jobs, browser automation, email, document creation, persistent skills, WhatsApp/Slack messaging — your client calls Hermes through this bridge.

> **Do not write code?** Start with the
> [plain-language setup and verification guide](docs/non-coder-start-here.md). It explains
> what each component does, the shortest Hostinger-and-Town path, how to test it safely,
> and what common errors mean.

```
┌──────────────────────────────────────────────────────┐
│ MCP client                                           │
│ (Claude Desktop / Claude.ai / Codex CLI / Cursor /…) │
└────────────────────────┬─────────────────────────────┘
                         │ HTTPS + OAuth 2.1
                         ▼
                ┌──────────────────────┐
                │ cloudflared tunnel   │  (public HTTPS edge)
                └──────────┬───────────┘
                           │
                           ▼ localhost:8765
                ┌──────────────────────┐
                │ hermes-mcp           │  (FastMCP, Streamable HTTP)
                │ - OAuth 2.1 + PKCE   │
                │ - HTTP -> gateway    │
                └──────────┬───────────┘
                           │ HTTP /v1/chat/completions
                           ▼ localhost:8642
                ┌──────────────────────┐
                │ hermes-gateway       │  (the running Hermes brain;
                │                      │   same agent loop Telegram uses)
                └──────────────────────┘
```

## Quickstart

These steps assume you already have **Hermes Agent** installed and working on a Linux/WSL machine, with the gateway listening on `127.0.0.1:8642`.

```bash
# 1. Install
pipx install hermes-mcp

# 2. Mint OAuth client credentials
hermes-mcp mint-client                  # prints OAUTH_CLIENT_ID + OAUTH_CLIENT_SECRET

# 3. Start a quick tunnel (testing only — URL changes on restart)
cloudflared tunnel --url http://127.0.0.1:8765
# prints: https://random-words-here.trycloudflare.com

# 4. Export env vars (using the URL from step 3)
export OAUTH_CLIENT_ID=<from step 2>
export OAUTH_CLIENT_SECRET=<from step 2>
export OAUTH_ISSUER_URL=https://random-words-here.trycloudflare.com
export MCP_ALLOWED_HOSTS=random-words-here.trycloudflare.com
export HERMES_API_KEY=<the API_SERVER_KEY from ~/.hermes/.env>

# 5. Verify everything is wired up
hermes-mcp doctor

# 6. Run
hermes-mcp serve
```

Then connect from your MCP client of choice — see [Client compatibility](#client-compatibility) below for per-client config snippets (Claude Desktop, Codex CLI, Cursor).

Once you've confirmed it works end-to-end, follow the [named tunnel](#named-tunnel-for-keeping-it) and [systemd](#running-as-a-service-on-the-mini-pc) sections to make it permanent.

Try asking: *"Use Hermes to schedule a daily cron job that emails me a summary of my inbox at 8am."*

---

## Configuration

All settings via environment variables. See [`.env.example`](.env.example) for the canonical list.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OAUTH_CLIENT_ID` | **yes** | — | Static OAuth 2.1 client ID. Generate with `hermes-mcp mint-client`. |
| `OAUTH_CLIENT_SECRET` | **yes** | — | Static OAuth 2.1 client secret (≥32 chars). Generate with `hermes-mcp mint-client`. |
| `OAUTH_ISSUER_URL` | **yes** | — | Public HTTPS URL where the server is reachable (your tunnel hostname). |
| `HERMES_API_KEY` | **yes** | — | Bearer token for the local Hermes gateway's OpenAI-compatible API (the `API_SERVER_KEY` from `~/.hermes/.env`). |
| `HERMES_API_URL` | no | `http://127.0.0.1:8642` | Base URL of the running Hermes gateway. |
| `HERMES_MODEL` | no | `hermes-agent` | Model identifier sent to `/v1/chat/completions`. |
| `MCP_ALLOWED_HOSTS` | no | (localhost only) | Comma-separated additional Host header values to accept (typically your public tunnel hostname). MCP uses this for DNS-rebinding protection. |
| `BIND_HOST` | no | `127.0.0.1` | Bind address. The tunnel reaches it on localhost. **Do not** bind `0.0.0.0` unless you understand the implications. |
| `BIND_PORT` | no | `8765` | Port. |
| `HERMES_REQUEST_TIMEOUT_SECONDS` | no | `300` | Max wall-clock per `hermes_ask` call. |
| `OAUTH_ALLOWED_REDIRECT_SCHEMES` | no | `claude,claudeai,cursor` | Comma-separated OAuth redirect-URI custom schemes to accept. `https` and `http`-on-localhost always allowed. Extend to add support for new MCP clients (e.g. add `vscode` for Continue). |
| `MCP_BEARER_TOKEN` | no | (unset) | Optional static bearer token (32+ chars). When set, the server accepts `Authorization: Bearer <token>` directly at `/mcp`, in addition to OAuth. Necessary for MCP clients whose UI has no OAuth flow (Codex desktop's custom-MCP form, Cursor's `headers` block). Generate with `hermes-mcp mint-bearer-token`. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` enables prompt-body logging. |

## Client compatibility

hermes-mcp speaks plain **Streamable HTTP** and supports two auth paths so different MCP clients can connect:

- **OAuth 2.1 (PKCE-only public client).** Static `OAUTH_CLIENT_ID` + auto-approve `/authorize`. PKCE is the dynamic per-exchange secret; `client_secret` is accepted in the form but no longer enforced (clients that send one still work, clients that omit it work too). DCR is disabled.
- **Static bearer token.** Set `MCP_BEARER_TOKEN` and the server accepts `Authorization: Bearer <token>` directly at `/mcp`, bypassing OAuth entirely. Necessary for clients whose UI has no OAuth field.

Pick whichever the client's UI supports — both auth paths coexist on the same server instance.

### Tested ✅

| Client | Auth | How to connect |
|---|---|---|
| **Claude Desktop / Claude.ai (web + mobile)** | OAuth | Settings → Connectors → Add custom connector → paste the server URL + your `OAUTH_CLIENT_ID` + your `OAUTH_CLIENT_SECRET`. (The secret is accepted but no longer enforced server-side; the field is still required by Claude's UI, so set it.) |
| **OpenAI Codex desktop** | Bearer | Settings → MCP → Connect to a custom MCP → Streamable HTTP → URL = your tunnel + `/mcp`, **Bearer token env var** = name of an OS env var on your laptop that holds your `MCP_BEARER_TOKEN` value. Restart Codex desktop after setting the env var so it's inherited. Skips OAuth entirely. |
| **Cursor** | Bearer | Settings → MCP → Add custom MCP → paste the JSON below into `~/.cursor/mcp.json`. No OAuth flow, no extra config. |
| **Town** | OAuth | Settings → MCP → Add Server → URL = your public bridge URL + `/mcp` → complete OAuth. Enable the server only for the routines that need it. See the [Town + Hostinger guide](docs/town-hostinger.md). |

#### Cursor — exact `~/.cursor/mcp.json` snippet

```json
{
  "mcpServers": {
    "hermes": {
      "url": "https://hermes.claude-hermes-mcp.com/mcp",
      "headers": {
        "Authorization": "Bearer <your-MCP_BEARER_TOKEN>"
      }
    }
  }
}
```

Replace `<your-MCP_BEARER_TOKEN>` with the token printed by `hermes-mcp mint-bearer-token`. The token sits in your laptop's `~/.cursor/mcp.json` (file-mode protected by your OS); if you'd rather not have it there in cleartext, use `"Authorization": "Bearer ${env:HERMES_MCP_BEARER}"` and set the env var on your OS instead. Save the file and Cursor picks it up automatically — no restart needed.

### Untested (likely workable via bearer)

These clients all support setting custom request headers, which should be enough to drive the bearer-token path. We haven't tested them end-to-end — if you connect one, please [open an issue](https://github.com/mlennie/hermes-mcp/issues) with your config so we can promote it.

| Client | Likely auth | Where to set it |
|---|---|---|
| **Continue (VSCode)** | Bearer via `requestOptions.headers` | `continue` config: `requestOptions.headers.Authorization = "Bearer ..."` |
| **OpenAI Codex CLI** | OAuth (PKCE) | `~/.codex/config.toml` → `[mcp_servers.hermes.oauth] client_id = "..."`. **Caveat:** Codex CLI's OAuth flow uses a localhost callback on whichever machine `codex` runs on; if you SSH from a laptop into the mini-PC where hermes-mcp lives, the laptop browser can't reach that callback. Easiest workaround is to use the desktop app (above) instead. |

### Adding a new client whose custom URI scheme isn't in the default

If your client uses OAuth AND a redirect scheme not in the default `claude,claudeai,cursor`, add it to `OAUTH_ALLOWED_REDIRECT_SCHEMES`:

```bash
export OAUTH_ALLOWED_REDIRECT_SCHEMES=claude,claudeai,cursor,vscode
```

Then restart `hermes-mcp` and complete the OAuth handshake from the new client.

## What MCP clients see

The MCP server exposes four tools:

### `hermes_ask(prompt, session_id?, toolsets?, async_mode?)`

Delegates a task to Hermes. Use it for anything the calling LLM cannot do directly:

- Scheduling cron jobs / recurring tasks
- Browser-driven web search and scraping
- Sending email
- Creating, saving, or editing local documents
- Anything that should persist after this chat ends (Hermes memory, skills)
- Sending WhatsApp / Slack messages via Hermes's messaging gateway

Pass the same `session_id` across calls within one chat to let Hermes build on previous steps (draft → refine → save). It is forwarded as the `X-Hermes-Session-Id` header so Hermes threads the call into an existing session.

The `toolsets` argument is accepted for backward compatibility but is currently ignored — toolset selection now lives in your Hermes config (`platform_toolsets.api_server`). Set it there to match the Telegram surface (typically `[hermes-telegram]`) so MCP clients get the same tools the Telegram path does.

#### Async mode for long-running tasks

Most MCP clients enforce a per-tool-call timeout: Claude.ai / Claude Desktop is roughly **two minutes**; Codex CLI, Cursor, and others differ. If Hermes is going to take longer than the client's limit, the call fails with a tool-execution error and any side effects already started (emails sent, files created) keep running but aren't reported back. Async mode sidesteps this:

```jsonc
// hermes_ask(prompt="...", async_mode=true) returns immediately:
{"job_id": "8a3f...e21", "status": "pending", "completion_scope": "gateway_response"}
```

Then poll `hermes_check(job_id)` until `status` is `completed`, `failed`, or `cancelled`. Hermes keeps running in the background regardless of whether you poll. Job results are stored in SQLite and survive a server restart for ~24 hours after reaching a terminal state. Interrupted requests retain an explicit unconfirmed outcome; they are never automatically replayed.

Every async response includes `completion_scope: "gateway_response"`. A `completed` job
means the Hermes gateway returned a response. If that response is a receipt for work
delegated to a separate worker, queue, CI run, or deployment, query that downstream state
before telling the user the work is complete. Reuse the same `session_id` for that status
request; do not resubmit the original instruction merely because the bridge job ended.

**When to use async** — the calling LLM reads heuristics from the tool description and should pick the right mode on its own, but the rules of thumb are:

| Use **async** when ANY is true | Use **sync** only when ALL are true |
|---|---|
| 3+ distinct external actions (multi-folder, multi-issue, multi-email) | Exactly one external action, or none |
| Browser-driven work (scraping, research) | Confident response in <30s |
| Drive trees, doc generation, multi-recipient outreach | No Telegram-approval-gated tools likely to fire |
| Multi-step agentic work (Hermes will chain tools) | |
| Estimated >30 seconds | |
| Telegram approval buttons may appear | |
| **You're not sure** — pick async | |

False async costs you a polling loop. False sync costs you the whole task hitting the 2-minute cliff. The asymmetry strongly favors async when in doubt.

If you want to force the choice, just say so in your prompt: *"use `async_mode=true` for this"*.

### `hermes_check(job_id)`

Returns a JSON string with the current status of an async job:

```jsonc
{"job_id": "8a3f...e21", "status": "completed", "completion_scope": "gateway_response", "created_at": 1747...,
 "finished_at": 1747..., "prompt_chars": 12303, "session_id": "...",
 "result": "..."}
{"job_id": "8a3f...e21", "status": "failed",    "completion_scope": "gateway_response", "error":  "...", ...}
{"job_id": "8a3f...e21", "status": "cancelled", "completion_scope": "gateway_response", ...}
{"job_id": "8a3f...e21", "status": "running",   "completion_scope": "gateway_response", ...}
{"job_id": "8a3f...e21", "status": "pending",   "completion_scope": "gateway_response", ...}
{"job_id": "<your-input>", "status": "unknown", "completion_scope": "gateway_response"} // never issued by this server, reaped after 24h, or wiped by hermes_reset
```

`created_at` and `finished_at` are epoch seconds — the calling LLM can subtract them to show "running for N minutes" in chat.

### `hermes_cancel(job_id)`

Releases an in-flight async job. **Critical caveat: this does NOT stop the gateway from running.**

Python threads can't be safely killed mid-`httpx.post`, so cancellation is bookkeeping only: subsequent `hermes_check` calls return `status: cancelled`, but the Hermes worker keeps running until it finishes or hits its 300-second timeout. Anything Hermes does in the meantime — emails sent, Drive files created, Linear issues opened — *happens anyway*.

Cancel when you want to **release the result**, not undo the work. If the work needs to be undone, ask Hermes to undo it explicitly.

Returns the same JSON shape as `hermes_check`. Cancelling an already-terminal job is a no-op and returns the current status unchanged.

### `hermes_reset()`

Wipes every job from the durable store in a single call. Use this to recover from a cluttered or stuck queue without restarting the server process. After it returns, every prior `job_id` becomes `unknown` on `hermes_check` and `hermes_cancel`.

```jsonc
// hermes_reset() returns:
{"cleared": 4, "by_status": {"running": 1, "pending": 3}}
{"cleared": 0, "by_status": {}}   // empty store
```

Same caveat as `hermes_cancel`, but applied to everything at once: it does **not** stop in-flight worker threads or gateway calls. Workers whose jobs are wiped run to completion and their side effects happen anyway; their eventual `mark_completed` becomes a safe no-op when the job id is gone.

**The job store is shared across all MCP callers.** If multiple client sessions (Claude, Codex, Cursor, ...) or a background Hermes-agent workflow are pointed at the same MCP bridge, resetting wipes their jobs too. Treat it as a global operation and confirm with the user before calling it if other work might be in flight.

Expired terminal jobs (older than the 24h TTL) are reaped lazily before counting, so the `by_status` map reflects only what was actually live in the store at call time.

## Network exposure: `cloudflared`

Recommended. Free, open-source, no bandwidth cap that matters at personal scale.

If Hermes Agent already runs under Docker behind Traefik, including Hostinger's Docker
Manager, use the [Town + Hostinger Docker/Traefik guide](docs/town-hostinger.md). It includes
a container image, two Compose patterns, public-route checks, durable-session guidance, and the
router-label placement that prevents a healthy deployment from returning `502`.

Town users can also adapt the
[durable Hermes Project Lead routine template](docs/town-routine-template.md) as a guarded,
single-session front desk for a project.

There are two flavors. Use the **quick tunnel** to test today; use the **named tunnel** for any setup you want to leave running.

### Quick tunnel (for testing)

Throwaway URL, no Cloudflare account needed, dies on `cloudflared` restart. Perfect for the first end-to-end test.

```bash
# 1. Install cloudflared
sudo apt install cloudflared        # or download from cloudflare.com

# 2. Run a quick tunnel pointed at the local bridge
cloudflared tunnel --url http://127.0.0.1:8765
```

`cloudflared` prints a URL like `https://random-words-here.trycloudflare.com`. That's your tunnel for as long as the process runs. Use it as the server URL in your MCP client (see [Client compatibility](#client-compatibility) above):

```
Connector URL:  https://random-words-here.trycloudflare.com/mcp
Client ID:      <from `hermes-mcp mint-client`>
Client Secret:  <from `hermes-mcp mint-client`>
```

Set `OAUTH_ISSUER_URL` to `https://random-words-here.trycloudflare.com` and add the hostname to `MCP_ALLOWED_HOSTS` so MCP's DNS-rebinding check accepts it.

⚠ Quick tunnels are ephemeral. The hostname changes every restart — your client's connector breaks every time. Move to a named tunnel as soon as you're past the smoke test.

### Named tunnel (for keeping it)

Stable hostname on a Cloudflare-managed domain. Survives reboots.

**Prerequisite:** a domain on Cloudflare DNS. Easiest is registering one through [Cloudflare Registrar](https://dash.cloudflare.com/?to=/:account/domains/register) (~$10/yr, sold at cost). If you already have a domain elsewhere, change its nameservers at the registrar to the two Cloudflare gives you, wait for the zone to go Active, then continue. **Don't** put your primary domain on Cloudflare DNS without first auditing email/Workspace records — you'll need to verify Cloudflare's auto-import covers MX, SPF, DKIM, and DMARC before changing nameservers. Buying a separate cheap domain just for the tunnel is the boring safe move.

```bash
# 1. Authorize this machine on your Cloudflare account (interactive: opens a URL)
cloudflared tunnel login

# 2. Create the tunnel — pick any name, e.g. "hermes"
cloudflared tunnel create hermes

# 3. Route a DNS hostname to it (requires the domain be on Cloudflare DNS)
cloudflared tunnel route dns hermes hermes.your-domain.example

# 4. Configure ~/.cloudflared/config.yml
cat > ~/.cloudflared/config.yml <<EOF
tunnel: <UUID-from-step-2>
credentials-file: $HOME/.cloudflared/<UUID-from-step-2>.json
ingress:
  - hostname: hermes.your-domain.example
    service: http://127.0.0.1:8765
  - service: http_status:404
EOF

# 5. Test it
cloudflared tunnel run hermes
# In another terminal:
#   curl -sS https://hermes.your-domain.example/.well-known/oauth-authorization-server
#   ⇒ should print the OAuth metadata JSON
```

Your stable URL is now `https://hermes.your-domain.example`. Update your MCP client to point at `<URL>/mcp`, set `OAUTH_ISSUER_URL` to the URL, and add `hermes.your-domain.example` to `MCP_ALLOWED_HOSTS`.

Run cloudflared as a systemd user service — see [`deploy/cloudflared.service`](deploy/cloudflared.service):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/cloudflared.service ~/.config/systemd/user/cloudflared.service
systemctl --user daemon-reload
systemctl --user enable --now cloudflared.service
journalctl --user -u cloudflared -f
```

## Alternative tunnel: `ngrok`

Equally valid; pick this if you already have an ngrok account and don't want to set up Cloudflare DNS.

```bash
ngrok config add-authtoken <your-token>

# Free tier includes one stable static domain
ngrok http 8765 --domain=your-name.ngrok-free.app
```

A systemd unit is provided in [`deploy/ngrok.service`](deploy/ngrok.service).

## Adding the connector in Claude

**Claude Desktop:** Settings → Connectors → Add custom connector → paste `<tunnel-url>/mcp` → paste your `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET`. Claude completes the OAuth 2.1 authorization-code flow with PKCE automatically.

**Claude mobile app:** same flow under Settings → Connectors. The connector you add is per-account, so it works on both Desktop and mobile from one configuration.

> Screenshots are coming once we cut a v0.1.0 release. PRs welcome.

## Running as a service on the mini-PC

Install [`deploy/hermes-mcp.service`](deploy/hermes-mcp.service) as a **systemd user unit** so it shares the lifecycle of your other personal services (e.g. `hermes-gateway`, `mcp-proxy`):

```bash
# 1. Install hermes-mcp on a stable path
pipx install hermes-mcp

# 2. Set up the env file (mode 0600)
mkdir -p ~/.config/hermes-mcp
install -m 0600 .env.example ~/.config/hermes-mcp/env
$EDITOR ~/.config/hermes-mcp/env       # fill in OAUTH_*, HERMES_API_KEY, etc.

# 3. Install the unit
mkdir -p ~/.config/systemd/user
cp deploy/hermes-mcp.service ~/.config/systemd/user/

# 4. Make sure user services start at boot, not just login
loginctl enable-linger "$USER"

# 5. Enable + start
systemctl --user daemon-reload
systemctl --user enable --now hermes-mcp
journalctl --user -u hermes-mcp -f
```

Restart after editing the env file: `systemctl --user restart hermes-mcp`.

### What survives a reboot

| Concern | Behavior |
|---|---|
| `hermes-mcp` process | Starts at boot. ✅ |
| `cloudflared` tunnel | Starts at boot. ✅ |
| Tunnel URL | Stable (named tunnel). ✅ |
| OAuth `client_id` / `client_secret` | Read from `~/.config/hermes-mcp/env` at startup. ✅ |
| `MCP_BEARER_TOKEN` (if set) | Read from `~/.config/hermes-mcp/env` at startup. ✅ |
| Completed async-job results | Persist on disk for ~24 hours after completion; Docker needs the supplied named volume. ✅ |
| Interrupted async jobs | Retained as failed with an **unconfirmed outcome**; never automatically resubmitted. |
| Live OAuth access / refresh tokens | **Stored in memory only — lost on every restart.** ❌ |

**Practical impact:** the host can reboot freely; the bridge comes back up on the same URL. But Claude Desktop is holding access and refresh tokens that are now invalid (the in-memory store they were minted from is gone). On the next call, Claude usually reports `"Error occurred during tool execution"` rather than transparently re-running OAuth.

**The fix is one click**, not re-entering credentials:

> Claude Desktop → **Settings → Connectors** → click your `hermes-mcp` connector → **Disconnect** → **Reconnect**.

Claude does the OAuth flow against the bridge using the saved `client_id` / `client_secret` and you're back online. Same goes for any time you `systemctl --user restart hermes-mcp` (e.g. after editing the env file or upgrading the package).

This is a known limitation of the in-memory token store. Persisting tokens to disk is on the roadmap.

## Responsiveness, durable jobs, and upgrades

Waiting for a Hermes response now yields to the MCP event loop. Status checks can
continue during a long request. Short SQLite operations use an explicitly sized
executor (`HERMES_MCP_EXECUTOR_WORKERS`, default 16, valid range 1–128).
The server remains a single worker; do not add Uvicorn workers against the same
job store. A process-owner lock rejects that configuration.

`HERMES_MCP_JOB_STORE_PATH` selects the SQLite file. The local default is
`~/.local/state/hermes-mcp/jobs.sqlite3`. Docker examples mount a named volume at
`/var/lib/hermes-mcp`; the systemd example uses the writable
`~/.config/hermes-mcp` directory. Keep the database, WAL, and lock files together
on persistent local storage with a private parent directory.

**Existing installation? Follow [the upgrade guide](docs/upgrading.md).** It covers
preserving existing deployment settings, adding the volume, checking a completed
job after restart, and reconnecting OAuth if needed. Results already lost by an
older in-memory bridge cannot be recovered by upgrading.

Job completion still means **the gateway returned an answer**, not that every
worker or deployment mentioned in that answer finished. The Town
`completion_scope: gateway_response` field remains intact.

## Security

**This bridge lets a remote LLM run actions on your machine via Hermes.** Treat it accordingly. Full threat model in [THREAT_MODEL.md](THREAT_MODEL.md). In short:

- **Do not run Hermes with `--yolo`.** Keep approval hooks on.
- **Scope `platform_toolsets.api_server`** in your Hermes config to the minimum toolset your use case needs (see [What Claude sees](#hermes_askprompt-session_id-toolsets)).
- **`MCP_BEARER_TOKEN` (if set) and `HERMES_API_KEY` are the long-lived credentials.** The bearer token, if configured, is the only gate behind the tunnel URL for clients on that path — a leak is equivalent to remote action execution on your host. `HERMES_API_KEY` lets an attacker bypass the bridge and call the gateway directly. Rotate (`hermes-mcp mint-bearer-token` for the bearer; edit `API_SERVER_KEY` in `~/.hermes/.env` for the gateway) if exposed.
- **`OAUTH_CLIENT_SECRET` is *not* a security gate.** Despite the name, it's accepted at `/token` for backward compatibility (Claude's UI requires a value to send) but not enforced server-side. PKCE — specifically the mandatory `code_verifier` check at every authorization-code exchange — is what protects token issuance. Leaking `OAUTH_CLIENT_SECRET` does not on its own let an attacker mint access tokens. Treat it as you would a username, not a password.
- **Prompt injection is real.** A malicious prompt slipping into Claude's context (via a webpage, a file you pasted) can craft tool calls. Hermes's own approval hooks are your last line of defense — keep them on.

Code-side mitigations baked in:

- OAuth 2.1 with **mandatory PKCE-S256** (server enforces `code_verifier` on every authorization-code exchange; mismatch returns `invalid_grant`). Server registered as a **public client** (`token_endpoint_auth_method=none`), so `client_secret` is not part of the auth contract — PKCE is the dynamic per-exchange secret.
- Static bearer-token path (`MCP_BEARER_TOKEN`) compared in constant time via `hmac.compare_digest`. First use logs a single INFO-level audit line per process.
- Authorization codes are single-use with atomic pop-on-exchange; refresh tokens rotate atomically and approximate RFC 6819 reuse detection.
- `redirect_uri` scheme allowlist on `/authorize` (https, http-on-localhost, claude, claudeai, cursor; configurable via `OAUTH_ALLOWED_REDIRECT_SCHEMES`) prevents the bridge becoming an open redirector to `javascript:` / `data:` URIs.
- Access tokens are 256-bit `secrets.token_urlsafe`, expire after 1 hour, live only in memory (no on-disk persistence). Refresh tokens 30d, also in memory.
- DNS-rebinding protection via `MCP_ALLOWED_HOSTS` enforced at the transport layer.
- Prompt bodies and gateway response bodies logged only at `DEBUG`. INFO logs are endpoint + length + session_id + duration only. The OAuth `state` parameter is sanitized before logging.
- Bind defaults to `127.0.0.1`; non-loopback `BIND_HOST` triggers a startup warning.
- **No telemetry, ever.** Your prompts go Claude → tunnel edge → bridge → gateway. Nothing else.

## Common pitfalls

- **`hermes-mcp doctor` reports "hermes gateway unreachable"** → the gateway isn't running. `systemctl --user status hermes-gateway` will tell you why.
- **`doctor` reports "rejected the API key (401)"** → `HERMES_API_KEY` doesn't match `API_SERVER_KEY` in `~/.hermes/.env`. Update one or the other and restart.
- **Connector stuck on "Verifying"** → most often it's a wrong `client_id` or `OAUTH_ISSUER_URL` not matching the URL you pasted into Claude (they must be the same hostname). The `client_secret` value doesn't matter to the server but Claude won't submit the form without one — paste anything ≥1 char.
- **"Invalid Host header" / 421** → your tunnel hostname isn't in `MCP_ALLOWED_HOSTS`. Add it (comma-separated) and restart.
- **Cloudflared 502** → `hermes-mcp` isn't running. `journalctl --user -u hermes-mcp` will tell you why.
- **After a reboot or `systemctl --user restart hermes-mcp`, Claude says "Error occurred during tool execution"** → expected. OAuth tokens are in-memory; restarting the bridge invalidates them. **Fix:** in Claude Desktop, Settings → Connectors → your hermes-mcp connector → Disconnect → Reconnect. The `client_id`/`client_secret` are saved, so Claude re-auths in a few seconds. See [What survives a reboot](#what-survives-a-reboot).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Apache-2.0. See [LICENSE](LICENSE).

## Status

This is an unofficial bridge. It is not affiliated with or endorsed by the Hermes Agent project, and not affiliated with Anthropic.

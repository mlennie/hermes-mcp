"""FastMCP server exposing `hermes_ask` / `hermes_check` / `hermes_cancel`
/ `hermes_reset` over Streamable HTTP, gated by OAuth 2.1.

`build_app()` constructs a FastMCP instance wired up with our static-client
OAuth provider. FastMCP itself adds the bearer-validation middleware and the
authorization endpoints (`/authorize`, `/token`, `/.well-known/...`).

Async-job state lives in the shared `JobStore` owned by `build_app`; the
four tools are tightly coupled around its lifecycle (submit / poll /
release / clean-slate).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from .config import Config, LogLevel
from .hermes_client import HermesClient, HermesError
from .jobs import COMPLETION_SCOPE, JobStore
from .oauth import StaticClientProvider
from .persistent_jobs import PersistentJobStore

UvicornLogLevel = Literal["critical", "error", "warning", "info", "debug"]
_UVICORN_LEVELS: dict[LogLevel, UvicornLogLevel] = {
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "warning",
    "ERROR": "error",
    "CRITICAL": "critical",
}


def _uvicorn_log_level(level: LogLevel) -> UvicornLogLevel:
    return _UVICORN_LEVELS[level]


logger = logging.getLogger(__name__)

_TOOL_DESCRIPTION = """\
Delegate a task to Hermes Agent on this user's mini-PC.

Use this when the user asks for things the calling LLM cannot do directly
itself:
  - Scheduling cron jobs / recurring tasks
  - Browser-driven web search and scraping
  - Sending email
  - Creating, saving, or editing local documents
  - Anything that should persist after this chat ends (Hermes memory, skills)
  - Sending WhatsApp / Slack messages via Hermes's messaging gateway

Args:
  prompt: Natural-language instruction for Hermes.
  session_id: Optional. Pass the same id across multiple calls in one chat
    to let Hermes remember prior steps (e.g. draft -> refine -> save).
  toolsets: Optional. Restrict Hermes to specific toolsets for this call.
  async_mode: Optional. Default False. Decides whether `hermes_ask` blocks
    on the result or returns a job id immediately. Read this carefully —
    picking wrong on a long task hits the MCP client's per-tool-call
    timeout and the user sees a tool-execution error. Timeouts vary by
    client (Claude.ai / Claude Desktop is ~2 minutes; Codex CLI, Cursor,
    and others differ); when uncertain, prefer async.

    USE async_mode=True (the safer default for non-trivial work) WHEN ANY
    of these are true about the user's request:
      - Three or more distinct external actions (e.g. "create 5 folders",
        "send 3 emails", "open N Linear issues")
      - Browser-driven work (each page load adds 5-10 seconds; almost any
        scraping or research task should be async)
      - Drive folder trees, document generation, or multi-recipient outreach
      - Multi-step agentic work where Hermes will chain several tool calls
        before responding
      - Anything you'd estimate could take more than ~30 seconds
      - Tasks that may require user approval on Telegram (those buttons
        add latency unpredictably)
      - You're not sure. False async costs you a polling loop. False sync
        costs you the whole task hitting the client's timeout and side
        effects (emails sent, files created) being partial and unreported.

    USE async_mode=False (sync) ONLY when ALL of these are true:
      - Exactly one external action (one email, one message, one cron,
        one file save) OR no external action at all (a question Hermes
        can answer from its own knowledge / skills)
      - You confidently expect the response in under ~30 seconds
      - There are no Telegram-approval-gated tools likely to fire

  Async behavior: `hermes_ask` returns a JSON string containing a job id,
  `status: pending`, and `completion_scope: gateway_response` immediately.
  Poll `hermes_check(job_id)` every 5-10 seconds (not faster) for the
  result. A `completed` bridge job proves only that the Hermes gateway
  returned a response. If that response says work was delegated to a
  downstream worker, queue, or service, use the same `session_id` to ask
  Hermes for current downstream state before reporting that work complete.
  Call `hermes_cancel(job_id)` if the user no longer wants the result —
  note this RELEASES the bookkeeping but does NOT stop the gateway from
  running; side effects already started will continue.

Returns:
  Sync mode: Hermes's final answer text.
  Async mode: JSON string containing `job_id`, `status: pending`, and
    `completion_scope: gateway_response`.
"""

_CHECK_TOOL_DESCRIPTION = """\
Check the status of an async hermes_ask job.

Use this only with a `job_id` returned by a prior `hermes_ask(..., async_mode=True)`
call. Polls Hermes Agent's durable job store for the result.

Polling guidance: wait at least 5-10 seconds between calls; Hermes jobs that
need async mode typically take minutes, not seconds, and tight polling just
burns the user's tokens. `completed`, `failed`, `cancelled`, and `unknown`
are all terminal — do not keep polling after seeing them.
Restart-interrupted jobs are retained as failed with an unconfirmed-outcome error.
Never automatically resubmit them: downstream effects may already have happened.
`unknown` means one of: the id was never issued by this server, the result was reaped
(24h after a terminal state), or the job was wiped by
a `hermes_reset` call. Polling will never turn `unknown` back into a result.

Args:
  job_id: The job id returned by the original async hermes_ask call.

Returns:
  JSON string with `job_id`, `status` (one of `pending`, `running`,
  `completed`, `failed`, `cancelled`, `unknown`), `completion_scope`
  (`gateway_response`), `created_at` (epoch seconds), `prompt_chars`, and:
    - `session_id` if the caller supplied one
    - `finished_at` (epoch seconds) once terminal
    - `result` on completed
    - `error` on failed
  Jobs are kept ~24 hours after they reach a terminal state.

  `status: completed` applies to the gateway request only. If `result`
  reports that Hermes handed work to another worker, queue, or service,
  query that downstream state separately before declaring the user's work
  complete.
"""

_RESET_TOOL_DESCRIPTION = """\
Clear ALL jobs from this server's durable job store.

Use this to recover from a cluttered or stuck queue when you want a clean
slate without restarting the server process. After this returns, every
prior `job_id` becomes `unknown` on `hermes_check` / `hermes_cancel`.

IMPORTANT — same caveat as hermes_cancel, but for every job at once:
  - Does NOT stop worker threads or underlying gateway calls. Any in-flight
    Hermes work keeps running until it finishes or hits its 300-second
    timeout. Side effects (emails sent, files created, etc.) happen anyway.
  - **All MCP callers share this job store.** Resetting wipes jobs
    submitted by other MCP-client sessions (Claude, Codex, Cursor, etc.)
    and by any background Hermes-agent workflow that uses this same MCP.
    Treat it as a global operation, not a per-session one. Confirm with
    the user before calling it if there is any chance other work is in
    flight that they care about.
  - Use sparingly. Prefer `hermes_cancel(job_id)` for individual jobs you
    know about. Reach for `hermes_reset` only when the queue is in a state
    you don't want to reason about job-by-job.

Returns:
  JSON string with `cleared` (total jobs removed) and `by_status` (a map
  of prior status -> count). Example:
    {"cleared": 4, "by_status": {"running": 1, "pending": 3}}
  An empty store returns {"cleared": 0, "by_status": {}}.
"""

_CANCEL_TOOL_DESCRIPTION = """\
Cancel (release) an async hermes_ask job.

IMPORTANT — what this does and does not do:
  - Marks the job as `cancelled` in this server's bookkeeping. Subsequent
    `hermes_check` calls return `status: cancelled`.
  - Does NOT stop the worker thread or the underlying gateway call. Python
    cannot safely kill a thread that's blocked on I/O. Hermes will keep
    running until it finishes or hits its 300-second timeout. Any side
    effects the gateway produces (emails sent, files created, calendar
    events scheduled, etc.) happen anyway.

Use this when you want to release the *result* (because the user changed
their mind or doesn't need it anymore). Do NOT use this expecting it to
undo work already in progress. If the work itself needs to be undone, do
that explicitly — e.g. ask Hermes to delete the Drive folder it just
created.

Cancelling an already-terminal job (completed, failed, cancelled, or
unknown) is a no-op.

Args:
  job_id: The job id returned by the original async hermes_ask call.

Returns:
  JSON string with the same shape as `hermes_check`. If the job was
  still in flight, `status` will be `cancelled`. If it was already
  terminal, the existing status is returned unchanged.
"""


def _build_transport_security(config: Config) -> TransportSecuritySettings:
    """Allowed-host list passed to FastMCP. Always includes localhost so the
    `hermes-mcp doctor` flow and curl smoke tests still work; appends any
    user-supplied hostnames (typically the public tunnel domain).
    """
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *config.allowed_hosts]
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        *(f"https://{h}" for h in config.allowed_hosts if "://" not in h),
        *(h for h in config.allowed_hosts if "://" in h),
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _run_job(
    client: HermesClient,
    jobs: JobStore | PersistentJobStore,
    job_id: str,
    prompt: str,
    session_id: str | None,
    toolsets: list[str] | None,
) -> None:
    """Background worker: invoke the gateway and stash the outcome.

    Runs in a daemon thread spawned by `hermes_ask(..., async_mode=True)`.
    Any HermesError or unexpected exception is captured into the job record;
    nothing is re-raised because there's no caller to receive it.
    """
    jobs.mark_running(job_id)
    try:
        result = client.ask(prompt, session_id=session_id, toolsets=toolsets)
    except HermesError as exc:
        logger.info("async job %s failed: %s", job_id, exc)
        jobs.mark_failed(job_id, str(exc))
    except Exception as exc:
        # Final boundary for the worker thread — nothing else will catch this.
        logger.exception("async job %s crashed", job_id)
        jobs.mark_failed(job_id, f"unexpected error: {type(exc).__name__}")
    else:
        jobs.mark_completed(job_id, result)


def build_app(
    config: Config,
    client: HermesClient,
    jobs: JobStore | PersistentJobStore | None = None,
) -> FastMCP:
    """Create a FastMCP server with the hermes_ask, hermes_check,
    hermes_cancel, and hermes_reset tools wired up.

    `jobs` is exposed so tests can inject a store with a short TTL or a
    small capacity. Normal operation opens a process-owned persistent SQLite store.
    """
    provider = StaticClientProvider(
        client_id=config.oauth_client_id,
        client_secret=config.oauth_client_secret,
        bearer_token=config.mcp_bearer_token,
        allowed_redirect_schemes=frozenset(config.allowed_redirect_schemes),
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
        asyncio.get_running_loop().set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                max_workers=config.executor_workers, thread_name_prefix="hermes-mcp-io"
            )
        )
        yield {}

    issuer_url = AnyHttpUrl(config.oauth_issuer_url)
    resource_server_url = AnyHttpUrl(f"{config.oauth_issuer_url}/mcp")

    mcp: FastMCP = FastMCP(
        "hermes-mcp",
        host=config.bind_host,
        port=config.bind_port,
        log_level=config.log_level,
        stateless_http=False,
        lifespan=lifespan,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            client_registration_options=ClientRegistrationOptions(enabled=False),
            revocation_options=RevocationOptions(enabled=False),
        ),
        transport_security=_build_transport_security(config),
    )

    job_store = jobs if jobs is not None else PersistentJobStore(config.job_store_path)

    @mcp.tool(description=_TOOL_DESCRIPTION)
    async def hermes_ask(
        prompt: str,
        session_id: str | None = None,
        toolsets: list[str] | None = None,
        async_mode: bool = False,
    ) -> str:
        # HermesError propagates; FastMCP wraps any Exception in ToolError.
        if not async_mode:
            return await client.ask_async(prompt, session_id=session_id, toolsets=toolsets)

        job = await asyncio.to_thread(
            job_store.create, prompt_chars=len(prompt), session_id=session_id
        )
        logger.info(
            "async job %s queued (prompt_chars=%d session=%s)",
            job.job_id,
            len(prompt),
            "y" if session_id else "n",
        )
        thread = threading.Thread(
            target=_run_job,
            args=(client, job_store, job.job_id, prompt, session_id, toolsets),
            name=f"hermes-job-{job.job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return json.dumps(
            {
                "job_id": job.job_id,
                "status": "pending",
                "completion_scope": COMPLETION_SCOPE,
            }
        )

    @mcp.tool(description=_CHECK_TOOL_DESCRIPTION)
    async def hermes_check(job_id: str) -> str:
        job = await asyncio.to_thread(job_store.get, job_id)
        if job is None:
            return json.dumps(
                {
                    "job_id": job_id,
                    "status": "unknown",
                    "completion_scope": COMPLETION_SCOPE,
                }
            )
        return json.dumps(job.to_dict())

    @mcp.tool(description=_RESET_TOOL_DESCRIPTION)
    async def hermes_reset() -> str:
        cleared, by_status = await asyncio.to_thread(job_store.reset_all)
        return json.dumps({"cleared": cleared, "by_status": by_status})

    @mcp.tool(description=_CANCEL_TOOL_DESCRIPTION)
    async def hermes_cancel(job_id: str) -> str:
        # Best-effort mark; the worker thread may already have finished.
        changed = await asyncio.to_thread(job_store.mark_cancelled, job_id)
        job = await asyncio.to_thread(job_store.get, job_id)
        if job is None:
            # Unknown id — never issued by this server (reap-between-calls
            # is impossible: mark_cancelled would have just set finished_at,
            # and the reap window is 24h, so a freshly-cancelled job cannot
            # vanish here).
            return json.dumps(
                {
                    "job_id": job_id,
                    "status": "unknown",
                    "completion_scope": COMPLETION_SCOPE,
                }
            )
        if changed:
            logger.info("async job %s cancelled by caller", job_id)
        return json.dumps(job.to_dict())

    return mcp


def serve(config: Config, client: HermesClient) -> None:
    mcp = build_app(config, client)
    logger.info(
        "hermes-mcp listening on %s:%d (transport=streamable-http, oauth issuer=%s)",
        config.bind_host,
        config.bind_port,
        config.oauth_issuer_url,
    )
    uvicorn.run(
        mcp.streamable_http_app(),
        host=config.bind_host,
        port=config.bind_port,
        log_level=_uvicorn_log_level(config.log_level),
    )

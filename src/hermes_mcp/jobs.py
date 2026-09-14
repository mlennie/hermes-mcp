"""In-memory job store for async `hermes_ask` calls.

When a caller invokes `hermes_ask(prompt, async_mode=True)`, the server records
a job, returns its id immediately, and runs the gateway call in a background
thread. The caller polls `hermes_check(job_id)` to retrieve the result, or
calls `hermes_cancel(job_id)` to release the result (see warning below).

The production server uses PersistentJobStore (persistent_jobs.py), which
shares this Job record format. This in-memory implementation remains available
for isolated tests and explicit dependency injection. OAuth state is separate.

About "cancellation": Python threads cannot be safely killed mid-IO, so
`mark_cancelled` is a **tombstone**. It updates this server's bookkeeping
so `hermes_check` returns `status: cancelled`, but the worker thread keeps
running and the gateway keeps doing whatever it was doing. Cancel releases
the result, not the work. `mark_completed`/`mark_failed` therefore refuse
to overwrite a terminal status (so a late-finishing worker can't undo a
cancellation).

Time bookkeeping uses `time.time()` (wall clock, epoch seconds) so the
values can be surfaced to callers via `to_dict()`. The TTL reap accepts
the small risk of system-clock jumps in exchange for code simplicity —
this is a personal MCP bridge, not a billing system.

Thread-safety: all reads and writes happen under a single `threading.Lock`.
Mutations are short (dict updates), so contention is negligible.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "completed", "failed", "cancelled", "unknown"]

# An async MCP job ends when the Hermes gateway returns. The returned Hermes
# message may itself be a receipt for work delegated to another worker, queue,
# or service. Exposing that boundary in every job record prevents callers from
# treating a successful bridge response as proof that downstream work finished.
COMPLETION_SCOPE = "gateway_response"

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({"completed", "failed", "cancelled"})

DEFAULT_TTL_SECONDS = 24 * 60 * 60
MAX_JOBS = 1000


@dataclass
class Job:
    """A single async hermes_ask call."""

    job_id: str
    created_at: float
    status: JobStatus = "pending"
    result: str | None = None
    error: str | None = None
    finished_at: float | None = None
    prompt_chars: int = 0
    session_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serializable shape returned to the MCP client.

        Always includes `job_id`, `status`, `completion_scope`, `created_at`,
        `prompt_chars`.
        Includes `session_id` only when supplied by the caller, `finished_at`
        only when terminal, and `result`/`error` only when applicable.
        """
        d: dict[str, object] = {
            "job_id": self.job_id,
            "status": self.status,
            "completion_scope": COMPLETION_SCOPE,
            "created_at": self.created_at,
            "prompt_chars": self.prompt_chars,
        }
        if self.session_id is not None:
            d["session_id"] = self.session_id
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclass
class JobStore:
    """Thread-safe in-memory store of `Job` records.

    `ttl_seconds` controls how long terminal jobs stay queryable; expired
    entries are reaped lazily on the next `get`/`create` call (no background
    sweeper, no extra thread to manage).
    """

    ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_jobs: int = MAX_JOBS
    _jobs: dict[str, Job] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self, prompt_chars: int = 0, session_id: str | None = None) -> Job:
        """Allocate a new pending job and return it."""
        now = time.time()
        with self._lock:
            self._reap_locked(now)
            if len(self._jobs) >= self.max_jobs:
                # Refuse rather than silently evicting in-flight work.
                raise RuntimeError(
                    f"job store at capacity ({self.max_jobs}); "
                    "wait for jobs to complete or restart the server"
                )
            job = Job(
                job_id=uuid.uuid4().hex,
                created_at=now,
                prompt_chars=prompt_chars,
                session_id=session_id,
            )
            self._jobs[job.job_id] = job
            return job

    def get(self, job_id: str) -> Job | None:
        """Look up a job by id. Returns `None` if unknown or already reaped."""
        now = time.time()
        with self._lock:
            self._reap_locked(now)
            return self._jobs.get(job_id)

    def mark_running(self, job_id: str) -> None:
        """Transition pending -> running. Refuses to rewind from terminal states."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.status == "pending":
                job.status = "running"

    def mark_completed(self, job_id: str, result: str) -> bool:
        """Stash a successful result. No-op if the job is already terminal
        (e.g. cancelled). Returns True if the state changed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return False
            job.status = "completed"
            job.result = result
            job.finished_at = time.time()
            return True

    def mark_failed(self, job_id: str, error: str) -> bool:
        """Stash a failure. No-op if the job is already terminal. Returns
        True if the state changed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return False
            job.status = "failed"
            job.error = error
            job.finished_at = time.time()
            return True

    def mark_cancelled(self, job_id: str) -> bool:
        """Mark a job as cancelled (tombstone). No-op if unknown or already
        terminal. Returns True if the state changed.

        Important: does NOT stop the worker thread or the underlying gateway
        call. See module docstring.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return False
            job.status = "cancelled"
            job.finished_at = time.time()
            return True

    def reset_all(self) -> tuple[int, dict[JobStatus, int]]:
        """Drop every job from the store. Returns `(cleared, by_status)`.

        Expired terminal jobs are reaped first (matching `create`/`get`) so
        the returned counts reflect only jobs that were actually live in the
        store when the caller asked — not zombies waiting for the next
        lazy-reap pass.

        Like `mark_cancelled`, this does NOT stop any worker threads or the
        underlying gateway calls — Python cannot safely kill a thread mid-I/O.
        Workers whose jobs are wiped will run to completion and then no-op
        when their `mark_completed`/`mark_failed` call finds an unknown id.

        Intended for clearing a stuck or cluttered queue when the operator
        wants a clean slate without restarting the server process.
        """
        now = time.time()
        with self._lock:
            self._reap_locked(now)
            by_status: dict[JobStatus, int] = {}
            for job in self._jobs.values():
                by_status[job.status] = by_status.get(job.status, 0) + 1
            cleared = len(self._jobs)
            self._jobs.clear()
        if cleared:
            logger.info("job store reset: cleared %d job(s) by_status=%s", cleared, by_status)
        return cleared, by_status

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)

    def _reap_locked(self, now: float) -> None:
        """Drop terminal jobs older than `ttl_seconds`. Caller must hold the lock."""
        cutoff = now - self.ttl_seconds
        stale = [
            jid
            for jid, job in self._jobs.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]
        for jid in stale:
            del self._jobs[jid]
        if stale:
            logger.debug("job store reaped %d expired job(s)", len(stale))

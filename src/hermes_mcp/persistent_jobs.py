"""Single-process durable jobs. Call from worker threads, never the event loop.

The owner lock lasts for the store's lifetime; the serving process owns it until
exit so daemon workers can finish recording results during server shutdown.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import cast

from .jobs import DEFAULT_TTL_SECONDS, MAX_JOBS, Job, JobStatus

INTERRUPTED_ERROR = (
    "MCP bridge restarted before recording the gateway outcome. Work may have completed; "
    "consult the original session and artifacts. Do not automatically resubmit."
)


class PersistentJobStore:
    """SQLite-backed equivalent of JobStore, with atomic terminal transitions.

    A single-owner lock rejects multiple server processes sharing a database.
    Completed results survive restarts for the existing 24-hour retention period.
    Unfinished requests become failed/unconfirmed, never automatically replayed.
    """

    def __init__(
        self, path: str | Path, ttl_seconds: int = DEFAULT_TTL_SECONDS, max_jobs: int = MAX_JOBS
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_jobs = max_jobs
        self._lock = threading.Lock()
        self._closed = False
        db_path = Path(path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._owner = open(str(db_path) + ".lock", "a")
        os.chmod(str(db_path) + ".lock", 0o600)
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._owner.close()
            raise RuntimeError("Another MCP process owns this job store; keep one worker") from None
        connection = None
        try:
            fd = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(db_path, 0o600)
            connection = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
            self._db = connection
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            with self._db:
                self._db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                    status TEXT NOT NULL, result TEXT, error TEXT, finished_at REAL,
                    prompt_chars INTEGER NOT NULL, session_id TEXT)""")
                self._db.execute(
                    """UPDATE jobs SET status='failed', error=?, finished_at=?
                    WHERE status IN ('pending','running')""",
                    (INTERRUPTED_ERROR, time.time()),
                )
        except BaseException:
            if connection is not None:
                connection.close()
            self._owner.close()
            raise

    def _reap(self) -> None:
        self._db.execute(
            "DELETE FROM jobs WHERE finished_at < ?", (time.time() - self.ttl_seconds,)
        )

    def create(self, prompt_chars: int = 0, session_id: str | None = None) -> Job:
        job = Job(uuid.uuid4().hex, time.time(), prompt_chars=prompt_chars, session_id=session_id)
        with self._lock, self._db:
            self._reap()
            if self._db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= self.max_jobs:
                raise RuntimeError(
                    f"job store at capacity ({self.max_jobs}); wait for terminal jobs to expire"
                )
            self._db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                (
                    job.job_id,
                    job.created_at,
                    job.status,
                    None,
                    None,
                    None,
                    prompt_chars,
                    session_id,
                ),
            )
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock, self._db:
            self._reap()
            row = self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return Job(**dict(row)) if row else None

    def mark_running(self, job_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET status='running' WHERE job_id=? AND status='pending'", (job_id,)
            )

    def _finish(
        self, job_id: str, status: JobStatus, result: str | None = None, error: str | None = None
    ) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                """UPDATE jobs SET status=?,result=?,error=?,finished_at=?
                WHERE job_id=? AND status IN ('pending','running')""",
                (status, result, error, time.time(), job_id),
            )
            return cur.rowcount == 1

    def mark_completed(self, job_id: str, result: str) -> bool:
        return self._finish(job_id, "completed", result=result)

    def mark_failed(self, job_id: str, error: str) -> bool:
        return self._finish(job_id, "failed", error=error)

    def mark_cancelled(self, job_id: str) -> bool:
        return self._finish(job_id, "cancelled")

    def reset_all(self) -> tuple[int, dict[JobStatus, int]]:
        with self._lock, self._db:
            self._reap()
            counts = {
                cast(JobStatus, row[0]): int(row[1])
                for row in self._db.execute(
                    "SELECT status,COUNT(*) FROM jobs GROUP BY status"
                ).fetchall()
            }
            self._db.execute("DELETE FROM jobs")
            return sum(counts.values()), counts

    def __len__(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def close(self) -> None:
        """Release resources after all users/workers have stopped. Idempotent."""
        with self._lock:
            if not self._closed:
                self._db.close()
                self._owner.close()
                self._closed = True

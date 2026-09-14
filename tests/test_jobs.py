from __future__ import annotations

import time

import pytest

from hermes_mcp.jobs import Job, JobStore


def test_create_returns_pending_job_with_unique_id() -> None:
    store = JobStore()
    a = store.create(prompt_chars=10, session_id="sess-1")
    b = store.create()
    assert a.status == "pending"
    assert a.prompt_chars == 10
    assert a.session_id == "sess-1"
    assert a.result is None
    assert a.error is None
    assert a.created_at > 0
    assert a.finished_at is None
    assert a.job_id != b.job_id
    assert len(store) == 2


def test_get_unknown_returns_none() -> None:
    assert JobStore().get("does-not-exist") is None


def test_mark_running_transitions_pending_to_running() -> None:
    store = JobStore()
    job = store.create()
    store.mark_running(job.job_id)
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "running"


def test_mark_running_does_not_overwrite_terminal_state() -> None:
    """If the worker completes before mark_running fires (improbable but
    racy), the completed state must survive — no rewinding to 'running'."""
    store = JobStore()
    job = store.create()
    store.mark_completed(job.job_id, "done")
    store.mark_running(job.job_id)
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "completed"


def test_mark_completed_stores_result_and_finishes() -> None:
    store = JobStore()
    job = store.create()
    assert store.mark_completed(job.job_id, "the result") is True
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "completed"
    assert after.result == "the result"
    assert after.finished_at is not None


def test_mark_failed_stores_error_and_finishes() -> None:
    store = JobStore()
    job = store.create()
    assert store.mark_failed(job.job_id, "boom") is True
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "failed"
    assert after.error == "boom"
    assert after.finished_at is not None


def test_mark_unknown_id_is_noop() -> None:
    """Marking an unknown id must not raise — the worker thread cannot know
    if its job was reaped between create and finish."""
    store = JobStore()
    assert store.mark_completed("nope", "x") is False
    assert store.mark_failed("nope", "x") is False
    assert store.mark_cancelled("nope") is False
    store.mark_running("nope")  # void return, just shouldn't raise


# --- terminal-state guards (a late worker must not clobber a cancellation) ---


def test_mark_completed_refuses_to_overwrite_cancelled() -> None:
    """The headline cancellation guarantee: once the user cancels, a
    late-finishing worker thread cannot resurrect the result."""
    store = JobStore()
    job = store.create()
    assert store.mark_cancelled(job.job_id) is True
    assert store.mark_completed(job.job_id, "ghost result") is False
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "cancelled"
    assert after.result is None


def test_mark_failed_refuses_to_overwrite_cancelled() -> None:
    store = JobStore()
    job = store.create()
    store.mark_cancelled(job.job_id)
    assert store.mark_failed(job.job_id, "ghost error") is False
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "cancelled"
    assert after.error is None


def test_mark_completed_refuses_to_overwrite_failed() -> None:
    store = JobStore()
    job = store.create()
    store.mark_failed(job.job_id, "first failure")
    assert store.mark_completed(job.job_id, "would-be result") is False
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "failed"


def test_mark_cancelled_refuses_to_overwrite_completed() -> None:
    store = JobStore()
    job = store.create()
    store.mark_completed(job.job_id, "done")
    assert store.mark_cancelled(job.job_id) is False
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "completed"


def test_mark_cancelled_refuses_to_overwrite_failed() -> None:
    store = JobStore()
    job = store.create()
    store.mark_failed(job.job_id, "boom")
    assert store.mark_cancelled(job.job_id) is False
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "failed"


def test_mark_cancelled_is_idempotent() -> None:
    store = JobStore()
    job = store.create()
    assert store.mark_cancelled(job.job_id) is True
    assert store.mark_cancelled(job.job_id) is False  # already cancelled
    after = store.get(job.job_id)
    assert after is not None
    assert after.status == "cancelled"


def test_reset_all_clears_every_job_and_reports_counts() -> None:
    store = JobStore()
    pending = store.create()
    running = store.create()
    store.mark_running(running.job_id)
    done = store.create()
    store.mark_completed(done.job_id, "ok")
    boom = store.create()
    store.mark_failed(boom.job_id, "boom")
    cancelled = store.create()
    store.mark_cancelled(cancelled.job_id)

    cleared, by_status = store.reset_all()
    assert cleared == 5
    assert by_status == {
        "pending": 1,
        "running": 1,
        "completed": 1,
        "failed": 1,
        "cancelled": 1,
    }
    assert len(store) == 0
    # All ids must now be unknown.
    for job in (pending, running, done, boom, cancelled):
        assert store.get(job.job_id) is None


def test_reset_all_on_empty_store_returns_empty_counts() -> None:
    assert JobStore().reset_all() == (0, {})


def test_reset_all_does_not_break_worker_finishing_late() -> None:
    """A worker thread whose job was wiped must safely no-op on mark_completed,
    matching the existing 'mark_unknown_id_is_noop' invariant."""
    store = JobStore()
    job = store.create()
    store.mark_running(job.job_id)
    store.reset_all()
    # Worker still believes the job exists; these are the calls it would make.
    assert store.mark_completed(job.job_id, "late result") is False
    assert store.mark_failed(job.job_id, "late error") is False


def test_reset_all_reaps_expired_terminal_jobs_before_counting() -> None:
    """Expired terminal jobs should be reaped first, not counted toward the
    reset summary — keeps the count honest about what was actually live."""
    store = JobStore(ttl_seconds=10)
    stale = store.create()
    store.mark_completed(stale.job_id, "ancient")
    store._jobs[stale.job_id].finished_at = time.time() - 3600  # 1h ago

    fresh = store.create()  # still pending, never finishes -> never reaped

    cleared, by_status = store.reset_all()
    assert cleared == 1
    assert by_status == {"pending": 1}
    assert len(store) == 0
    # The fresh id is gone too (full wipe), but the stale one was reaped, not
    # counted as "completed".
    assert store.get(fresh.job_id) is None


def test_mark_cancelled_works_on_pending_and_running() -> None:
    store = JobStore()
    pending = store.create()
    running = store.create()
    store.mark_running(running.job_id)
    assert store.mark_cancelled(pending.job_id) is True
    assert store.mark_cancelled(running.job_id) is True
    assert store.get(pending.job_id) is not None
    assert store.get(running.job_id) is not None
    assert store.get(pending.job_id).status == "cancelled"  # type: ignore[union-attr]
    assert store.get(running.job_id).status == "cancelled"  # type: ignore[union-attr]


# --- TTL + capacity ----------------------------------------------------------


def test_ttl_reaps_terminal_jobs() -> None:
    """A completed job older than `ttl_seconds` is dropped on the next access."""
    store = JobStore(ttl_seconds=10)
    job = store.create()
    # Pin finished_at into the past so we don't have to sleep in tests.
    store._jobs[job.job_id].status = "completed"
    store._jobs[job.job_id].finished_at = time.time() - 3600  # 1h ago
    # Force a reap by triggering get() — lazy cleanup runs first.
    assert store.get(job.job_id) is None
    assert len(store) == 0


def test_ttl_reaps_cancelled_jobs() -> None:
    """Cancelled jobs are terminal and should be reaped on the same schedule
    as completed/failed."""
    store = JobStore(ttl_seconds=10)
    job = store.create()
    store.mark_cancelled(job.job_id)
    store._jobs[job.job_id].finished_at = time.time() - 3600
    assert store.get(job.job_id) is None


def test_ttl_does_not_reap_in_flight_jobs() -> None:
    """A still-running job has finished_at=None and must never be reaped,
    no matter how long it's been running."""
    store = JobStore(ttl_seconds=1)
    job = store.create()
    # Backdate created_at — in-flight jobs don't have finished_at set yet.
    store._jobs[job.job_id].created_at = time.time() - 999_999
    assert store.get(job.job_id) is not None


def test_capacity_limit_refuses_new_creates() -> None:
    store = JobStore(max_jobs=2)
    store.create()
    store.create()
    with pytest.raises(RuntimeError, match="capacity"):
        store.create()


# --- to_dict shape -----------------------------------------------------------


def test_to_dict_always_includes_required_fields() -> None:
    """Pending job: always-present fields are job_id, status, created_at,
    prompt_chars. session_id only when supplied."""
    job = Job(job_id="abc", created_at=1234.5)
    d = job.to_dict()
    assert d == {
        "job_id": "abc",
        "status": "pending",
        "completion_scope": "gateway_response",
        "created_at": 1234.5,
        "prompt_chars": 0,
    }


def test_to_dict_includes_session_id_when_set() -> None:
    job = Job(job_id="abc", created_at=1.0, session_id="sess", prompt_chars=42)
    d = job.to_dict()
    assert d["session_id"] == "sess"
    assert d["prompt_chars"] == 42


def test_to_dict_includes_finished_at_only_when_terminal() -> None:
    store = JobStore()
    job = store.create()
    assert "finished_at" not in job.to_dict()
    store.mark_completed(job.job_id, "ok")
    after = store.get(job.job_id)
    assert after is not None
    assert "finished_at" in after.to_dict()


def test_to_dict_includes_result_or_error_appropriately() -> None:
    job = Job(job_id="abc", created_at=0.0)
    job.status = "completed"
    job.result = "hi"
    assert job.to_dict()["result"] == "hi"
    job.status = "failed"
    job.result = None
    job.error = "boom"
    assert job.to_dict()["error"] == "boom"
    assert "result" not in job.to_dict()


def test_to_dict_cancelled_has_no_result_or_error() -> None:
    """Cancellation is a 'release', not a failure — no error field."""
    store = JobStore()
    job = store.create()
    store.mark_cancelled(job.job_id)
    d = store.get(job.job_id).to_dict()  # type: ignore[union-attr]
    assert d["status"] == "cancelled"
    assert "result" not in d
    assert "error" not in d
    assert "finished_at" in d

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hermes_mcp.config import Config
from hermes_mcp.hermes_client import HermesClient
from hermes_mcp.jobs import JobStore
from hermes_mcp.server import build_app


class SlowGateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        # An actual detached OS subprocess, no model call or product access.
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(2)"], start_new_session=True, check=True
        )
        body = json.dumps({"choices": [{"message": {"content": "finished"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Concurrency(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_answers_during_detached_subprocess(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowGateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = Config.from_env(
            {
                "OAUTH_CLIENT_ID": "test",
                "OAUTH_CLIENT_SECRET": "x" * 32,
                "OAUTH_ISSUER_URL": "https://example.com",
                "HERMES_API_KEY": "fixture",
            }
        )
        client = HermesClient(
            "http://127.0.0.1:" + str(server.server_port), "fixture", "fixture", 5
        )
        mcp = build_app(config, client, jobs=JobStore())
        ask = mcp._tool_manager.get_tool("hermes_ask")
        check = mcp._tool_manager.get_tool("hermes_check")
        t0 = time.monotonic()
        work = asyncio.create_task(ask.run({"prompt": "synthetic"}))
        try:
            await asyncio.sleep(0.15)
            delay = time.monotonic() - t0
            self.assertLess(delay, 0.75, f"event loop blocked {delay:.3f}s")
            self.assertFalse(work.done(), "slow request must still be running")
            for _ in range(3):
                start = time.monotonic()
                result = await check.run({"job_id": "nonexistent-test-id"})
                latency = time.monotonic() - start
                self.assertEqual(json.loads(result)["status"], "unknown")
                self.assertLess(latency, 0.5)
                print(f"MCP_POLL_LATENCY_S={latency:.4f}", flush=True)
            self.assertEqual(await work, "finished")
        finally:
            await work
            await asyncio.to_thread(server.shutdown)
            server.server_close()


class Persistence(unittest.TestCase):
    def test_restart_retains_results_and_marks_unconfirmed_work(self):
        from hermes_mcp.persistent_jobs import PersistentJobStore

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "jobs.sqlite3")
            # A separate interpreter commits records then terminates.
            script = """
import sys,json
from hermes_mcp.persistent_jobs import PersistentJobStore
s=PersistentJobStore(sys.argv[1])
a=s.create(session_id='original-session'); s.mark_completed(a.job_id,'exact completed result')
b=s.create(); s.mark_running(b.job_id)
c=s.create(); s.mark_cancelled(c.job_id)
print(json.dumps([a.job_id,b.job_id,c.job_id]))
"""
            ids = json.loads(
                subprocess.check_output([sys.executable, "-c", script, path], text=True)
            )
            store = PersistentJobStore(path)
            try:
                self.assertEqual(store.get(ids[0]).result, "exact completed result")
                self.assertEqual(store.get(ids[0]).session_id, "original-session")
                self.assertEqual(store.get(ids[1]).status, "failed")
                self.assertIn("Do not automatically resubmit", store.get(ids[1]).error)
                self.assertEqual(store.get(ids[2]).status, "cancelled")
                self.assertFalse(store.mark_completed(ids[2], "late"))
                self.assertIsNone(store.get("never-created"))
                with self.assertRaises(RuntimeError):
                    PersistentJobStore(path)
                self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            finally:
                store.close()

    def test_concurrent_transitions_capacity_ttl_and_reset(self):
        from hermes_mcp.persistent_jobs import PersistentJobStore

        with tempfile.TemporaryDirectory() as tmp:
            store = PersistentJobStore(Path(tmp) / "jobs.sqlite3", max_jobs=2, ttl_seconds=10)
            a = store.create()
            b = store.create()
            with self.assertRaises(RuntimeError):
                store.create()
            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(
                    pool.map(lambda i: store.mark_completed(a.job_id, str(i)), range(30))
                )
            self.assertEqual(sum(outcomes), 1)
            store.mark_cancelled(b.job_id)
            with store._lock, store._db:
                store._db.execute(
                    "UPDATE jobs SET finished_at=? WHERE job_id=?", (time.time() - 20, a.job_id)
                )
            self.assertIsNone(store.get(a.job_id))
            self.assertEqual(store.reset_all(), (1, {"cancelled": 1}))
            self.assertFalse(store.mark_completed(b.job_id, "late"))
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_failed_initialization_releases_owner_lock(tmp_path, monkeypatch):
    import sqlite3

    import pytest

    from hermes_mcp.persistent_jobs import PersistentJobStore

    original = sqlite3.connect

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("fixture")

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(sqlite3.OperationalError):
        PersistentJobStore(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(sqlite3, "connect", original)
    store = PersistentJobStore(tmp_path / "jobs.sqlite3")
    store.close()
    store.close()


async def test_default_store_and_executor_wired(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from hermes_mcp.persistent_jobs import PersistentJobStore

    store = PersistentJobStore(tmp_path / "jobs.sqlite3")
    opened = []

    def factory(path):
        opened.append(path)
        return store

    monkeypatch.setattr("hermes_mcp.server.PersistentJobStore", factory)
    config = Config.from_env(
        {
            "OAUTH_CLIENT_ID": "test",
            "OAUTH_CLIENT_SECRET": "x" * 32,
            "OAUTH_ISSUER_URL": "https://example.com",
            "HERMES_API_KEY": "fixture",
            "HERMES_MCP_JOB_STORE_PATH": str(tmp_path / "jobs.sqlite3"),
            "HERMES_MCP_EXECUTOR_WORKERS": "12",
        }
    )
    mcp = build_app(config, MagicMock())
    try:
        assert opened == [str(tmp_path / "jobs.sqlite3")]
        async with mcp._mcp_server.lifespan(mcp._mcp_server):
            assert asyncio.get_running_loop()._default_executor._max_workers == 12
            job = store.create(session_id="original")
            store.mark_completed(job.job_id, "saved result")
            check = mcp._tool_manager.get_tool("hermes_check")
            result = json.loads(await check.run({"job_id": job.job_id}))
            assert result["result"] == "saved result"
            assert result["completion_scope"] == "gateway_response"
    finally:
        store.close()

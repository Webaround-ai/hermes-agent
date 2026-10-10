"""Request-scoped context must reach synchronous API-run workers without leaking."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest

from gateway.platforms import api_server_runs


@pytest.mark.asyncio
async def test_api_run_worker_receives_isolated_request_context():
    request_origin = ContextVar("test_api_run_request_origin", default=None)
    loop = asyncio.get_running_loop()
    # Reuse one worker thread so the final assertion also checks that a prior
    # request's context was restored before the next submission.
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    baseline = api_server_runs.api_worker_live_count()

    async def submit_for(origin):
        token = request_origin.set(origin)
        try:
            observed = await api_server_runs._submit_api_worker(loop, request_origin.get)
            assert request_origin.get() == origin
            return observed
        finally:
            request_origin.reset(token)

    try:
        assert await asyncio.gather(
            submit_for("owner-a:conversation-41"),
            submit_for("owner-b:conversation-92"),
        ) == ["owner-a:conversation-41", "owner-b:conversation-92"]
        # A later request with no origin must not inherit the preceding worker's
        # ContextVar value from the reused executor thread.
        assert await api_server_runs._submit_api_worker(loop, request_origin.get) is None
        assert api_server_runs.api_worker_live_count() == baseline
    finally:
        # Await executor shutdown so the test leaves no worker alive.
        await loop.shutdown_default_executor()

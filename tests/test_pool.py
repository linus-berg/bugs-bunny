"""Worker pool behaviour: isolation, timeouts, crashes, recycling, back-pressure."""

import asyncio
import os
import time

import pytest

from app.errors import ServiceError
from app.pool import WorkerPool
from tests import worker_tasks as tasks


def run(coro):
    return asyncio.run(coro)


async def started(size=1, **kwargs) -> WorkerPool:
    pool = WorkerPool(size, initializer=tasks.init, init_kwargs={"tag": "hello"}, **kwargs)
    await pool.start()
    return pool


async def wait_for_idle(pool: WorkerPool, count: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while pool.stats["idle"] < count:
        assert time.monotonic() < deadline, "replacement worker never became ready"
        await asyncio.sleep(0.05)


def test_runs_tasks_in_separate_processes_and_reports_capabilities():
    async def scenario():
        pool = await started()
        try:
            assert pool.capabilities["tag"] == "hello"
            assert await pool.run(tasks.echo, timeout=10, value={"a": [1, 2]}) == {"a": [1, 2]}
            worker_pid = await pool.run(tasks.pid, timeout=10)
            assert worker_pid != os.getpid()
            assert worker_pid == pool.capabilities["pid"]
        finally:
            await pool.close()

    run(scenario())


def test_service_errors_propagate_and_keep_the_worker():
    async def scenario():
        pool = await started()
        try:
            before = await pool.run(tasks.pid, timeout=10)
            with pytest.raises(ServiceError) as info:
                await pool.run(tasks.reject, timeout=10)
            assert (info.value.status_code, info.value.code, info.value.headers) == (422, "rejected", {"X-Test": "1"})
            assert await pool.run(tasks.pid, timeout=10) == before
        finally:
            await pool.close()

    run(scenario())


@pytest.mark.parametrize("task", [tasks.fail, tasks.unpicklable])
def test_unexpected_errors_become_generic_500s(task):
    async def scenario():
        pool = await started()
        try:
            with pytest.raises(ServiceError) as info:
                await pool.run(task, timeout=10)
            assert (info.value.status_code, info.value.code) == (500, "processing_failed")
            assert "boom" not in info.value.detail  # internals are logged, not leaked
            assert await pool.run(tasks.echo, timeout=10, value=1) == 1
        finally:
            await pool.close()

    run(scenario())


def test_timeout_kills_only_the_stuck_worker():
    async def scenario():
        pool = await started(size=2)
        try:
            started_at = time.monotonic()
            stuck = asyncio.create_task(pool.run(tasks.sleep, timeout=0.5, seconds=60))
            healthy = asyncio.create_task(pool.run(tasks.sleep, timeout=10, seconds=1))
            with pytest.raises(ServiceError) as info:
                await stuck
            assert (info.value.status_code, info.value.code) == (504, "processing_timeout")
            assert time.monotonic() - started_at < 5
            # The other request, running concurrently in the other worker, is unaffected.
            assert await healthy
            await wait_for_idle(pool, 2)
            assert await pool.run(tasks.echo, timeout=10, value="ok") == "ok"
        finally:
            await pool.close()

    run(scenario())


def test_crashed_worker_fails_its_request_and_is_replaced():
    async def scenario():
        pool = await started()
        try:
            crashed_pid = await pool.run(tasks.pid, timeout=10)
            with pytest.raises(ServiceError) as info:
                await pool.run(tasks.crash, timeout=10)
            assert (info.value.status_code, info.value.code) == (500, "processing_failed")
            # The next request waits for the replacement instead of failing.
            new_pid = await pool.run(tasks.pid, timeout=10)
            assert new_pid != crashed_pid
        finally:
            await pool.close()

    run(scenario())


def test_worker_that_died_while_idle_is_not_blamed_on_the_next_request():
    async def scenario():
        pool = await started()
        try:
            victim = await pool.run(tasks.pid, timeout=10)
            os.kill(victim, 9)
            await asyncio.sleep(0.2)
            assert await pool.run(tasks.pid, timeout=10) not in (victim, None)
        finally:
            await pool.close()

    run(scenario())


def test_workers_are_recycled_after_max_tasks():
    async def scenario():
        pool = await started(max_tasks_per_worker=2)
        try:
            first = [await pool.run(tasks.pid, timeout=10) for _ in range(2)]
            assert first[0] == first[1]
            assert await pool.run(tasks.pid, timeout=10) != first[0]
        finally:
            await pool.close()

    run(scenario())


def test_workers_run_in_parallel():
    async def scenario():
        pool = await started(size=3)
        try:
            began = time.monotonic()
            pids = await asyncio.gather(*(pool.run(tasks.sleep, timeout=10, seconds=1) for _ in range(3)))
            assert time.monotonic() - began < 2.5
            assert len(set(pids)) == 3
        finally:
            await pool.close()

    run(scenario())


def test_full_queue_is_rejected_with_retry_after():
    async def scenario():
        pool = await started(max_queue=1)
        try:
            busy = asyncio.create_task(pool.run(tasks.sleep, timeout=10, seconds=1))
            await asyncio.sleep(0.1)
            queued = asyncio.create_task(pool.run(tasks.echo, timeout=10, value="queued"))
            await asyncio.sleep(0.1)
            assert pool.stats == {"size": 1, "idle": 0, "queued": 1}
            with pytest.raises(ServiceError) as info:
                await pool.run(tasks.echo, timeout=10, value="rejected")
            assert (info.value.status_code, info.value.code) == (503, "server_busy")
            assert info.value.headers == {"Retry-After": "5"}
            assert await busy
            assert await queued == "queued"
        finally:
            await pool.close()

    run(scenario())


def test_cancelled_request_does_not_hand_a_busy_worker_to_the_next_one():
    async def scenario():
        pool = await started()
        try:
            task = asyncio.create_task(pool.run(tasks.sleep, timeout=10, seconds=2))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Had the busy worker been reused, this would receive the sleep's result.
            assert await pool.run(tasks.echo, timeout=10, value="fresh") == "fresh"
        finally:
            await pool.close()

    run(scenario())


def test_failing_initializer_fails_start_and_leaves_no_processes():
    async def scenario():
        pool = WorkerPool(2, initializer=tasks.failing_init)
        with pytest.raises(RuntimeError, match="Could not start worker processes"):
            await pool.start()
        assert pool._workers == set()

    run(scenario())


def test_closed_pool_rejects_work():
    async def scenario():
        pool = await started()
        await pool.close()
        with pytest.raises(ServiceError) as info:
            await pool.run(tasks.echo, timeout=10, value=1)
        assert info.value.status_code == 503

    run(scenario())

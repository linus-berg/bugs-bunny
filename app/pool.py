"""A small asyncio-facing pool of worker processes.

Why not ``concurrent.futures.ProcessPoolExecutor``? It cannot cancel a task
that is already running, and a single crashed worker breaks the whole pool,
failing every in-flight request. Here each worker owns a private pipe, so:

* a task that exceeds its timeout kills only its own worker;
* a crashed worker (e.g. MuPDF segfaulting on a malformed file) fails only
  its own request and is replaced in the background;
* workers are recycled after N tasks to bound memory growth;
* the number of requests waiting for a worker is bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import signal
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.connection import Connection
from typing import Any

from app.errors import ServiceError

log = logging.getLogger(__name__)

START_TIMEOUT = 120.0
"""Seconds a new worker may take to import libraries and load the layout models."""


class WorkerCrashed(Exception):
    pass


class WorkerTimeout(Exception):
    pass


def _worker_main(conn: Connection, initializer: Callable[..., dict] | None, init_kwargs: dict) -> None:
    # Ctrl-C reaches the whole process group; let the parent shut us down.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     [worker %(process)d] %(name)s: %(message)s")
    try:
        info = initializer(**init_kwargs) if initializer else {}
    except BaseException as e:
        log.exception("Worker initialisation failed")
        conn.send(("init_error", f"{type(e).__name__}: {e}"))
        return
    conn.send(("ready", info))

    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return  # parent is gone
        if message is None:
            return
        fn, kwargs = message
        try:
            reply: tuple[str, Any] = ("ok", fn(**kwargs))
        except ServiceError as e:
            reply = ("error", e)
        except Exception:
            log.exception("Unhandled error in %s", getattr(fn, "__qualname__", fn))
            reply = ("error", ServiceError(500, "processing_failed", "The document could not be processed."))
        try:
            conn.send(reply)
        except (BrokenPipeError, EOFError, OSError):
            return
        except Exception:  # pickling failed; nothing was written yet
            log.exception("Could not send result of %s", getattr(fn, "__qualname__", fn))
            conn.send(("error", ServiceError(500, "processing_failed", "The document could not be processed.")))


class _Worker:
    """Parent-side handle of one worker process. Methods block; call them from threads."""

    def __init__(self, ctx, initializer, init_kwargs) -> None:
        self._conn, child_conn = ctx.Pipe()
        self.process = ctx.Process(
            target=_worker_main,
            args=(child_conn, initializer, init_kwargs),
            name="extraction-worker",
            daemon=True,
        )
        self.process.start()
        child_conn.close()
        self.tasks = 0
        self.info: dict = {}

    @property
    def pid(self) -> int | None:
        return self.process.pid

    def wait_ready(self, timeout: float) -> None:
        if not self._conn.poll(timeout):
            raise WorkerTimeout(f"worker did not start within {timeout:g}s")
        try:
            status, payload = self._conn.recv()
        except (EOFError, OSError):
            raise WorkerCrashed(f"worker exited during start-up (exit code {self.process.exitcode})") from None
        if status != "ready":
            raise RuntimeError(f"worker failed to start: {payload}")
        self.info = payload

    def call(self, fn: Callable, kwargs: dict, timeout: float) -> Any:
        try:
            self._conn.send((fn, kwargs))
            if not self._conn.poll(timeout):
                raise WorkerTimeout
            status, payload = self._conn.recv()
        except (EOFError, OSError):  # includes BrokenPipeError
            raise WorkerCrashed from None
        if status == "ok":
            return payload
        raise payload

    def stop(self, grace: float = 5.0) -> None:
        with contextlib.suppress(OSError):
            self._conn.send(None)
        self.process.join(grace)
        self.kill()

    def kill(self) -> None:
        if self.process.is_alive():
            self.process.kill()
        self.process.join()
        # Don't close the connection here: a thread may still be blocked in
        # call(). It wakes up with EOF now that the process is gone, and the
        # connection is closed when the handle is garbage collected.


class WorkerPool:
    def __init__(
        self,
        size: int,
        *,
        initializer: Callable[..., dict] | None = None,
        init_kwargs: dict | None = None,
        max_tasks_per_worker: int = 0,
        max_queue: int | None = None,
        start_method: str = "spawn",
    ) -> None:
        if size < 1:
            raise ValueError("size must be at least 1")
        self.size = size
        self.max_tasks_per_worker = max_tasks_per_worker
        self.max_queue = max_queue
        self._initializer = initializer
        self._init_kwargs = init_kwargs or {}
        # spawn, not fork: the web process is multi-threaded, and forking a
        # threaded process can deadlock the child.
        self._ctx = multiprocessing.get_context(start_method)
        # Dedicated threads for blocking pipe I/O, so busy workers can never
        # starve the event loop's default executor (or vice versa).
        self._threads = ThreadPoolExecutor(max_workers=2 * size + 2, thread_name_prefix="worker-io")
        self._idle: asyncio.Queue[_Worker] = asyncio.Queue()
        self._workers: set[_Worker] = set()
        self._replacements: set[asyncio.Task] = set()
        self._waiting = 0
        self._closed = False
        self.capabilities: dict = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        results = await asyncio.gather(*(self._spawn() for _ in range(self.size)), return_exceptions=True)
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            await self.close()
            raise RuntimeError("Could not start worker processes") from failures[0]
        self.capabilities = results[0].info
        for worker in results:
            self._idle.put_nowait(worker)
        log.info("Started %d worker process(es)", self.size)

    async def close(self) -> None:
        self._closed = True
        for task in self._replacements:
            task.cancel()
        await asyncio.gather(*self._replacements, return_exceptions=True)
        workers, self._workers = list(self._workers), set()
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(self._threads, w.stop) for w in workers))
        self._threads.shutdown(wait=False, cancel_futures=True)

    # -- running tasks -------------------------------------------------------

    async def run(self, fn: Callable, /, *, timeout: float, **kwargs: Any) -> Any:
        """Run ``fn(**kwargs)`` in a worker process and return its result.

        ``fn`` must be a module-level function (it is pickled by reference).
        """
        worker = await self._acquire()
        loop = asyncio.get_running_loop()
        reusable = False
        try:
            result = await loop.run_in_executor(self._threads, worker.call, fn, kwargs, timeout)
            reusable = True
            return result
        except ServiceError:
            reusable = True  # the worker reported the error itself and is fine
            raise
        except WorkerTimeout:
            log.warning("Worker %s exceeded the %gs timeout; killing it", worker.pid, timeout)
            raise ServiceError(
                504, "processing_timeout", f"Processing took longer than {timeout:g} seconds and was aborted."
            ) from None
        except WorkerCrashed:
            log.error("Worker %s died while processing (exit code %s)", worker.pid, worker.process.exitcode)
            raise ServiceError(500, "processing_failed", "The document could not be processed.") from None
        finally:
            # Also reached on cancellation, where the worker is still busy and
            # must not be handed to another request.
            worker.tasks += 1
            recycle = self.max_tasks_per_worker and worker.tasks >= self.max_tasks_per_worker
            if reusable and not recycle and not self._closed:
                self._idle.put_nowait(worker)
            else:
                self._retire(worker, graceful=reusable)

    async def _acquire(self) -> _Worker:
        if self._closed:
            raise ServiceError(503, "shutting_down", "The server is shutting down.")
        if self.max_queue is not None and self._idle.empty() and self._waiting >= self.max_queue:
            raise ServiceError(
                503,
                "server_busy",
                "All workers are busy and the queue is full. Retry later.",
                headers={"Retry-After": "5"},
            )
        self._waiting += 1
        try:
            while True:
                worker = await self._idle.get()
                if worker.process.is_alive():
                    return worker
                # Died while idle (e.g. the OOM killer); don't blame the next document.
                log.warning("Idle worker %s exited unexpectedly; replacing it", worker.pid)
                self._retire(worker, graceful=False)
        finally:
            self._waiting -= 1

    @property
    def stats(self) -> dict[str, int]:
        return {"size": self.size, "idle": self._idle.qsize(), "queued": self._waiting}

    # -- worker management ---------------------------------------------------

    async def _spawn(self) -> _Worker:
        loop = asyncio.get_running_loop()
        worker = await loop.run_in_executor(self._threads, _Worker, self._ctx, self._initializer, self._init_kwargs)
        self._workers.add(worker)
        try:
            await loop.run_in_executor(self._threads, worker.wait_ready, START_TIMEOUT)
        except BaseException:
            self._workers.discard(worker)
            await loop.run_in_executor(self._threads, worker.kill)
            raise
        return worker

    def _retire(self, worker: _Worker, *, graceful: bool) -> None:
        if self._closed:
            # close() has already stopped everything and shut the executor
            # down; just make sure this one is gone.
            worker.process.kill()
            return
        task = asyncio.get_running_loop().create_task(self._replace(worker, graceful))
        self._replacements.add(task)
        task.add_done_callback(self._replacements.discard)

    async def _replace(self, old: _Worker, graceful: bool) -> None:
        loop = asyncio.get_running_loop()
        self._workers.discard(old)
        await loop.run_in_executor(self._threads, old.stop if graceful else old.kill)
        delay = 1.0
        while not self._closed:
            try:
                new = await self._spawn()
            except Exception:
                log.exception("Could not start a replacement worker; retrying in %gs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            if self._closed:
                await loop.run_in_executor(self._threads, new.kill)
            else:
                self._idle.put_nowait(new)
            return

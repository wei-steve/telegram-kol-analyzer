"""One shared single-worker executor for the blocking management worker ticks.

The strategy management tick and the break-even convergence tick are today
mutually exclusive only because both run directly on the asyncio event loop.
Offloading each loop independently would let them run concurrently for the
first time, which is a real behavior change on shared management batches and
protection state.

This module owns the one executor both loops submit to. ``max_workers=1``
keeps the existing mutual exclusion exactly while freeing the event loop. It is
deliberately not the default executor, which is shared with every
``asyncio.to_thread`` in the process and is already known to saturate.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import threading
from typing import Any, Callable, TypeVar

MANAGEMENT_WORKER_THREAD_NAME_PREFIX = "mgmt-worker"
MANAGEMENT_WORKER_MAX_WORKERS = 1

T = TypeVar("T")

_executor_lock = threading.Lock()
_executor: concurrent.futures.ThreadPoolExecutor | None = None


def get_management_worker_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the process-wide single-worker management executor."""

    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=MANAGEMENT_WORKER_MAX_WORKERS,
                thread_name_prefix=MANAGEMENT_WORKER_THREAD_NAME_PREFIX,
            )
        return _executor


def shutdown_management_worker_executor(wait: bool = True) -> None:
    """Shut the executor down; a later call recreates it. Idempotent."""

    global _executor
    with _executor_lock:
        executor = _executor
        _executor = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


async def run_on_management_worker(
    fn: Callable[..., T], /, *args: Any, **kwargs: Any
) -> T:
    """Run one blocking call on the shared executor and await its result.

    Exceptions propagate unchanged. Cancelling the awaiting coroutine stops the
    await, not the thread: a call already in flight runs to completion, which
    matches the pre-existing behavior where an in-flight tick also finished
    before cancellation was observed.
    """

    loop = asyncio.get_running_loop()
    executor = get_management_worker_executor()
    return await loop.run_in_executor(
        executor, functools.partial(fn, *args, **kwargs)
    )

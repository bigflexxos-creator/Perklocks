"""P0 Server-Stability — bounded async fan-out helper.

Standard pattern all over the codebase:

    tasks = [_do_one(item) for item in huge_list]
    await asyncio.gather(*tasks)

For lists in the thousands (CFB rosters ≈ 12 000 players, live-gamelog
ingestors ≈ 500-2 000 players) this allocates every coroutine frame at
once.  Even when an inner ``asyncio.Semaphore`` throttles the actual
HTTP work, **every** coroutine still holds its captured closure
variables (team dict, athlete dict, stat dicts).

``bounded_gather`` runs the same computation with at most ``limit``
coroutines *simultaneously running the body*.  We do this by driving
an input queue with a worker pool — coroutines are constructed
lazily inside the worker, so their frames never all exist at once.

Zero behavioural change vs. ``asyncio.gather`` — same input list,
same output ordering, same exception aggregation semantics with
``return_exceptions=True``.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Iterable, List, Optional, TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def bounded_gather(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limit: int = 16,
    return_exceptions: bool = True,
) -> List[R]:
    """Run ``worker(item)`` for each item with at most ``limit`` in flight.

    Coroutines are created lazily inside the worker loop; peak in-memory
    coroutine frames == ``limit`` (not ``len(items)``).

    Results are returned in input order.  If ``return_exceptions`` is
    True, exceptions are collected in place of the return value.
    """
    items_list: List[T] = list(items)
    n = len(items_list)
    if n == 0:
        return []
    results: List[R] = [None] * n                  # type: ignore
    if limit < 1:
        limit = 1
    if limit >= n:
        # Small enough that direct gather is fine.
        outs = await asyncio.gather(
            *[worker(it) for it in items_list],
            return_exceptions=return_exceptions,
        )
        return list(outs)

    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(n):
        queue.put_nowait(i)

    async def _worker() -> None:
        while True:
            try:
                idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                results[idx] = await worker(items_list[idx])
            except Exception as exc:
                if return_exceptions:
                    results[idx] = exc                     # type: ignore
                else:
                    raise
            finally:
                queue.task_done()

    workers = [asyncio.create_task(_worker(), name=f"bounded_gather_{i}")
               for i in range(limit)]
    try:
        await asyncio.gather(*workers)
    finally:
        for w in workers:
            if not w.done():
                w.cancel()
    return results


__all__ = ["bounded_gather"]

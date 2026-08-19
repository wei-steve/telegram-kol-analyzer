"""A per-key asyncio lock registry, for serializing work within a key only.

The message-processing chain used to hold one process-wide ``asyncio.Lock``
across every chat, so a slow message in one chat delayed every other chat.
Ordering only ever needed to be preserved *within* a chat. This registry
creates one lock per key on first use, so unrelated keys can proceed
concurrently while same-key work stays serialized, in arrival order.

Locks are dropped once nothing references them and they are unlocked, so a
process with a large or unbounded key space (chat ids arriving over a long
uptime) does not accumulate locks forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import AbstractAsyncContextManager, asynccontextmanager


class KeyedAsyncLockRegistry:
    """Lazily created per-key ``asyncio.Lock`` instances with bounded growth."""

    def __init__(self) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._refcounts: dict[Hashable, int] = {}
        self._guard = asyncio.Lock()

    def lock(self, key: Hashable) -> AbstractAsyncContextManager[None]:
        """Return an async context manager serializing work under ``key``.

        Two different keys proceed concurrently. The same key serializes, and
        waiters for the same key are served in FIFO order, matching
        ``asyncio.Lock``.
        """

        return self._key_context(key)

    def lock_all(self) -> AbstractAsyncContextManager[None]:
        """Return an async context manager acquiring every known key's lock.

        For the rare cross-chat operation. Locks are acquired in a
        deterministic sorted order, which is what makes this deadlock-free
        against a concurrent ``lock_all()`` call: any two callers request
        locks in the same relative order, so no cycle of "A waits for B's
        lock while B waits for A's lock" can form.
        """

        return self._all_context()

    def known_key_count(self) -> int:
        """Return the number of keys currently holding a registry entry."""

        return len(self._locks)

    @asynccontextmanager
    async def _key_context(self, key: Hashable) -> AsyncIterator[None]:
        per_key_lock = await self._acquire_ref(key)
        try:
            async with per_key_lock:
                yield
        finally:
            await self._release_ref(key)

    async def _acquire_ref(self, key: Hashable) -> asyncio.Lock:
        async with self._guard:
            per_key_lock = self._locks.get(key)
            if per_key_lock is None:
                per_key_lock = asyncio.Lock()
                self._locks[key] = per_key_lock
            self._refcounts[key] = self._refcounts.get(key, 0) + 1
            return per_key_lock

    async def _release_ref(self, key: Hashable) -> None:
        async with self._guard:
            remaining = self._refcounts.get(key, 0) - 1
            if remaining <= 0:
                self._refcounts.pop(key, None)
                per_key_lock = self._locks.get(key)
                if per_key_lock is not None and not per_key_lock.locked():
                    del self._locks[key]
            else:
                self._refcounts[key] = remaining

    @asynccontextmanager
    async def _all_context(self) -> AsyncIterator[None]:
        async with self._guard:
            ordered_keys = sorted(self._locks.keys(), key=repr)
            ordered_locks = [self._locks[key] for key in ordered_keys]
        acquired: list[asyncio.Lock] = []
        try:
            for per_key_lock in ordered_locks:
                await per_key_lock.acquire()
                acquired.append(per_key_lock)
            yield
        finally:
            for per_key_lock in reversed(acquired):
                per_key_lock.release()

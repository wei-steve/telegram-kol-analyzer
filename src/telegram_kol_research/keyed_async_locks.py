"""Per-key asyncio locks with writer-preference cross-key admission.

Message ordering only needs to be preserved *within* a chat, so a single
process-wide ``asyncio.Lock`` across every chat is stricter than required.
This registry creates one lock per key on first use, so unrelated keys can
proceed concurrently while same-key work stays serialized, in arrival order.
It backs ``message_lock_mode="per_chat"`` in
:mod:`telegram_kol_research.message_lock_provider`.

``lock_all()`` is an admission barrier rather than a snapshot of known keys:
once a cross-key caller announces intent, new per-key callers wait until it has
entered and exited. Locks are dropped once nothing references them and they are
unlocked, so an unbounded key space does not accumulate locks forever.
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
        self._admission = asyncio.Condition()
        self._active_readers = 0
        self._waiting_writers = 0
        self._writer_active = False

    def lock(self, key: Hashable) -> AbstractAsyncContextManager[None]:
        """Return an async context manager serializing work under ``key``.

        Two different keys proceed concurrently. The same key serializes, and
        waiters for the same key are served in FIFO order, matching
        ``asyncio.Lock``.
        """

        return self._key_context(key)

    def lock_all(self) -> AbstractAsyncContextManager[None]:
        """Return an exclusive context blocking every per-key admission."""

        return self._all_context()

    def known_key_count(self) -> int:
        """Return the number of keys currently holding a registry entry."""

        return len(self._locks)

    def snapshot(self) -> dict[str, int | bool]:
        """Return a read-only in-memory view of current admission state."""

        return {
            "active_shared_admissions": self._active_readers,
            "waiting_exclusive_admissions": self._waiting_writers,
            "exclusive_admission_active": self._writer_active,
            "known_key_count": len(self._locks),
        }

    @asynccontextmanager
    async def _key_context(self, key: Hashable) -> AsyncIterator[None]:
        async with self._shared_admission():
            async with self._key_only_context(key):
                yield

    @asynccontextmanager
    async def _key_only_context(self, key: Hashable) -> AsyncIterator[None]:
        """Hold only ``key``; callers must already hold shared admission."""

        per_key_lock = await self._acquire_ref(key)
        try:
            async with per_key_lock:
                yield
        finally:
            await self._release_ref(key)

    @asynccontextmanager
    async def _shared_admission(self) -> AsyncIterator[None]:
        """Admit per-key work unless a writer is active or waiting."""

        admitted = False
        try:
            async with self._admission:
                await self._admission.wait_for(
                    lambda: not self._writer_active
                    and self._waiting_writers == 0
                )
                self._active_readers += 1
                admitted = True
            yield
        finally:
            if admitted:
                async with self._admission:
                    if self._active_readers <= 0:
                        raise RuntimeError("shared admission counter underflow")
                    self._active_readers -= 1
                    if self._active_readers == 0:
                        self._admission.notify_all()

    @asynccontextmanager
    async def _exclusive_admission(self) -> AsyncIterator[None]:
        """Admit one writer after existing readers and before new readers."""

        registered = False
        acquired = False
        try:
            async with self._admission:
                self._waiting_writers += 1
                registered = True
                try:
                    await self._admission.wait_for(
                        lambda: not self._writer_active
                        and self._active_readers == 0
                    )
                except BaseException:
                    self._waiting_writers -= 1
                    registered = False
                    self._admission.notify_all()
                    raise

                self._waiting_writers -= 1
                registered = False
                self._writer_active = True
                acquired = True
            yield
        finally:
            if registered:
                async with self._admission:
                    if self._waiting_writers <= 0:
                        raise RuntimeError("exclusive admission counter underflow")
                    self._waiting_writers -= 1
                    registered = False
                    self._admission.notify_all()
            if acquired:
                async with self._admission:
                    if not self._writer_active:
                        raise RuntimeError("exclusive admission ownership lost")
                    self._writer_active = False
                    acquired = False
                    self._admission.notify_all()

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
        async with self._exclusive_admission():
            yield

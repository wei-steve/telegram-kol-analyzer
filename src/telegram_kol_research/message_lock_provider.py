"""Resolve the message-processing lock per chat, honoring a live settings flag.

`handle_new_message` used to hold one process-wide `asyncio.Lock` across the
whole processing chain, so a slow message in one chat delayed every other
chat. `trading_settings.message_lock_mode` controls whether that stays true:

- ``"global"`` (the default): every chat gets the same shared lock, which is
  byte-for-byte the pre-Phase-2 behavior.
- ``"per_chat"``: each chat gets its own lock from a
  ``KeyedAsyncLockRegistry``, so unrelated chats no longer serialize on one
  another. Cross-chat operations (a manual refresh, a reconcile pass in
  per-chat mode) use :meth:`MessageLockProvider.lock_all` instead.

The mode is read fresh from the database on every call, not cached at
startup, so operators can flip it with no restart and no deploy - the
rollback path Phase 2 depends on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Hashable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from telegram_kol_research.keyed_async_locks import KeyedAsyncLockRegistry
from telegram_kol_research.trading_settings import load_trading_settings


class MessageLockProvider:
    """Resolve the lock context manager for one chat, or for every chat."""

    def __init__(
        self,
        *,
        session_factory: Any,
        global_lock: asyncio.Lock,
        registry: KeyedAsyncLockRegistry,
    ) -> None:
        self._session_factory = session_factory
        self._global_lock = global_lock
        self._registry = registry

    def mode(self) -> Literal["global", "per_chat"]:
        """Return the currently configured mode, read fresh from settings."""

        return load_trading_settings(self._session_factory).message_lock_mode

    def __call__(self, chat_id: Hashable) -> AbstractAsyncContextManager[None]:
        """Return the lock to hold while processing one message from ``chat_id``.

        In "global" mode this is always the same shared lock, so calling it
        with different ``chat_id`` values still serializes every chat against
        every other, identical to the pre-Phase-2 behavior.
        """

        if self.mode() == "per_chat":
            return self._registry.lock(chat_id)
        return self._global_lock

    def lock_all(self) -> AbstractAsyncContextManager[None]:
        """Return the lock for a cross-chat operation (refresh, mode switch)."""

        if self.mode() == "per_chat":
            return self._registry.lock_all()
        return self._global_lock


def resolve_message_lock_mode(
    operation_lock: Any | None,
) -> Literal["none", "global", "per_chat"]:
    """Classify an ``operation_lock`` argument without assuming its type.

    ``None`` means no locking at all (today's fallback for tests that opt
    out). A plain lock-like object (anything without a ``mode()`` method,
    e.g. a bare ``asyncio.Lock`` passed by an existing caller or test) is
    "global" - the only mode that existed before this module. A
    :class:`MessageLockProvider` reports its own current mode.
    """

    if operation_lock is None:
        return "none"
    mode_method = getattr(operation_lock, "mode", None)
    if callable(mode_method):
        resolved = mode_method()
        if resolved == "per_chat":
            return "per_chat"
        return "global"
    return "global"


def resolve_lock_context(
    operation_lock: Any | None,
    chat_id: Hashable,
) -> AbstractAsyncContextManager[None] | None:
    """Resolve the context manager ``operation_lock`` implies for ``chat_id``.

    ``operation_lock`` may be ``None`` (no locking), a callable provider
    (resolved with ``chat_id``), or a plain lock-like object usable directly
    as an async context manager - the shape every caller used before this
    module existed, kept working unchanged.
    """

    if operation_lock is None:
        return None
    if callable(operation_lock):
        return operation_lock(chat_id)
    return operation_lock

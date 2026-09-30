"""Cheap version numbers for the web console's live data patching.

``GET /api/live/state`` answers "did anything change?" for every open browser
tab every 5 seconds, so everything here must be:

* index or primary-key backed (a full scan of ``raw_messages`` froze the worker
  event loop on 2026-09-15 -- see ``build_global_freshness_statement``);
* computed at most once per TTL no matter how many tabs are polling
  (:class:`LiveStateCache`, single-flight);
* free of exchange reads: position content comes from the snapshot the worker
  already persisted, never from Deepcoin.

Design: docs/plans/2026-09-30-web-live-data-and-positions-first-design.md 3.2.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import threading
import time
from typing import Any

from sqlalchemy import func, select

from telegram_kol_research.models import (
    ExecutionEvent,
    RecognitionDecision,
    StrategyLifecycle,
    StrategyManagementBatch,
)

# Keys that only record *when* a snapshot was fetched. They must not move the
# positions version: the worker rewrites the snapshot every ~5 seconds and the
# page should only be patched when position content actually changed.
VOLATILE_SNAPSHOT_KEYS = frozenset(
    {
        "captured_at",
        "capturedAt",
        "fetched_at",
        "fetchedAt",
        "refreshed_at",
        "refreshedAt",
        "snapshot_at",
        "snapshotAt",
        "as_of",
    }
)

# Lifecycles that are still in flight. ``ix_strategy_lifecycles_status`` makes
# this a bounded index range (about thirty rows in production).
ACTIVE_LIFECYCLE_STATUSES = ("entered", "pending_entry")

LIVE_STATE_TTL_SECONDS = 2.0
STRATEGY_LIST_RENDER_CACHE_MAX_ENTRIES = 16


def strip_volatile_snapshot_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): strip_volatile_snapshot_keys(item)
            for key, item in value.items()
            if key not in VOLATILE_SNAPSHOT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [strip_volatile_snapshot_keys(item) for item in value]
    return value


def _short_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


def positions_version(payload: dict[str, Any] | None) -> str:
    """Content hash of a cached position snapshot payload, or ``p:none``."""

    if payload is None:
        return "p:none"
    body = json.dumps(
        strip_volatile_snapshot_keys(payload),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return f"p:{_short_hash(body)}"


def build_strategy_version_statements() -> list[tuple[str, Any]]:
    """Statements behind the ``strategies`` version, each index/PK backed.

    ``execution_bindings.updated_at`` is deliberately absent: the reconcile
    pass rewrites it on every binding roughly every 22 seconds, which would
    make the version change constantly (design 1.4 b).
    """

    statements: list[tuple[str, Any]] = [
        ("lifecycle_max_id", select(func.max(StrategyLifecycle.id))),
        ("execution_event_max_id", select(func.max(ExecutionEvent.id))),
        (
            "management_batch_max_id",
            select(func.max(StrategyManagementBatch.id)),
        ),
        ("recognition_decision_max_id", select(func.max(RecognitionDecision.id))),
        (
            "active_lifecycles",
            select(
                func.count(StrategyLifecycle.id),
                func.max(StrategyLifecycle.updated_at),
            ).where(
                StrategyLifecycle.lifecycle_status.in_(ACTIVE_LIFECYCLE_STATUSES)
            ),
        ),
    ]
    return statements


def strategies_base_fingerprint(session) -> str:
    parts: list[str] = []
    for label, statement in build_strategy_version_statements():
        row = session.execute(statement).one()
        parts.append(f"{label}={'|'.join(str(item) for item in row)}")
    return ";".join(parts)


def strategies_version(session, *, positions: str) -> str:
    """Version of everything a strategy card shows.

    Real-position state is shown on strategy cards, so the positions version
    is folded in.
    """

    return f"s:{_short_hash(strategies_base_fingerprint(session) + ';' + positions)}"


def messages_version(raw_message_id: int | None) -> str:
    return f"r:{int(raw_message_id or 0)}"


def groups_version(messages: str, config_signature: Any) -> str:
    return f"g:{_short_hash(f'{messages};{config_signature}')}"


class LiveStateCache:
    """One shared value, recomputed at most once per TTL (single-flight)."""

    def __init__(
        self,
        *,
        ttl_seconds: float = LIVE_STATE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._value: Any = None
        self._computed_at: float | None = None

    def get(self, compute: Callable[[], Any]) -> Any:
        # The lock is held across the computation on purpose: concurrent
        # callers wait for the one in-flight computation and then reuse its
        # value instead of each running the queries.
        with self._lock:
            now = self._clock()
            if (
                self._computed_at is not None
                and 0 <= now - self._computed_at < self._ttl
            ):
                return self._value
            value = compute()
            self._value = value
            self._computed_at = self._clock()
            return value


class StrategyListRenderCache:
    """Latest rendered strategy-list HTML per query, valid for one version.

    Keyed by the request's query parameters; an entry is reused only while the
    (strategies, positions) version pair it was rendered for is unchanged, and
    only the latest render per query is kept. The number of distinct queries is
    bounded (oldest evicted) so a client cycling filters cannot grow it.

    Renders for one query are single-flight: concurrent callers wait for the
    render in progress and reuse it, so N tabs cost one render.
    """

    def __init__(self, *, max_entries: int = STRATEGY_LIST_RENDER_CACHE_MAX_ENTRIES):
        self._max_entries = max(1, int(max_entries))
        self._guard = threading.Lock()
        self._entries: dict[Any, tuple[Any, bytes]] = {}
        self._key_locks: dict[Any, threading.Lock] = {}
        self.render_count = 0

    def get(
        self, key: Any, versions: Any, render: Callable[[], bytes]
    ) -> bytes:
        with self._guard:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._guard:
                entry = self._entries.get(key)
            if entry is not None and entry[0] == versions:
                return entry[1]
            body = render()
            with self._guard:
                self.render_count += 1
                self._entries.pop(key, None)
                self._entries[key] = (versions, body)
                while len(self._entries) > self._max_entries:
                    oldest = next(iter(self._entries))
                    del self._entries[oldest]
                    self._key_locks.pop(oldest, None)
            return body

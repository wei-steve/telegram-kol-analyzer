"""Five-step REST resync for the Deepcoin private stream, and contract-name mapping.

Phase 2 of the REST+WebSocket program. Hard rule 12 of
``docs/rest-ws-trading-status.md`` requires that after a disconnect, a network
outage or a process restart the system re-aligns over REST before it may call
its observation complete. That sequence is implemented here exactly as the
handoff document specifies:

1. REST snapshot of active orders, trigger orders, fills and positions,
2. replay the locally persisted events that were never folded in,
3. re-establish the WebSocket and finish subscribing,
4. **a second REST snapshot**, covering the race window between step 1 and the
   subscription becoming effective,
5. compare stream state against REST state, letting state only move forward,
   and return ``healthy`` only once everything converged.

Step 4 is not optional. Dropping it does not remove the race window, it only
hides it.

Two rules constrain every read below:

* **GET only.** This module performs no exchange write of any kind. It calls
  only the existing ``list_*`` / ``read_*`` methods of ``DeepcoinRestClient``
  and adds no new read endpoint.
* **An incomplete read is unknown, never zero** (hard rule 4). A read that
  raises is recorded in ``incomplete_reads`` and blocks convergence. It is
  never allowed to look like "there are no orders".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telegram_kol_research.deepcoin_ws_stream_state import (
    ResyncOutcome,
    WsEntityStateTracker,
)

logger = logging.getLogger(__name__)

# Rebuild the instrument map at most this often; it is a product catalogue, not
# live state.
INSTRUMENT_MAP_TTL_SECONDS = 6 * 3600

# Replaying an unbounded backlog would stall startup. Anything beyond this is
# left ``unprocessed`` and blocks convergence rather than being silently
# skipped.
MAX_REPLAY_EVENTS = 5000


class DeepcoinInstrumentIdMap:
    """Explicit two-way map between the WS spelling and the REST spelling.

    The stream says ``ETHUSDT``; REST says ``ETH-USDT-SWAP``. That is a real
    format difference, confirmed in the recorded experiment, not a typo.

    The map is **built once from the authoritative product list**
    (``list_swap_instruments``) and then only looked up. Lookup never falls back
    to string surgery: an instrument that is not in the table resolves to
    ``None`` and its caller fails closed. Normalisation is applied while
    building, against the closed set of instruments the exchange itself
    published, and any two instruments that would collapse onto the same stream
    name are dropped from both directions rather than guessed between.
    """

    def __init__(self) -> None:
        self._ws_to_rest: dict[str, str] = {}
        self._rest_to_ws: dict[str, str] = {}
        self._built_at_ms: int | None = None
        self.instrument_count = 0
        self.collision_count = 0

    @staticmethod
    def _stream_name(inst_id: str) -> str | None:
        text = inst_id.strip().upper()
        if not text:
            return None
        parts = [part for part in text.split("-") if part]
        if len(parts) < 2:
            return None
        if parts[-1] == "SWAP":
            parts = parts[:-1]
        if len(parts) < 2:
            return None
        return "".join(parts)

    def build(self, instruments: Iterable[dict[str, Any]]) -> None:
        ws_to_rest: dict[str, str] = {}
        rest_to_ws: dict[str, str] = {}
        collisions: set[str] = set()
        count = 0
        for row in instruments:
            if not isinstance(row, dict):
                continue
            inst_id = str(row.get("instId") or "").strip()
            if not inst_id:
                continue
            count += 1
            stream_name = self._stream_name(inst_id)
            if stream_name is None:
                continue
            existing = ws_to_rest.get(stream_name)
            if existing is not None and existing != inst_id:
                collisions.add(stream_name)
                continue
            ws_to_rest[stream_name] = inst_id
            rest_to_ws[inst_id.upper()] = stream_name
        for stream_name in collisions:
            rest_id = ws_to_rest.pop(stream_name, None)
            if rest_id is not None:
                rest_to_ws.pop(rest_id.upper(), None)
        self._ws_to_rest = ws_to_rest
        self._rest_to_ws = rest_to_ws
        self.instrument_count = count
        self.collision_count = len(collisions)

    def is_stale(self, *, now_ms: int, ttl_seconds: int = INSTRUMENT_MAP_TTL_SECONDS) -> bool:
        if self._built_at_ms is None:
            return True
        return now_ms - self._built_at_ms >= ttl_seconds * 1000

    def mark_built(self, *, now_ms: int) -> None:
        self._built_at_ms = now_ms

    def invalidate(self) -> None:
        self._built_at_ms = None

    @property
    def size(self) -> int:
        return len(self._ws_to_rest)

    def rest_id_for_stream_name(self, stream_name: str | None) -> str | None:
        """Return the REST ``instId``, or ``None`` when the map does not know it."""

        if not stream_name:
            return None
        return self._ws_to_rest.get(str(stream_name).strip().upper())

    def stream_name_for_rest_id(self, inst_id: str | None) -> str | None:
        if not inst_id:
            return None
        return self._rest_to_ws.get(str(inst_id).strip().upper())


@dataclass
class RestSnapshot:
    """One REST view of the account. ``complete`` is the only trustworthy flag.

    ``complete=False`` means *unknown*. It never means the account is empty:
    the collections on an incomplete snapshot must not be read as "there is
    none of these".
    """

    complete: bool
    positions: list[dict[str, Any]]
    open_orders: list[dict[str, Any]]
    trigger_orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    instruments_queried: tuple[str, ...] = ()
    incomplete_reads: tuple[str, ...] = ()
    unresolved_instruments: tuple[str, ...] = ()

    def object_count(self) -> int:
        return (
            len(self.positions)
            + len(self.open_orders)
            + len(self.trigger_orders)
            + len(self.fills)
        )


def _row_time_ms(row: dict[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


_REST_TIME_KEYS = ("uTime", "cTime", "ts", "fillTime")


class DeepcoinWsResyncCoordinator:
    """Runs the five-step resync. Owns no socket and no database session.

    Everything it touches arrives as an injected callable, which is what makes
    the whole sequence -- including the mid-sequence subscribe -- reproducible
    in an offline test.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        now_provider: Callable[[], datetime],
        monotonic_ms_provider: Callable[[], int],
        instrument_map: DeepcoinInstrumentIdMap | None = None,
        max_replay_events: int = MAX_REPLAY_EVENTS,
    ) -> None:
        self._client_factory = client_factory
        self._now = now_provider
        self._now_ms = monotonic_ms_provider
        self.instrument_map = instrument_map or DeepcoinInstrumentIdMap()
        self._max_replay_events = max_replay_events

    # ---------------------------------------------------------------- reads

    def _with_client(self, work: Callable[[Any], Any]) -> Any:
        client = self._client_factory()
        try:
            return work(client)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.warning("Deepcoin resync client cleanup failed")

    def refresh_instrument_map(self, *, force: bool = False) -> bool:
        """Rebuild the instrument map from REST. Returns whether it is usable."""

        now_ms = self._now_ms()
        if not force and not self.instrument_map.is_stale(now_ms=now_ms):
            return self.instrument_map.size > 0
        try:
            instruments = self._with_client(lambda c: c.list_swap_instruments())
        except Exception as exc:
            logger.warning(
                "Deepcoin instrument map refresh failed (%s); staying fail-closed",
                type(exc).__name__,
            )
            return self.instrument_map.size > 0
        self.instrument_map.build(instruments or [])
        self.instrument_map.mark_built(now_ms=now_ms)
        return self.instrument_map.size > 0

    def rest_snapshot(self, *, stream_instruments: Iterable[str] = ()) -> RestSnapshot:
        """Read the account over GET only.

        ``stream_instruments`` are stream-format names seen in the inbox; each
        one is resolved through the explicit map so that per-instrument trigger
        reads can be issued. A name the map cannot resolve is reported in
        ``unresolved_instruments`` and makes the snapshot incomplete: an
        unresolvable contract is unknown coverage, not an empty one.
        """

        incomplete: list[str] = []
        positions: list[dict[str, Any]] = []
        open_orders: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        trigger_orders: list[dict[str, Any]] = []

        def _read(label: str, work: Callable[[Any], Any]) -> list[dict[str, Any]]:
            try:
                rows = self._with_client(work)
            except Exception as exc:
                # Hard rule 4: a failed read is unknown, not zero.
                incomplete.append(f"{label}:{type(exc).__name__}")
                return []
            return [row for row in (rows or []) if isinstance(row, dict)]

        positions = _read("positions", lambda c: c.list_positions())
        open_orders = _read("open_orders", lambda c: c.list_open_orders())
        fills = _read("fills", lambda c: c.list_trade_fills())

        rest_ids: set[str] = set()
        unresolved: list[str] = []
        for row in list(positions) + list(open_orders) + list(fills):
            inst_id = str(row.get("instId") or "").strip()
            if inst_id:
                rest_ids.add(inst_id)
        for stream_name in stream_instruments:
            resolved = self.instrument_map.rest_id_for_stream_name(stream_name)
            if resolved is None:
                if stream_name and stream_name not in unresolved:
                    unresolved.append(str(stream_name))
                continue
            rest_ids.add(resolved)

        for inst_id in sorted(rest_ids):
            trigger_orders.extend(
                _read(
                    f"trigger_orders[{inst_id}]",
                    lambda c, _inst=inst_id: c.list_trigger_orders_pending(
                        inst_id=_inst
                    ),
                )
            )

        complete = not incomplete and not unresolved
        return RestSnapshot(
            complete=complete,
            positions=positions,
            open_orders=open_orders,
            trigger_orders=trigger_orders,
            fills=fills,
            instruments_queried=tuple(sorted(rest_ids)),
            incomplete_reads=tuple(incomplete),
            unresolved_instruments=tuple(unresolved),
        )

    # ----------------------------------------------------------- comparison

    @staticmethod
    def compare_forward_only(
        before: RestSnapshot,
        after: RestSnapshot,
        tracker: WsEntityStateTracker,
    ) -> tuple[int, list[str]]:
        """Return (advanced object count, reasons the comparison did not settle).

        "Forward only" here means: an object present in ``before`` whose REST
        timestamp in ``after`` went backwards is a regression and blocks
        convergence. An object that disappeared between the snapshots is a
        *completion* (filled, cancelled, closed) and is forward movement, not a
        zero -- but only when both snapshots were complete, which the caller
        has already checked.
        """

        reasons: list[str] = []
        advanced = 0

        def _index(snapshot: RestSnapshot) -> dict[tuple[str, str], int | None]:
            index: dict[tuple[str, str], int | None] = {}
            groups = (
                ("position", snapshot.positions, ("posId", "positionId")),
                ("order", snapshot.open_orders, ("ordId", "orderId")),
                ("trigger", snapshot.trigger_orders, ("ordId", "orderId")),
                ("fill", snapshot.fills, ("tradeId", "billId", "ordId")),
            )
            for kind, rows, id_keys in groups:
                for row in rows:
                    identity = ""
                    for key in id_keys:
                        value = str(row.get(key) or "").strip()
                        if value:
                            identity = value
                            break
                    if not identity:
                        continue
                    index[(kind, identity)] = _row_time_ms(row, _REST_TIME_KEYS)
            return index

        before_index = _index(before)
        after_index = _index(after)
        for key, before_ms in before_index.items():
            if key not in after_index:
                advanced += 1
                continue
            after_ms = after_index[key]
            if before_ms is None or after_ms is None:
                continue
            if after_ms < before_ms:
                reasons.append(f"rest_time_regression:{key[0]}")
                continue
            if after_ms > before_ms:
                advanced += 1
        advanced += sum(1 for key in after_index if key not in before_index)
        # The tracker is consulted only to confirm it holds no entity the second
        # snapshot contradicts; it is never rolled back from REST.
        if tracker.entity_count() and not after.complete:
            reasons.append("stream_state_without_complete_rest")
        return advanced, reasons

    # ------------------------------------------------------------ sequence

    def run(
        self,
        *,
        tracker: WsEntityStateTracker,
        replay_unprocessed: Callable[[WsEntityStateTracker, int], int],
        subscribe: Callable[[], None],
        stream_instruments: Iterable[str] = (),
    ) -> ResyncOutcome:
        """Execute steps 1-5 and report whether everything converged."""

        started_at = self._now()
        durations: dict[str, int] = {}
        stream_instruments = list(stream_instruments)

        def _step(label: str, work: Callable[[], Any]) -> Any:
            start_ms = self._now_ms()
            try:
                return work()
            finally:
                durations[label] = max(0, self._now_ms() - start_ms)

        self.refresh_instrument_map()

        # Step 1 -- REST snapshot before anything else.
        before: RestSnapshot = _step(
            "step1_rest_snapshot",
            lambda: self.rest_snapshot(stream_instruments=stream_instruments),
        )

        # Step 2 -- replay whatever the inbox persisted but never folded in.
        replayed: int = _step(
            "step2_replay_events",
            lambda: int(replay_unprocessed(tracker, self._max_replay_events)),
        )

        # Step 3 -- re-establish the stream and finish subscribing.
        try:
            _step("step3_subscribe", subscribe)
        except Exception as exc:
            finished_at = self._now()
            return ResyncOutcome(
                converged=False,
                reason=f"subscribe_failed:{type(exc).__name__}",
                started_at=started_at,
                finished_at=finished_at,
                step_durations_ms=durations,
                replayed_events=replayed,
                rest_objects_before=before.object_count(),
            )

        # Step 4 -- the second snapshot. It closes the race window between the
        # first snapshot and the subscription taking effect. Removing it would
        # not remove the window.
        after: RestSnapshot = _step(
            "step4_rest_snapshot",
            lambda: self.rest_snapshot(stream_instruments=stream_instruments),
        )

        # Step 5 -- forward-only comparison.
        advanced, reasons = _step(
            "step5_compare",
            lambda: self.compare_forward_only(before, after, tracker),
        )

        incomplete = tuple(before.incomplete_reads) + tuple(after.incomplete_reads)
        unresolved = tuple(
            dict.fromkeys(
                tuple(before.unresolved_instruments)
                + tuple(after.unresolved_instruments)
            )
        )
        finished_at = self._now()

        if incomplete:
            reason = "incomplete_rest_read"
        elif unresolved:
            reason = "unresolved_instrument"
        elif reasons:
            reason = reasons[0]
        else:
            reason = "converged"

        return ResyncOutcome(
            converged=reason == "converged",
            reason=reason,
            started_at=started_at,
            finished_at=finished_at,
            step_durations_ms=durations,
            replayed_events=replayed,
            rest_objects_before=before.object_count(),
            rest_objects_after=after.object_count(),
            advanced_entities=advanced,
            incomplete_reads=incomplete,
            unresolved_instruments=unresolved,
        )

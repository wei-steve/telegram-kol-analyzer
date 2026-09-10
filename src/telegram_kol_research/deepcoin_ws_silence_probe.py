"""Ask whether a silent stream missed anything, before tearing it down.

Phase 6-pre-4. The application-level silence timer rebuilds the stream after
ten minutes with no frame at all. That was the right cheap default -- a live
pong proves the socket is open and proves nothing about the business stream
still being routed to us -- but 6-pre-1 measured what it costs: **145 gaps,
1060 seconds, 1.23% of the day, and 134 of those were silence timeouts rather
than real disconnects.** Every one of them is a window in which a new entry is
held back.

**What this probe claims is narrower than "the subscription is alive", because
that cannot be established.** Deepcoin publishes no listen-key renewal
endpoint, and acquiring a fresh key would only prove the REST credential works
-- a new key belongs to a new connection. Nor does a REST snapshot help
directly: during silence the local state was built from earlier frames and
"no new frames" is the definition of silence, so agreement is guaranteed
whether the stream is healthy or dead.

So the probe answers the question a snapshot *can* answer: **did anything
change during this silence that we would have missed?** Two snapshots taken at
different times either match or they do not.

* they match -> nothing happened that we could have missed. Even if the
  subscription is already dead, not reconnecting costs no information. Reset
  the timer and keep reading.
* they differ -> something changed and no frame told us. Reconnect and resync
  immediately -- earlier and better aimed than a timer would have been.
* it cannot be read -> rule 4: an unreadable exchange is "unknown", never
  "nothing happened". Reconnect.

**Why the comparison is snapshot-to-snapshot and not snapshot-to-ledger.** The
ledger drifts. The example that prompted this: on 2026-09-10 pos
``1001125178552543`` was a live three-contract BTC short while the leg owning
it had read ``manually_closed`` for two days. (That particular row has since
been repaired -- but the mechanism that produced it, a snapshot taken before a
fact existed being committed after it, is not fixed yet, and its signature is
``recovered_at`` later than ``updated_at``. The example is history; the drift
is not.) Comparing against the ledger would find a difference on every probe
for as long as any such row exists, reconnect every ten minutes exactly as
before, and cost three GETs for the privilege. The ledger difference is still computed -- as an *observation*
written into the gap statistics, so drift like that stops being invisible --
but it never decides whether to reconnect.

**Residual, stated rather than argued away.** A change that goes A -> B -> A
between two probes looks like no change. That needs the subscription to be
dead *and* the value to return to exactly where it was, and the exposure is
bounded by the next probe or the sixty-minute planned reconnect, whichever
comes first. The planned reconnect on listen-key expiry, ping/pong timeouts
and real disconnects (socket close, ``50118``) are all unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

PROBE_PASS = "missed_nothing"
PROBE_CHANGED = "changed_during_silence"
PROBE_UNREADABLE = "unreadable"
PROBE_NO_BASELINE = "no_baseline"
#: A frame arrived since the baseline was taken, so the baseline describes a
#: world we have already been told changed. Comparing against it would report a
#: difference we did not miss. The probe refreshes it and passes instead --
#: what it is really asking is "has anything changed **since the last frame**",
#: and a frame having just arrived is itself evidence the stream was alive.
PROBE_REFRESHED = "baseline_refreshed_after_frame"


@dataclass(frozen=True, slots=True)
class SilenceProbeResult:
    status: str
    fingerprint: str | None = None
    reason: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def missed_nothing(self) -> bool:
        """Only an affirmative answer keeps the connection. Everything else reconnects.

        Two answers are affirmative and they mean different things: nothing
        changed since the last probe, or the baseline predated a frame we did
        receive. Both establish that no event went unseen; only the second one
        costs a baseline refresh.
        """

        return self.status in (PROBE_PASS, PROBE_REFRESHED)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def position_facts(rows: Sequence[Mapping[str, Any]] | None) -> list[list[str]] | None:
    """The position facts a missed frame would have changed.

    ``posId`` plus size and side, and deliberately **not** ``slTriggerPx``:
    that field reflects only the most recent TPSL pair and has twice been read
    as "this position has no stop" when two stop orders were resting on it
    (ARCHITECTURE section 6). A field that lies is worse here than no field.
    """

    if not isinstance(rows, list):
        return None
    facts = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        facts.append(
            [
                str(row.get("posId") or ""),
                str(row.get("posSide") or row.get("side") or "").lower(),
                str(row.get("pos") or ""),
            ]
        )
    return sorted(facts)


def pending_order_facts(
    rows: Sequence[Mapping[str, Any]] | None,
) -> list[list[str]] | None:
    if not isinstance(rows, list):
        return None
    facts = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        facts.append(
            [
                str(row.get("ordId") or row.get("orderId") or ""),
                str(row.get("posSide") or "").lower(),
                str(row.get("sz") or ""),
            ]
        )
    return sorted(facts)


def snapshot_fingerprint(
    *,
    positions: Sequence[Mapping[str, Any]] | None,
    pending_by_instrument: Mapping[str, Sequence[Mapping[str, Any]] | None],
    open_orders: Sequence[Mapping[str, Any]] | None = None,
) -> str | None:
    """One stable digest of everything a missed frame could have moved.

    ``None`` when any part could not be read -- a partial snapshot must not be
    compared against a whole one, because the difference would read as "the
    exchange changed" when it means "we saw less this time".
    """

    position_part = position_facts(positions)
    if position_part is None:
        return None
    pending_part: dict[str, list[list[str]]] = {}
    for inst_id, rows in sorted(pending_by_instrument.items()):
        facts = pending_order_facts(rows)
        if facts is None:
            return None
        pending_part[str(inst_id).upper()] = facts
    order_part = None
    if open_orders is not None:
        order_part = pending_order_facts(open_orders)
        if order_part is None:
            return None
    payload = {
        "positions": position_part,
        "pending": pending_part,
        "open_orders": order_part,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def instruments_to_probe(session_factory) -> tuple[list[str], bool]:
    """Which instruments to snapshot, and whether a V2 order read is needed.

    Bounded on purpose: the instruments this account actually holds something
    on, plus a V2 ``orders-pending`` read **only** when the ledger still has a
    live ordinary limit leg. Phase 5a established that endpoint is blind to
    ordinary limit orders on V1 and that the migrated legs are the only ones
    that need it, so reading it unconditionally would spend a GET to learn
    nothing.

    The cost is ``1 + len(instruments) + (1 if limit legs else 0)``, so it
    scales with how many instruments the account holds at once -- measured at
    **4** against production on 2026-09-10 (BTC and ETH). The phase note's
    "three GETs" was written assuming a single instrument and is wrong; the
    real ceiling is the instrument count, which nothing here caps. At one probe
    per 600s against a 5/s quota that is not worth capping, but a bound nobody
    enforces must not be written down as if it were one.
    """

    from telegram_kol_research.models import ExecutionOrderLeg

    instruments: set[str] = set()
    needs_open_orders = False
    with session_factory() as session:
        rows = (
            session.query(
                ExecutionOrderLeg.request_json,
                ExecutionOrderLeg.order_kind,
                ExecutionOrderLeg.status,
            )
            .filter(
                ExecutionOrderLeg.status.in_(
                    ("pending", "submitting", "submitted", "active")
                )
            )
            .all()
        )
    for request_json, order_kind, _status in rows:
        try:
            request = json.loads(request_json or "{}")
        except (TypeError, ValueError):
            request = {}
        inst_id = str(request.get("instId") or "").upper() if isinstance(request, dict) else ""
        if inst_id:
            instruments.add(inst_id)
        if str(order_kind or "") == "limit":
            needs_open_orders = True
    return sorted(instruments), needs_open_orders


def take_silence_snapshot(
    deepcoin_client: Any,
    *,
    instruments: Sequence[str],
    include_open_orders: bool,
) -> tuple[str | None, dict[str, Any]]:
    """Read the snapshot. Any failure yields ``None`` and the reason, never a partial.

    Every call goes through the ordinary client, so it shares the 5/s limiter
    with entry admission and the reconcilers rather than opening a second
    channel with its own budget.
    """

    detail: dict[str, Any] = {"instruments": list(instruments), "gets": 0}
    try:
        positions = deepcoin_client.list_positions()
        detail["gets"] += 1
    except Exception as exc:
        detail["failure"] = f"positions:{type(exc).__name__}"
        return None, detail

    pending: dict[str, Any] = {}
    for inst_id in instruments:
        try:
            pending[inst_id] = deepcoin_client.list_trigger_orders_pending(
                inst_id=inst_id
            )
            detail["gets"] += 1
        except Exception as exc:
            detail["failure"] = f"pending:{inst_id}:{type(exc).__name__}"
            return None, detail

    open_orders = None
    if include_open_orders:
        try:
            open_orders = deepcoin_client.list_open_orders()
            detail["gets"] += 1
        except Exception as exc:
            detail["failure"] = f"open_orders:{type(exc).__name__}"
            return None, detail

    fingerprint = snapshot_fingerprint(
        positions=positions,
        pending_by_instrument=pending,
        open_orders=open_orders,
    )
    if fingerprint is None:
        detail["failure"] = "incomplete_snapshot"
        return None, detail
    detail["position_count"] = len(positions) if isinstance(positions, list) else None
    return fingerprint, detail


def probe_silence(
    deepcoin_client: Any,
    session_factory,
    *,
    baseline_fingerprint: str | None,
    baseline_stale: bool = False,
) -> SilenceProbeResult:
    """The whole decision: keep reading, or reconnect.

    ``baseline_fingerprint`` is the snapshot taken once the connection was
    established and its resync had converged -- the last moment we know the
    local picture was right. Without one there is nothing to compare against
    and the answer is to reconnect, never "probably fine".
    """

    instruments, needs_open_orders = instruments_to_probe(session_factory)
    fingerprint, detail = take_silence_snapshot(
        deepcoin_client,
        instruments=instruments,
        include_open_orders=needs_open_orders,
    )
    if fingerprint is None:
        return SilenceProbeResult(
            PROBE_UNREADABLE,
            reason=str(detail.get("failure") or "unreadable"),
            detail=detail,
        )
    if baseline_stale:
        # Frames arrived after the baseline was taken. We were told about those
        # changes, so a difference here is not something we missed -- it is the
        # baseline being out of date. Adopt this snapshot as the new baseline
        # and keep the connection: the arriving frames already showed the
        # stream was being routed to us.
        return SilenceProbeResult(
            PROBE_REFRESHED,
            fingerprint=fingerprint,
            reason="baseline_predates_last_frame",
            detail=detail,
        )
    if baseline_fingerprint is None:
        return SilenceProbeResult(
            PROBE_NO_BASELINE,
            fingerprint=fingerprint,
            reason="no_baseline_snapshot",
            detail=detail,
        )
    if fingerprint != baseline_fingerprint:
        return SilenceProbeResult(
            PROBE_CHANGED,
            fingerprint=fingerprint,
            reason="snapshot_changed_during_silence",
            detail=detail,
        )
    return SilenceProbeResult(
        PROBE_PASS,
        fingerprint=fingerprint,
        reason="no_change_during_silence",
        detail=detail,
    )

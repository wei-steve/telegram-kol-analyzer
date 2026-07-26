"""Strategy lifecycle monitor —per-contract candle scanning with time-order traversal.

Every 60 seconds the monitor:
1. Loads all *pending_entry* and *entered* signals from the last 7 days.
2. Groups them by Gate contract name (e.g. BTC_USDT).
3. For each contract fetches 1m candles from the oldest signal_at to now
   (auto-paginating).
4. Walks the candles chronologically, activating signals once candle time
   passes their *signal_at*, checking entry-range overlaps and SL / TP hits.
5. Persists state transitions and pushes SSE events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.lifecycle_exit_intents import (
    has_live_execution_binding,
    record_lifecycle_exit_intent,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    TradeIdea,
    utc_now,
)

logger = logging.getLogger(__name__)

ExpiryReviewNotifier = Callable[[dict[str, Any]], Awaitable[None]]

# ── helpers ──────────────────────────────────────────────────────────


def _symbol_to_contract(symbol: str) -> str:
    """Normalize a symbol to Gate contract name: 'BTC' →'BTC_USDT'.

    Handles common KOL variants like 'ETH/USDT', 'SOLUSDT', etc."""
    s = symbol.upper().replace("/", "_").replace("-", "_")
    if s.endswith("_USDT"):
        return s
    if s.endswith("USDT") and not s.endswith("_USDT"):
        return s[:-4] + "_USDT"
    return f"{s}_USDT"


def _parse_take_profits(tp_text: str | None) -> list[float]:
    """Parse take-profit text into a sorted list of floats."""
    if not tp_text:
        return []
    try:
        parsed = json.loads(tp_text)
        if isinstance(parsed, list):
            return sorted(float(v) for v in parsed)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return sorted(float(v) for v in re.findall(r"\d+(?:\.\d+)?", tp_text))


def _parse_entry_range_values(entry_text: str | None) -> tuple[float | None, float | None]:
    """Parse '62000-62200' →(62000.0, 62200.0)."""
    if not entry_text:
        return None, None
    values = re.findall(r"\d+(?:\.\d+)?", entry_text)
    if len(values) < 2:
        if values:
            single = float(values[0])
            return single, single
        return None, None
    low, high = float(values[0]), float(values[1])
    if low > high:
        low, high = high, low
    return low, high


def _parse_single_float(text: str | None) -> float | None:
    """Parse '61000' →61000.0."""
    if not text:
        return None
    values = re.findall(r"\d+(?:\.\d+)?", text)
    return float(values[0]) if values else None


def _looks_like_market_entry_text(entry_text: str | None) -> bool:
    if not entry_text:
        return False
    normalized = entry_text.lower()
    return any(
        keyword in normalized
        for keyword in (
            "市价",
            "现价",
            "当前价",
            "地板",
            "直接",
            "立即",
            "马上",
            "市价进",
            "进场灵活",
            "灵活进场",
            "不必踩点",
            "不用踩点",
            "无需踩点",
            "进场零花",
            "附近",
            "左右",
            "一带",
            "around",
            "market",
        )
    )


def _looks_like_flexible_entry_text(entry_text: str | None) -> bool:
    if not entry_text:
        return False
    normalized = entry_text.lower()
    return any(
        keyword in normalized
        for keyword in (
            "进场灵活",
            "灵活进场",
            "不必踩点",
            "不用踩点",
            "无需踩点",
            "进场零花",
        )
    )


def _first_entry_reference_price(entry_text: str | None) -> float | None:
    if not entry_text:
        return None
    values = re.findall(r"\d+(?:\.\d+)?", entry_text)
    return float(values[0]) if values else None


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _before(left: datetime | None, right: datetime | None) -> bool:
    left_value = _utc_naive(left)
    right_value = _utc_naive(right)
    if left_value is None or right_value is None:
        return False
    return left_value < right_value


def _stop_loss_active_at(sig: StrategyLifecycle, candle_at: datetime | None) -> bool:
    management_at = getattr(sig, "_management_event_at", None)
    if management_at is None:
        return True
    candle_value = _utc_naive(candle_at)
    management_value = _utc_naive(management_at)
    if candle_value is None or management_value is None:
        return True
    return candle_value >= management_value


def _load_management_event_time(session, row: StrategyLifecycle) -> datetime | None:
    if row.management_signal_message_id is None:
        return None
    raw_message = (
        session.query(RawMessage)
        .filter(RawMessage.chat_id == row.chat_id)
        .filter(RawMessage.message_id == row.management_signal_message_id)
        .one_or_none()
    )
    return raw_message.posted_at if raw_message is not None else None


def _candle_from_payload(row: dict[str, Any] | list[Any]) -> PriceCandle:
    if isinstance(row, dict):
        opened_at = row.get("t") or row.get("time")
        high = row.get("h") or row.get("high")
        low = row.get("l") or row.get("low")
    else:
        opened_at = row[0]
        high = row[3]
        low = row[4]
    return PriceCandle(
        opened_at=datetime.fromtimestamp(int(opened_at), tz=UTC).replace(tzinfo=None),
        high=float(high),
        low=float(low),
    )


# ── data types ───────────────────────────────────────────────────────


@dataclass(slots=True)
class PriceCandle:
    opened_at: datetime
    high: float
    low: float


@dataclass(slots=True)
class ExitResult:
    reason: str  # stop_loss | take_profit
    price: float


@dataclass(slots=True)
class SignalCheckState:
    signal: StrategyLifecycle
    status: str  # pending_entry | entered | done
    entry_triggered_at: datetime | None = None
    entry_price: float | None = None


@dataclass(slots=True)
class StateTransition:
    signal_id: int
    from_status: str
    to_status: str
    trigger_price: float | None = None
    exit_reason: str | None = None
    occurred_at: datetime | None = None


@dataclass
class LifecycleMonitorConfig:
    cycle_interval_seconds: int = 60
    max_age_hours: int = 3
    max_age_days: int = 7  # how far back to load active signals
    candle_per_page: int = 1000
    http_timeout_seconds: float = 10.0
    market_entry_tolerance_ratio: float = 0.0015


# ── monitor ──────────────────────────────────────────────────────────


class LifecycleMonitor:
    """Scans pending / entered strategies grouped by Gate contract.

    One K-line series per contract is fetched from the oldest *signal_at*
    to now.  Then candles are walked chronologically so that signals are
    only checked after their *signal_at* and entry always precedes exit.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        broker: LiveUpdateBroker,
        *,
        config: LifecycleMonitorConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.gateio.ws",
        settle: str = "usdt",
        now_provider=None,
        expiry_review_notifier: ExpiryReviewNotifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._config = config or LifecycleMonitorConfig()
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=self._config.http_timeout_seconds)
        self._base_url = base_url
        self._settle = settle
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._expiry_review_notifier = expiry_review_notifier

    # ── public API ────────────────────────────────────────────────

    async def run_loop(self) -> None:
        """Background loop started by the FastAPI lifespan."""
        logger.info(
            "LifecycleMonitor started  interval=%ds  max_age=%dh  lookback=%dd",
            self._config.cycle_interval_seconds,
            self._config.max_age_hours,
            self._config.max_age_days,
        )
        # ── Backfill StrategyLifecycle from existing TradeIdeas (once at startup) ──
        try:
            count = self.backfill_from_trade_ideas()
            if count:
                logger.info("Backfilled %d lifecycle records from TradeIdeas", count)
        except Exception:
            logger.exception("Backfill failed, continuing anyway")
        while True:
            try:
                await self._run_one_cycle()
            except asyncio.CancelledError:
                logger.info("LifecycleMonitor cancelled")
                return
            except Exception:
                logger.exception("LifecycleMonitor cycle failed, retrying in %ds",
                                 self._config.cycle_interval_seconds)
            await asyncio.sleep(self._config.cycle_interval_seconds)

    async def run_once(self) -> list[dict[str, Any]]:
        """Run one cycle synchronously (for tests / on-demand refresh)."""
        transitions = await self._run_one_cycle()
        return [
            {
                "signal_id": t.signal_id,
                "from": t.from_status,
                "to": t.to_status,
                "exit_reason": t.exit_reason,
                "trigger_price": t.trigger_price,
                "occurred_at": (t.occurred_at.isoformat() if t.occurred_at else None),
            }
            for t in transitions
        ]

    async def on_new_exit_signal(
        self, *, chat_id: int, symbol: str, side: str, message_id: int
    ) -> bool:
        """Called when a KOL exit signal is recognised.  Matches and closes
        the most recent *entered* lifecycle for the same (chat, symbol, side).
        Returns True when a match was found.
        """
        with self._session_factory() as session:
            matching = (
                session.query(StrategyLifecycle)
                .filter(
                    StrategyLifecycle.chat_id == chat_id,
                    StrategyLifecycle.symbol == symbol.upper(),
                    StrategyLifecycle.side == side.lower(),
                    StrategyLifecycle.lifecycle_status == "entered",
                )
                .order_by(StrategyLifecycle.entered_at.desc())
                .first()
            )
            if matching is None:
                logger.info(
                    "No entered position for exit signal  chat=%s  %s %s",
                    chat_id, symbol, side,
                )
                return False

            if has_live_execution_binding(session, matching):
                record_lifecycle_exit_intent(
                    session,
                    matching,
                    exit_message_id=message_id,
                    reason="KOL exit signal awaiting exchange reconciliation.",
                    updated_at=self._now(),
                )
                session.commit()
                logger.info(
                    "Exit intent recorded for live-bound lifecycle=%s message=%s",
                    matching.id,
                    message_id,
                )
                return True

            matching.lifecycle_status = "exited"
            matching.exit_reason = "kol_signal"
            matching.exited_at = self._now()
            matching.exit_signal_message_id = message_id
            matching.updated_at = self._now()
            session.commit()

            self._push_sse(matching, "entered", "exited")
            logger.info(
                "Exit signal matched  lifecycle=%s  %s %s  @ %s",
                matching.id, matching.symbol, matching.side, matching.exited_at,
            )
            return True

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    # ── backfill ─────────────────────────────────────────────────

    def backfill_from_trade_ideas(self) -> int:
        """Create + fix StrategyLifecycle records.

        1. Fix: entered records with entry_range →pending_entry
        2. Create new records from TradeIdeas without lifecycle
        """
        from telegram_kol_research.models import (
            SignalCandidate, RawMessage, TradeIdea,
        )

        created = 0
        with self._session_factory() as session:
            # ── Fix: entered →pending_entry if no real entry evidence ──
            # Case 1: has entry_range →should be pending (let monitor verify)
            # Case 2: no entry_range AND no stop_loss AND no take_profit →pending
            entered_bad = (
                session.query(StrategyLifecycle)
                .filter(
                    StrategyLifecycle.lifecycle_status == "entered",
                    or_(
                        StrategyLifecycle.entered_at.is_(None),
                        StrategyLifecycle.entry_price_actual.is_(None),
                        and_(
                            StrategyLifecycle.entry_range_low.is_(None),
                            StrategyLifecycle.stop_loss.is_(None),
                            or_(
                                StrategyLifecycle.take_profit.is_(None),
                                StrategyLifecycle.take_profit == "",
                            ),
                        ),
                    ),
                )
                .all()
            )
            fixed = 0
            for lc in entered_bad:
                if _has_live_execution_binding(session, lc.execution_binding_id):
                    continue
                if (
                    lc.entered_at is not None
                    and lc.entry_price_actual is not None
                    and not _before(lc.entered_at, lc.signal_at)
                ):
                    continue
                lc.lifecycle_status = "pending_entry"
                lc.entered_at = None
                lc.entry_price_actual = None
                fixed += 1
            if fixed:
                session.commit()
                logger.info("Fixed %d records: entered→pending_entry", fixed)

            # ── Create new records from orphan TradeIdeas ──
            orphan_trades = (
                session.query(TradeIdea, SignalCandidate, RawMessage)
                .join(SignalCandidate,
                      TradeIdea.primary_signal_candidate_id == SignalCandidate.id)
                .join(RawMessage,
                      SignalCandidate.raw_message_id == RawMessage.id)
                .outerjoin(StrategyLifecycle,
                          StrategyLifecycle.trade_idea_id == TradeIdea.id)
                .filter(TradeIdea.status == "open")
                .filter(StrategyLifecycle.id == None)  # no existing lifecycle
                .all()
            )

            duplicate_marked = 0
            for ti, cand, raw_msg in orphan_trades:
                if not cand.symbol or not cand.side:
                    continue

                # check no duplicate by (chat_id, message_id)
                existing = (
                    session.query(StrategyLifecycle)
                    .filter(
                        StrategyLifecycle.chat_id == raw_msg.chat_id,
                        StrategyLifecycle.message_id == raw_msg.message_id,
                    )
                    .one_or_none()
                )
                if existing is not None:
                    continue

                entry_low, entry_high = _parse_entry_range_values(
                    cand.entry_text
                )
                stop_loss = _parse_single_float(cand.stop_loss_text)
                from telegram_kol_research.message_recognition import (
                    DUPLICATE_ACTIVE_STRATEGY_WINDOW_HOURS,
                    _apply_entry_correction_to_lifecycle,
                    _find_active_lifecycle_entry_correction,
                    _find_duplicate_active_lifecycle,
                )

                duplicate = _find_duplicate_active_lifecycle(
                    session,
                    raw_message=raw_msg,
                    candidate=cand,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    stop_loss=stop_loss,
                    window_hours=DUPLICATE_ACTIVE_STRATEGY_WINDOW_HOURS,
                )
                if duplicate is not None:
                    cand.event_type = "duplicate_entry_signal"
                    cand.review_note = (
                        f"Duplicate active strategy lifecycle #{duplicate.id}; "
                        f"original message #{duplicate.message_id}."
                    )
                    cand.confidence = max(cand.confidence or 0.0, 0.9)
                    ti.status = "duplicate"
                    duplicate_marked += 1
                    continue

                correction = _find_active_lifecycle_entry_correction(
                    session,
                    raw_message=raw_msg,
                    candidate=cand,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    stop_loss=stop_loss,
                    window_hours=DUPLICATE_ACTIVE_STRATEGY_WINDOW_HOURS,
                )
                if correction is not None:
                    _apply_entry_correction_to_lifecycle(
                        correction,
                        raw_message=raw_msg,
                        candidate=cand,
                        entry_low=entry_low,
                        entry_high=entry_high,
                        stop_loss=stop_loss,
                    )
                    ti.status = "duplicate"
                    duplicate_marked += 1
                    continue

                # If there's a parsed entry range, start as pending_entry
                # so the monitor checks candles to confirm actual entry.
                # Always start as pending_entry.
                # Only candle-check or exchange binding can confirm entry.
                init_status = "pending_entry"

                lc = StrategyLifecycle(
                    signal_candidate_id=cand.id,
                    chat_id=raw_msg.chat_id,
                    message_id=raw_msg.message_id,
                    symbol=cand.symbol.upper(),
                    side=cand.side.lower(),
                    lifecycle_status=init_status,
                    signal_at=ti.created_at or utc_now(),
                    entered_at=ti.created_at if init_status == "entered" else None,
                    entry_range_low=entry_low,
                    entry_range_high=entry_high,
                    stop_loss=stop_loss,
                    take_profit=cand.take_profit_text,
                    trade_idea_id=ti.id,
                )
                session.add(lc)
                created += 1

            if created or duplicate_marked:
                session.commit()
            if duplicate_marked:
                logger.info(
                    "Marked %d orphan TradeIdeas as duplicate active strategies",
                    duplicate_marked,
                )
            if created:
                logger.info(
                    "Backfilled %d StrategyLifecycle records from TradeIdeas",
                    created,
                )

        return created

    # ── cycle ──────────────────────────────────────────────────────

    async def _run_one_cycle(self) -> list[StateTransition]:
        now = self._now()
        await self._request_pending_expiry_reviews(now)

        all_signals = self._load_active_signals()
        if not all_signals:
            return []

        by_contract: dict[str, list[StrategyLifecycle]] = {}
        for sig in all_signals:
            contract = _symbol_to_contract(sig.symbol)
            by_contract.setdefault(contract, []).append(sig)

        tasks = [
            self._scan_contract(contract, signals, now)
            for contract, signals in by_contract.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_transitions: list[StateTransition] = []
        for result in results:
            if isinstance(result, Exception):
                logger.exception("Contract scan failed: %s", result)
            else:
                all_transitions.extend(result)

        self._apply_transitions(all_transitions)
        return all_transitions

    # ── DB queries ─────────────────────────────────────────────────

    def _load_active_signals(self) -> list[StrategyLifecycle]:
        cutoff = self._now() - timedelta(days=self._config.max_age_days)
        pending_cutoff = self._now() - timedelta(hours=self._config.max_age_hours)
        with self._session_factory() as session:
            signals = (
                session.query(StrategyLifecycle)
                .filter(
                    or_(
                        StrategyLifecycle.lifecycle_status == "entered",
                        and_(
                            StrategyLifecycle.lifecycle_status == "pending_entry",
                            or_(
                                StrategyLifecycle.signal_at >= pending_cutoff,
                                StrategyLifecycle.management_action == "expiry_review_continued",
                                StrategyLifecycle.management_action == "expiry_review_requested",
                            ),
                        ),
                    ),
                    StrategyLifecycle.signal_at >= cutoff,
                )
                .order_by(StrategyLifecycle.signal_at.asc())
                .all()
            )
            self._attach_management_event_times(session, signals)
            self._attach_entry_texts(session, signals)
            return signals

    async def _request_pending_expiry_reviews(self, now: datetime) -> None:
        if self._expiry_review_notifier is None:
            return
        review_payloads: list[dict[str, Any]] = []
        state_changed = False
        with self._session_factory() as session:
            rows = (
                session.query(StrategyLifecycle)
                .filter(
                    StrategyLifecycle.lifecycle_status.in_(
                        ["pending_entry", "entered"]
                    )
                )
                .all()
            )
            for row in rows:
                management_action = str(row.management_action or "")
                if management_action.startswith("expiry_") and management_action not in {
                    "expiry_review_requested",
                    "expiry_review_continued",
                }:
                    continue
                pending_leg_context = (
                    self._entered_lifecycle_pending_entry_leg_context(session, row)
                    if row.lifecycle_status == "entered"
                    else {}
                )
                if row.lifecycle_status == "entered" and not pending_leg_context:
                    if row.expiry_review_next_at is not None:
                        row.expiry_review_next_at = None
                        state_changed = True
                    continue
                if not self._expiry_review_due(row, now):
                    continue
                continued_review = row.expiry_review_next_at is not None
                expiry_at = self._next_expiry_review_at(row)
                previous_review_at = (
                    row.last_checked_at
                    if continued_review
                    else None
                )
                review_reason = self._expiry_review_reason(
                    row,
                    pending_entry_leg_count=len(
                        pending_leg_context.get("pending_leg_ids", [])
                    ),
                    continued_review=continued_review,
                )
                management_note = (
                    f"{review_reason}，"
                    "需要人工确认继续等待、标记过期或撤销交易所挂单。"
                )
                claimed = self._claim_expiry_review(
                    session,
                    row,
                    now=now,
                    management_note=management_note,
                    continued_review=continued_review,
                    require_pending_leg=row.lifecycle_status == "entered",
                )
                if not claimed:
                    continue
                state_changed = True
                review_payloads.append(
                    {
                        "lifecycle_id": row.id,
                        "lifecycle_status": row.lifecycle_status,
                        "chat_id": row.chat_id,
                        "message_id": row.message_id,
                        "symbol": row.symbol,
                        "side": row.side,
                        "signal_at": row.signal_at,
                        "expiry_at": expiry_at,
                        "max_age_hours": self._config.max_age_hours,
                        "previous_review_at": previous_review_at,
                        "review_reason": review_reason,
                        "entry_range_low": row.entry_range_low,
                        "entry_range_high": row.entry_range_high,
                        "stop_loss": row.stop_loss,
                        "take_profit": row.take_profit,
                        **pending_leg_context,
                    }
                )
            if state_changed:
                session.commit()

        for payload in review_payloads:
            try:
                await self._expiry_review_notifier(payload)
            except Exception:
                logger.exception(
                    "Pending-entry expiry review notifier failed lifecycle_id=%s",
                    payload.get("lifecycle_id"),
                )

    @staticmethod
    def _claim_expiry_review(
        session,
        row: StrategyLifecycle,
        *,
        now: datetime,
        management_note: str,
        continued_review: bool,
        require_pending_leg: bool,
    ) -> bool:
        claim = session.query(StrategyLifecycle).filter(
            StrategyLifecycle.id == row.id,
            StrategyLifecycle.lifecycle_status == row.lifecycle_status,
            StrategyLifecycle.execution_binding_id == row.execution_binding_id,
        )
        if row.management_action is None:
            claim = claim.filter(StrategyLifecycle.management_action.is_(None))
        else:
            claim = claim.filter(
                StrategyLifecycle.management_action == row.management_action
            )
        if continued_review:
            claim = claim.filter(
                StrategyLifecycle.expiry_review_next_at.is_not(None),
                StrategyLifecycle.expiry_review_next_at <= _utc_naive(now),
            )
        else:
            claim = claim.filter(
                StrategyLifecycle.expiry_review_notified_at.is_(None),
                StrategyLifecycle.expiry_review_next_at.is_(None),
            )
        if require_pending_leg:
            pending_leg_exists = exists().where(
                and_(
                    ExecutionOrderLeg.execution_binding_id
                    == StrategyLifecycle.execution_binding_id,
                    ExecutionOrderLeg.purpose == "entry",
                    func.lower(ExecutionOrderLeg.status).in_(
                        ["open", "pending", "submitted"]
                    ),
                    ExecutionOrderLeg.terminal_reason.is_(None),
                    or_(
                        ExecutionOrderLeg.pos_id.is_(None),
                        ExecutionOrderLeg.pos_id == "",
                    ),
                    ~func.lower(
                        func.coalesce(
                            ExecutionOrderLeg.attribution_status,
                            "unassigned",
                        )
                    ).in_(["attribution_conflict", "evidence_unavailable"]),
                )
            )
            claim = claim.filter(pending_leg_exists)
        claimed = claim.update(
            {
                StrategyLifecycle.management_action: "expiry_review_requested",
                StrategyLifecycle.management_note: management_note,
                StrategyLifecycle.last_checked_at: now,
                StrategyLifecycle.updated_at: now,
                StrategyLifecycle.expiry_review_notified_at: now,
                StrategyLifecycle.expiry_review_next_at: None,
            },
            synchronize_session=False,
        )
        return claimed == 1

    @staticmethod
    def _entered_lifecycle_pending_entry_leg_context(
        session, row: StrategyLifecycle
    ) -> dict[str, Any]:
        binding_id = getattr(row, "execution_binding_id", None)
        if binding_id is None:
            return {}
        binding = session.get(ExecutionBinding, binding_id)
        if (
            binding is None
            or binding.chat_id != row.chat_id
            or binding.message_id != row.message_id
        ):
            return {}
        pending_statuses = {
            "open",
            "pending",
            "submitted",
        }
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding.id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .order_by(ExecutionOrderLeg.leg_index.asc(), ExecutionOrderLeg.id.asc())
            .all()
        )
        pending_legs = [
            leg
            for leg in legs
            if str(leg.status or "").lower() in pending_statuses
            and leg.terminal_reason is None
            and not leg.pos_id
            and str(leg.attribution_status or "unassigned")
            not in {"attribution_conflict", "evidence_unavailable"}
        ]
        if not pending_legs:
            return {}
        return {
            "pending_leg_ids": [leg.id for leg in pending_legs],
            "pending_order_ids": [
                str(leg.order_id) for leg in pending_legs if leg.order_id
            ],
        }

    @staticmethod
    def _attach_management_event_times(session, signals: list[StrategyLifecycle]) -> None:
        message_keys = {
            (sig.chat_id, sig.management_signal_message_id)
            for sig in signals
            if sig.management_signal_message_id is not None
        }
        if not message_keys:
            return
        chat_ids = {chat_id for chat_id, _ in message_keys}
        message_ids = {message_id for _, message_id in message_keys}
        rows = (
            session.query(RawMessage)
            .filter(RawMessage.chat_id.in_(chat_ids))
            .filter(RawMessage.message_id.in_(message_ids))
            .all()
        )
        by_key = {(row.chat_id, row.message_id): row.posted_at for row in rows}
        for sig in signals:
            if sig.management_signal_message_id is None:
                continue
            setattr(
                sig,
                "_management_event_at",
                by_key.get((sig.chat_id, sig.management_signal_message_id)),
            )

    @staticmethod
    def _attach_entry_texts(session, signals: list[StrategyLifecycle]) -> None:
        candidate_ids = {
            sig.signal_candidate_id
            for sig in signals
            if sig.signal_candidate_id is not None
        }
        by_id: dict[int, str | None] = {}
        if candidate_ids:
            rows = (
                session.query(SignalCandidate.id, SignalCandidate.entry_text)
                .filter(SignalCandidate.id.in_(candidate_ids))
                .all()
            )
            by_id = {candidate_id: entry_text for candidate_id, entry_text in rows}

        message_keys = {(sig.chat_id, sig.message_id) for sig in signals}
        chat_ids = {chat_id for chat_id, _ in message_keys}
        message_ids = {message_id for _, message_id in message_keys}
        message_rows = (
            session.query(RawMessage)
            .filter(RawMessage.chat_id.in_(chat_ids))
            .filter(RawMessage.message_id.in_(message_ids))
            .all()
            if message_keys
            else []
        )
        message_text_by_key = {
            (row.chat_id, row.message_id): row.text for row in message_rows
        }
        for sig in signals:
            if sig.signal_candidate_id is None:
                setattr(sig, "_entry_text", None)
            else:
                setattr(sig, "_entry_text", by_id.get(sig.signal_candidate_id))
            setattr(
                sig,
                "_original_message_text",
                message_text_by_key.get((sig.chat_id, sig.message_id)),
            )

    # ── contract scan ──────────────────────────────────────────────

    async def _scan_contract(
        self,
        contract: str,
        signals: list[StrategyLifecycle],
        now: datetime,
    ) -> list[StateTransition]:
        if not signals:
            return []

        oldest_signal_at = min(s.signal_at for s in signals)

        candles = await self._fetch_candles_full(contract, oldest_signal_at, now)
        if not candles:
            logger.warning("No candles for %s, skipping", contract)
            return []
        current_price = None
        if any(
            self._looks_like_market_entry_signal(sig)
            for sig in signals
        ):
            current_price = await self._fetch_current_price(contract)

        signals_sorted = sorted(signals, key=lambda s: s.signal_at)

        transitions: list[StateTransition] = []
        signal_index = 0
        active: dict[int, SignalCheckState] = {}

        for c in candles:
            ct = c.opened_at
            ct_compare = _utc_naive(ct)
            if ct_compare is None:
                continue

            # activate signals whose time has come
            while (
                signal_index < len(signals_sorted)
                and _utc_naive(signals_sorted[signal_index].signal_at) <= ct_compare
            ):
                sig = signals_sorted[signal_index]
                active[sig.id] = SignalCheckState(
                    signal=sig,
                    status=sig.lifecycle_status,
                )
                signal_index += 1

            # check every active signal
            for sid, check in list(active.items()):
                if check.status == "done":
                    continue

                sig = check.signal
                if _before(ct, sig.signal_at):
                    continue

                if check.status == "pending_entry":
                    if self._is_expired(sig, ct):
                        check.status = "done"
                        transitions.append(StateTransition(
                            signal_id=sig.id,
                            from_status="pending_entry",
                            to_status="expired",
                            exit_reason="expired",
                            occurred_at=self._expiry_at(sig),
                        ))
                    elif self._entry_triggered(sig, c):
                        check.status = "entered"
                        check.entry_triggered_at = ct
                        check.entry_price = self._resolve_entry_price(sig, c)

                        if not self._has_exit_conditions(sig):
                            check.status = "done"

                        transitions.append(StateTransition(
                            signal_id=sig.id,
                            from_status="pending_entry",
                            to_status="entered",
                            trigger_price=check.entry_price,
                            occurred_at=ct,
                        ))
                    elif self._market_entry_candle_triggered(sig, c):
                        check.status = "entered"
                        check.entry_triggered_at = ct
                        check.entry_price = self._resolve_market_entry_price(sig, c)

                        if not self._has_exit_conditions(sig):
                            check.status = "done"

                        transitions.append(StateTransition(
                            signal_id=sig.id,
                            from_status="pending_entry",
                            to_status="entered",
                            trigger_price=check.entry_price,
                            occurred_at=ct,
                        ))

                elif check.status == "entered":
                    exit_result = self._check_exit(sig, c)
                    if exit_result is not None:
                        check.status = "done"
                        transitions.append(StateTransition(
                            signal_id=sig.id,
                            from_status="entered",
                            to_status="exited",
                            exit_reason=exit_result.reason,
                            trigger_price=exit_result.price,
                            occurred_at=ct,
                        ))

        # after candles: check expiry
        for sig in signals_sorted:
            check = active.get(sig.id)
            if check is None:
                continue
            if check.status == "pending_entry" and self._is_expired(sig, now):
                transitions.append(StateTransition(
                    signal_id=sig.id,
                    from_status="pending_entry",
                    to_status="expired",
                    exit_reason="expired",
                    occurred_at=self._expiry_at(sig),
                ))
                continue
            if (
                check.status == "pending_entry"
                and self._market_entry_close_enough(sig, current_price)
            ):
                check.status = "entered"
                transitions.append(StateTransition(
                    signal_id=sig.id,
                    from_status="pending_entry",
                    to_status="entered",
                    trigger_price=current_price,
                    occurred_at=now,
                ))
                continue
            if check.status == "pending_entry" and self._is_expired(sig, now):
                transitions.append(StateTransition(
                    signal_id=sig.id,
                    from_status="pending_entry",
                    to_status="expired",
                    exit_reason="expired",
                    occurred_at=now,
                ))

        return transitions

    # ── candle fetching ────────────────────────────────────────────

    async def _fetch_candles_full(
        self, contract: str, from_: datetime, to_: datetime
    ) -> list[PriceCandle]:
        """Fetch 1m candles from *from_* to *to_* with auto-pagination."""
        all_candles: list[PriceCandle] = []
        # Ensure naive datetimes are treated as UTC
        _from = from_ if from_.tzinfo is not None else from_.replace(tzinfo=UTC)
        _to = to_ if to_.tzinfo is not None else to_.replace(tzinfo=UTC)
        cursor = int(_from.timestamp())
        end_ts = int(_to.timestamp())

        while cursor < end_ts:
            try:
                response = await self._http.get(
                    f"{self._base_url}/api/v4/futures/{self._settle}/candlesticks",
                    params={
                        "contract": contract,
                        "interval": "1m",
                        "from": cursor,
                        "to": end_ts,
                    },
                    timeout=self._config.http_timeout_seconds,
                )
                response.raise_for_status()
                batch = [_candle_from_payload(row) for row in response.json()]
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    logger.warning(
                        "Invalid Gate.io contract %s (HTTP 400), skipping", contract
                    )
                else:
                    logger.error(
                        "Candle fetch failed for %s: HTTP %s %s",
                        contract, e.response.status_code, e.response.reason_phrase,
                    )
                break
            except Exception:
                logger.exception("Candle fetch failed for %s  from=%s", contract, cursor)
                break

            if not batch:
                break

            all_candles.extend(batch)
            cursor = int(batch[-1].opened_at.timestamp()) + 60
            # Gate returns ~1000 candles max; fewer means we reached end_ts
            if len(batch) < 1000:
                break

        return all_candles

    async def _fetch_current_price(self, contract: str) -> float | None:
        try:
            response = await self._http.get(
                f"{self._base_url}/api/v4/futures/{self._settle}/tickers",
                params={"contract": contract},
                timeout=self._config.http_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            first = payload[0] if isinstance(payload, list) and payload else payload
            if not isinstance(first, dict):
                return None
            price = first.get("last") or first.get("mark_price") or first.get("index_price")
            return float(price) if price not in (None, "") else None
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Current price fetch failed for %s: HTTP %s",
                contract,
                e.response.status_code,
            )
        except Exception:
            logger.exception("Current price fetch failed for %s", contract)
        return None

    # ── entry / exit checks ────────────────────────────────────────

    @staticmethod
    def _entry_triggered(sig: StrategyLifecycle, c: PriceCandle) -> bool:
        if sig.entry_range_low is None or sig.entry_range_high is None:
            return False
        return c.low <= sig.entry_range_high and c.high >= sig.entry_range_low

    @staticmethod
    def _resolve_entry_price(sig: StrategyLifecycle, c: PriceCandle) -> float:
        overlap_low = max(c.low, sig.entry_range_low or 0)
        overlap_high = min(c.high, sig.entry_range_high or 0)
        return (overlap_low + overlap_high) / 2.0

    def _market_entry_close_enough(
        self, sig: StrategyLifecycle, current_price: float | None
    ) -> bool:
        if current_price is None or current_price <= 0:
            return False
        return self._price_interval_overlaps_market_entry(
            sig,
            low=current_price,
            high=current_price,
        )

    def _market_entry_candle_triggered(
        self, sig: StrategyLifecycle, c: PriceCandle
    ) -> bool:
        return self._price_interval_overlaps_market_entry(sig, low=c.low, high=c.high)

    def _price_interval_overlaps_market_entry(
        self,
        sig: StrategyLifecycle,
        *,
        low: float,
        high: float,
    ) -> bool:
        if not self._looks_like_market_entry_signal(sig):
            return False
        original_text = getattr(sig, "_original_message_text", None)
        entry_text = getattr(sig, "_entry_text", None)
        combined_text = " ".join(str(part or "") for part in (entry_text, original_text))
        if (
            _looks_like_flexible_entry_text(combined_text)
            and sig.entry_range_low is not None
            and sig.entry_range_high is not None
        ):
            lower, upper = sorted((sig.entry_range_low, sig.entry_range_high))
            tolerance = min(abs(lower), abs(upper)) * self._config.market_entry_tolerance_ratio
            return low <= upper + tolerance and high >= lower - tolerance
        reference_price = _first_entry_reference_price(entry_text)
        if reference_price is None:
            reference_price = _first_entry_reference_price(original_text)
        if reference_price is None or reference_price <= 0:
            return False
        tolerance = abs(reference_price) * self._config.market_entry_tolerance_ratio
        return low <= reference_price + tolerance and high >= reference_price - tolerance

    def _resolve_market_entry_price(self, sig: StrategyLifecycle, c: PriceCandle) -> float:
        original_text = getattr(sig, "_original_message_text", None)
        entry_text = getattr(sig, "_entry_text", None)
        combined_text = " ".join(str(part or "") for part in (entry_text, original_text))
        if (
            _looks_like_flexible_entry_text(combined_text)
            and sig.entry_range_low is not None
            and sig.entry_range_high is not None
        ):
            lower, upper = sorted((sig.entry_range_low, sig.entry_range_high))
            tolerance = min(abs(lower), abs(upper)) * self._config.market_entry_tolerance_ratio
            overlap_low = max(c.low, lower - tolerance)
            overlap_high = min(c.high, upper + tolerance)
            return (overlap_low + overlap_high) / 2.0
        reference_price = _first_entry_reference_price(entry_text)
        if reference_price is None:
            reference_price = _first_entry_reference_price(original_text)
        if reference_price is None:
            return (c.low + c.high) / 2.0
        tolerance = abs(reference_price) * self._config.market_entry_tolerance_ratio
        overlap_low = max(c.low, reference_price - tolerance)
        overlap_high = min(c.high, reference_price + tolerance)
        return (overlap_low + overlap_high) / 2.0

    @staticmethod
    def _looks_like_market_entry_signal(sig: StrategyLifecycle) -> bool:
        return _looks_like_market_entry_text(
            " ".join(
                str(part or "")
                for part in (
                    getattr(sig, "_entry_text", None),
                    getattr(sig, "_original_message_text", None),
                )
            )
        )

    @staticmethod
    def _has_exit_conditions(sig: StrategyLifecycle) -> bool:
        return sig.stop_loss is not None or bool(sig.take_profit)

    def _check_exit(self, sig: StrategyLifecycle, c: PriceCandle) -> ExitResult | None:
        # stop loss first
        if sig.stop_loss is not None and _stop_loss_active_at(sig, c.opened_at):
            if sig.side == "long" and c.low <= sig.stop_loss:
                return ExitResult(reason="stop_loss", price=sig.stop_loss)
            if sig.side == "short" and c.high >= sig.stop_loss:
                return ExitResult(reason="stop_loss", price=sig.stop_loss)

        # take profit (multi-level)
        tp_levels = _parse_take_profits(sig.take_profit)
        if tp_levels:
            for i in range(sig.filled_tp_index + 1, len(tp_levels)):
                tp = tp_levels[i]
                hit = False
                if sig.side == "long" and c.high >= tp:
                    hit = True
                elif sig.side == "short" and c.low <= tp:
                    hit = True
                if hit:
                    sig.filled_tp_index = i
                    if i == len(tp_levels) - 1:
                        return ExitResult(reason="take_profit", price=tp)
                    break

        return None

    # ── expiry ─────────────────────────────────────────────────────

    def _is_expired(self, sig: StrategyLifecycle, now: datetime) -> bool:
        if getattr(sig, "management_action", None) in {
            "expiry_review_continued",
            "expiry_review_requested",
        }:
            return False
        expiry_at = self._expiry_at(sig)
        return not _before(now, expiry_at)

    def _expiry_at(self, sig: StrategyLifecycle) -> datetime:
        signal_at = sig.signal_at
        if signal_at.tzinfo is None:
            signal_at = signal_at.replace(tzinfo=UTC)
        return signal_at + timedelta(hours=self._config.max_age_hours)

    def _expiry_review_due(self, sig: StrategyLifecycle, now: datetime) -> bool:
        next_review_at = getattr(sig, "expiry_review_next_at", None)
        if next_review_at is None:
            if getattr(sig, "expiry_review_notified_at", None) is not None:
                return False
            if getattr(sig, "management_action", None) == "expiry_review_continued":
                return False
        review_at = self._next_expiry_review_at(sig)
        return not _before(now, review_at)

    def _next_expiry_review_at(self, sig: StrategyLifecycle) -> datetime:
        next_review_at = getattr(sig, "expiry_review_next_at", None)
        if next_review_at is not None:
            if next_review_at.tzinfo is None:
                return next_review_at.replace(tzinfo=UTC)
            return next_review_at
        return self._expiry_at(sig)

    def _expiry_review_reason(
        self,
        sig: StrategyLifecycle,
        *,
        pending_entry_leg_count: int = 0,
        continued_review: bool = False,
    ) -> str:
        if continued_review:
            return f"上次人工选择继续等待后又超过 {self._config.max_age_hours} 小时"
        if pending_entry_leg_count:
            return (
                f"已入场策略仍有 {pending_entry_leg_count} 条入场腿超过 "
                f"{self._config.max_age_hours} 小时未触发"
            )
        return f"待入场策略已超过 {self._config.max_age_hours} 小时"

    # ── persistence ────────────────────────────────────────────────

    def _apply_transitions(self, transitions: list[StateTransition]) -> None:
        if not transitions:
            return

        with self._session_factory() as session:
            for t in transitions:
                row = session.get(StrategyLifecycle, t.signal_id)
                if row is None:
                    continue

                # idempotency guard
                if row.lifecycle_status != t.from_status:
                    continue
                if t.to_status in ("exited", "expired", "invalidated") and _has_live_execution_binding(
                    session,
                    row.execution_binding_id,
                ):
                    logger.info(
                        "Skipping simulated lifecycle exit for live execution binding: lifecycle_id=%s binding_id=%s to_status=%s reason=%s",
                        row.id,
                        row.execution_binding_id,
                        t.to_status,
                        t.exit_reason,
                    )
                    continue
                if t.to_status == "entered" and _before(t.occurred_at, row.signal_at):
                    logger.warning(
                        "Skipping impossible entry transition: lifecycle_id=%s signal_at=%s occurred_at=%s",
                        row.id,
                        row.signal_at,
                        t.occurred_at,
                    )
                    continue
                if t.to_status in ("exited", "expired", "invalidated"):
                    reference_at = row.entered_at if row.entered_at is not None else row.signal_at
                    if t.exit_reason == "stop_loss":
                        management_at = _load_management_event_time(session, row)
                        if management_at is not None:
                            reference_at = max(_utc_naive(reference_at), _utc_naive(management_at))
                    if _before(t.occurred_at, reference_at):
                        logger.warning(
                            "Skipping impossible exit transition: lifecycle_id=%s reference_at=%s occurred_at=%s",
                            row.id,
                            reference_at,
                            t.occurred_at,
                        )
                        continue

                row.lifecycle_status = t.to_status
                row.last_checked_at = t.occurred_at or self._now()

                if t.to_status == "entered":
                    row.entered_at = t.occurred_at
                    if t.trigger_price is not None:
                        row.entry_price_actual = t.trigger_price

                elif t.to_status in ("exited", "expired", "invalidated"):
                    row.exited_at = t.occurred_at
                    row.exit_reason = row.exit_reason or t.exit_reason
                    if t.trigger_price is not None:
                        row.exit_price_actual = t.trigger_price

                    # close linked TradeIdea
                    if row.trade_idea_id is not None:
                        trade_idea = session.get(TradeIdea, row.trade_idea_id)
                        if trade_idea is not None and trade_idea.status == "open":
                            trade_idea.status = "closed"
                            trade_idea.closed_at = row.exited_at

                row.updated_at = self._now()
                session.add(row)

            session.commit()

        # SSE push (outside transaction so it sees committed state)
        for t in transitions:
            # re-read for the SSE payload
            with self._session_factory() as session:
                row = session.get(StrategyLifecycle, t.signal_id)
                if row is None:
                    continue
                self._push_sse(row, t.from_status, t.to_status)

    def _push_sse(
        self,
        row: StrategyLifecycle,
        from_status: str,
        to_status: str,
    ) -> None:
        self._broker.publish_event(
            event_type="lifecycle_status_changed",
            payload={
                "lifecycle_id": row.id,
                "symbol": row.symbol,
                "side": row.side,
                "chat_id": row.chat_id,
                "message_id": row.message_id,
                "from_status": from_status,
                "to_status": to_status,
                "exit_reason": row.exit_reason,
                "entry_price_actual": row.entry_price_actual,
                "exit_price_actual": row.exit_price_actual,
                "occurred_at": (row.updated_at.isoformat() if row.updated_at else None),
            },
        )


def _has_live_execution_binding(session, binding_id: int | None) -> bool:
    if binding_id is None:
        return False
    binding = session.get(ExecutionBinding, binding_id)
    if binding is None:
        return False
    return binding.status in {"open", "active"}

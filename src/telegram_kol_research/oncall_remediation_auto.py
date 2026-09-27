"""Phase-4 deterministic auto-execution gates (G-D).

See docs/plans/2026-09-27-codex-oncall-phase4-auto-remediation-spec.md
section 4. Every check here is a pure function over data already read by the
caller (or read here through an indexed/primary-key lookup); nothing writes
to the exchange, and an inconclusive read always fails the check closed.

This module is deliberately standalone (it does not import
``oncall_remediation.py``, to avoid a cycle -- that module imports *this*
one) and depends only on ``models``/``config``/``position_management_remediation``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time as dtime, timedelta
from typing import Any, Callable

from sqlalchemy import func, or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.config import OncallRemediationConfig
from telegram_kol_research.models import (
    ExecutionBinding,
    MessageInstructionItem,
    OncallRemediationControl,
    OncallRemediationProposal,
    RawMessage,
    RuntimeIncident,
    SignalCandidate,
    StrategyManagementBatch,
)
from telegram_kol_research.position_management_remediation import (
    PositionRemediationAction,
)
from telegram_kol_research.protection_ledger import load_account_protection_ownership


_BEIJING_OFFSET = timedelta(hours=8)


def _naive_utc(value: datetime) -> datetime:
    """Same convention as ``oncall_remediation._naive_utc`` (duplicated here
    to keep this module import-free of it -- see module docstring)."""

    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _beijing_day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    beijing_now = now + _BEIJING_OFFSET
    start_beijing = datetime.combine(beijing_now.date(), dtime.min)
    end_beijing = start_beijing + timedelta(days=1)
    return start_beijing - _BEIJING_OFFSET, end_beijing - _BEIJING_OFFSET


# ---------------------------------------------------------------------------
# D1: transient-reason whitelist (spec section 4, row D1)
# ---------------------------------------------------------------------------

#: The five "transient" reason families the user ruling (section 11 item 8)
#: allows as a starting set. Any other reason -- known or unknown -- refuses
#: auto-execution and falls back to the approve-mode button (never a bare
#: rejection). Matched as a substring against the JSON blobs below, the same
#: convention ``oncall_remediation.A7_IRREVERSIBLE_REASON_PATTERNS`` already
#: uses for the mirror-image (irreversible) check, because both read the same
#: machine-generated reason strings, not free text.
D1_TRANSIENT_REASON_PATTERNS = (
    "prior_partial_batch_unresolved",
    "target_strategy_binding_visibility_retry_expired",
    "exchange_snapshot_incomplete",
    "close_final_preflight_failed",
    "protection_missing_cancellable_order_id",
)


@dataclass(frozen=True, slots=True)
class GateDCheck:
    check: str
    passed: bool
    actual: Any
    threshold: Any
    reason_code: str | None


def _extract_reasons_from_blob(blob: str | None) -> set[str]:
    """Pull every ``"reason"``/``"reason_code"`` value out of a JSON blob.

    ``message_instruction_items.py:342/401`` writes ``{"reason": <code>}``
    into ``error_json``; other call sites use ``reason_code``. Falls back to
    treating the raw text as a single "reason" when it does not parse as
    JSON, so a bare reason-code string is not silently ignored.
    """

    if not blob:
        return set()
    try:
        payload = json.loads(blob)
    except (TypeError, ValueError):
        return {str(blob)}
    reasons: set[str] = set()
    if isinstance(payload, dict):
        for key in ("reason", "reason_code"):
            value = payload.get(key)
            if value:
                reasons.add(str(value))
    if not reasons:
        reasons.add(str(blob))
    return reasons


def check_d1_reason_whitelist(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    instruction_item_id: int | None,
) -> GateDCheck:
    """D1: every collected reason must be transient, and there must be >= 1."""

    reasons: set[str] = set()
    with session_factory() as session:
        if instruction_item_id is not None:
            item = session.get(MessageInstructionItem, int(instruction_item_id))
            if item is not None:
                reasons |= _extract_reasons_from_blob(item.error_json)
                reasons |= _extract_reasons_from_blob(item.result_json)
        batch_reason_codes = (
            session.query(StrategyManagementBatch.reason_code)
            .filter(StrategyManagementBatch.raw_message_id == raw_message_id)
            .all()
        )
        for (reason_code,) in batch_reason_codes:
            if reason_code:
                reasons.add(str(reason_code))

    def _is_transient(reason: str) -> bool:
        return any(pattern in reason for pattern in D1_TRANSIENT_REASON_PATTERNS)

    if not reasons:
        return GateDCheck("D1", False, "no_reason_found", D1_TRANSIENT_REASON_PATTERNS, "d1_reason_not_transient")
    non_transient = sorted(r for r in reasons if not _is_transient(r))
    if non_transient:
        return GateDCheck(
            "D1", False, non_transient, D1_TRANSIENT_REASON_PATTERNS, "d1_reason_not_transient"
        )
    return GateDCheck("D1", True, sorted(reasons), D1_TRANSIENT_REASON_PATTERNS, None)


# ---------------------------------------------------------------------------
# D2: no successor message (spec section 4, row D2)
# ---------------------------------------------------------------------------


def check_d2_no_successor_message(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    lifecycle_id: int | None,
    strategy_instance_id: str | None,
    posted_at: datetime,
) -> GateDCheck:
    """D2: no later management candidate/batch exists for this target.

    Self and any ``review_status='approved_remediation'`` projection
    candidate (the remediation flow's own projected candidate) are excluded --
    see ``position_management_remediation._project_canonical_remediation_candidate``.
    """

    posted_at = _naive_utc(posted_at)
    with session_factory() as session:
        successor_candidate = None
        if lifecycle_id is not None:
            successor_candidate = (
                session.query(SignalCandidate.id)
                .join(RawMessage, RawMessage.id == SignalCandidate.raw_message_id)
                .filter(
                    SignalCandidate.target_lifecycle_id == lifecycle_id,
                    SignalCandidate.raw_message_id != raw_message_id,
                    SignalCandidate.review_status != "approved_remediation",
                    RawMessage.posted_at > posted_at,
                )
                .first()
            )
        successor_batch = None
        if strategy_instance_id is not None:
            successor_batch = (
                session.query(StrategyManagementBatch.id)
                .join(RawMessage, RawMessage.id == StrategyManagementBatch.raw_message_id)
                .filter(
                    StrategyManagementBatch.strategy_instance_id == strategy_instance_id,
                    StrategyManagementBatch.raw_message_id != raw_message_id,
                    RawMessage.posted_at > posted_at,
                )
                .first()
            )
    hit = successor_candidate is not None or successor_batch is not None
    return GateDCheck(
        "D2",
        not hit,
        {"candidate": successor_candidate is not None, "batch": successor_batch is not None},
        "no_successor",
        "d2_successor_message" if hit else None,
    )


# ---------------------------------------------------------------------------
# D3: position untouched since the message was posted (spec section 4, row D3)
# ---------------------------------------------------------------------------

#: The only classification ``classify_current_position_protection_health``
#: returns that means "verified fine" -- see protection_health.py:26-31/259.
#: ``recovery_required``/``evidence_insufficient`` are both non-healthy.
D3_HEALTHY_PROTECTION_CLASSIFICATIONS = frozenset({"healthy_current_evidence"})

#: ExecutionBinding.last_exchange_status values written by a
#: reconciliation sweep that concluded a position closed *without* this
#: repository's own management path having done it -- see
#: execution_bindings.py:4505-4507 (``sync_manual_closed_deepcoin_positions``).
#: A binding carrying one of these, updated after the message was posted, is
#: exactly "someone/something moved this outside the remediation path".
D3_EXTERNAL_CHANGE_EXCHANGE_STATUSES = frozenset({"manual_closed_or_not_found_on_exchange"})


def check_d3_position_untouched(
    session_factory: sessionmaker,
    *,
    action: PositionRemediationAction,
    posted_at: datetime,
) -> GateDCheck:
    """D3, three sub-checks, all DB-only (no fresh exchange call):

    (a) every ``evidence["protection_health"]`` entry (present only when the
        planner found a prior protection incident for that leg) must classify
        ``healthy_current_evidence``; absent entirely counts as pass (nothing
        to flag) -- see position_management_remediation.py:891-921.
    (b) every ``action.pos_ids`` must have >= 1 owned protection order in the
        canonical ledger (``load_account_protection_ownership``), unless the
        action itself is ``full_exit`` (no protection is expected to survive
        a full close it is about to submit).
    (c) the action's ``execution_binding_id`` (evidence) must not have been
        marked externally-closed after the message posted.

    This is intentionally a DB-only proxy for "unchanged since the message",
    not a second live exchange snapshot: G-A/G-C already compare a fresh
    snapshot's fingerprint byte-for-byte against the one the proposal froze,
    which is the check that actually re-reads the exchange. D3 exists to
    catch a *ledger-visible* human intervention that a byte-identical
    snapshot fingerprint would not by itself explain.
    """

    posted_at = _naive_utc(posted_at)
    reasons: list[str] = []
    actual: dict[str, Any] = {}

    protection_health = action.evidence.get("protection_health") or []
    unhealthy = [
        row for row in protection_health
        if row.get("classification") not in D3_HEALTHY_PROTECTION_CLASSIFICATIONS
    ]
    actual["protection_health_unhealthy"] = unhealthy
    if unhealthy:
        reasons.append("d3_protection_unhealthy")

    binding_id = action.evidence.get("execution_binding_id")
    with session_factory() as session:
        ownership = load_account_protection_ownership(session, venue="deepcoin")
        if action.action_kind != "full_exit":
            missing = [
                pos_id
                for pos_id in action.pos_ids
                if not ownership.orders_for_position(pos_id)
            ]
            actual["pos_ids_without_owned_protection"] = missing
            if missing:
                reasons.append("d3_protection_ownership_gap")

        externally_changed = False
        if binding_id is not None:
            binding = session.get(ExecutionBinding, int(binding_id))
            if (
                binding is not None
                and binding.last_exchange_status in D3_EXTERNAL_CHANGE_EXCHANGE_STATUSES
                and _naive_utc(binding.updated_at) > posted_at
            ):
                externally_changed = True
        actual["execution_binding_externally_changed"] = externally_changed
        if externally_changed:
            reasons.append("d3_position_externally_changed")

    if reasons:
        return GateDCheck("D3", False, actual, "unchanged_since_posted", reasons[0])
    return GateDCheck("D3", True, actual, "unchanged_since_posted", None)


# ---------------------------------------------------------------------------
# D4: stop distance (spec section 4, row D4) -- adjust_stop_loss only
# ---------------------------------------------------------------------------


def check_d4_stop_distance(
    *,
    action: PositionRemediationAction,
    deepcoin_client,
    settings,
    now: datetime,
) -> GateDCheck:
    """D4: applies only to ``adjust_stop_loss``; every other action passes
    vacuously (spec table only lists it for stop-tightening actions)."""

    if action.action_kind != "adjust_stop_loss":
        return GateDCheck("D4", True, "not_applicable", None, None)

    instruments = action.evidence.get("instrument_scope") or []
    instrument_id = str(instruments[0]) if instruments else None
    stop_price = action.expected_effect.get("stop_loss")
    positions = action.evidence.get("positions") or []
    side = None
    if positions and isinstance(positions[0], dict):
        side = positions[0].get("posSide") or positions[0].get("pos_side")

    if not instrument_id or stop_price in (None, "") or side is None:
        return GateDCheck("D4", False, "missing_inputs", None, "d4_quote_unavailable")

    try:
        stop = float(stop_price)
    except (TypeError, ValueError):
        return GateDCheck("D4", False, "invalid_stop_price", None, "d4_quote_unavailable")

    try:
        quote = deepcoin_client.get_ticker_quote(inst_id=instrument_id)
    except Exception:  # noqa: BLE001 - a quote failure fails this check closed
        quote = None
    if not isinstance(quote, dict):
        return GateDCheck("D4", False, "no_quote", None, "d4_quote_unavailable")

    try:
        price = float(quote.get("price"))
    except (TypeError, ValueError):
        return GateDCheck("D4", False, "invalid_quote_price", None, "d4_quote_unavailable")

    max_age = getattr(settings, "management_stop_quote_max_age_seconds", None)
    try:
        observed_at = datetime.fromisoformat(str(quote.get("observed_at")))
        age_seconds = (
            (now.replace(tzinfo=UTC) if now.tzinfo is None else now) - observed_at
        ).total_seconds()
    except (TypeError, ValueError):
        age_seconds = -1
    if max_age is None or age_seconds < 0 or age_seconds > float(max_age):
        return GateDCheck(
            "D4", False, {"age_seconds": age_seconds}, max_age, "d4_quote_unavailable"
        )

    normalized_side = str(side or "").strip().lower()
    if normalized_side in {"long", "buy"}:
        direction_ok = stop < price
    elif normalized_side in {"short", "sell"}:
        direction_ok = stop > price
    else:
        return GateDCheck("D4", False, "unknown_side", None, "d4_stop_direction_invalid")
    if not direction_ok:
        return GateDCheck(
            "D4", False, {"stop": stop, "price": price, "side": normalized_side}, "correct_side",
            "d4_stop_direction_invalid",
        )

    distance_pct = abs(stop - price) / price * 100.0
    threshold = None  # filled by caller (config value); kept generic here
    return GateDCheck("D4", True, distance_pct, None, None)


def check_d4_stop_distance_with_threshold(
    *, action: PositionRemediationAction, deepcoin_client, settings, now: datetime, min_distance_pct: float
) -> GateDCheck:
    result = check_d4_stop_distance(
        action=action, deepcoin_client=deepcoin_client, settings=settings, now=now
    )
    if not result.passed or result.actual in ("not_applicable",):
        return result
    if isinstance(result.actual, (int, float)) and result.actual < min_distance_pct:
        return GateDCheck(
            "D4", False, result.actual, min_distance_pct, "d4_stop_distance_too_close"
        )
    return GateDCheck("D4", True, result.actual, min_distance_pct, None)


# ---------------------------------------------------------------------------
# D5: break-even branch prediction (spec section 4 row D5; not a gate)
# ---------------------------------------------------------------------------


def predict_break_even_branch(action: PositionRemediationAction) -> str:
    """D5 never blocks (user ruling, section 11 item 5): this only records
    which branch the planner is likely to take, for the audit/notification.

    The real decision (``reserve_break_even_market_actions``) only runs once
    a batch is already ``executing`` and calls the exchange client itself, so
    it cannot be replayed read-only ahead of execution without either a
    second live exchange call or mutating batch state early -- both out of
    scope for a gate. This always returns ``unknown_until_execution``;
    ``finalize_executing_proposals`` records the branch actually taken.
    """

    if action.action_kind != "move_stop_to_break_even":
        return "not_applicable"
    return "unknown_until_execution"


# ---------------------------------------------------------------------------
# D6: system health (spec section 4, row D6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutoHealthInputs:
    process_started_at: datetime
    recent_loop_stall: bool = False


def check_d6_system_health(
    session_factory: sessionmaker,
    *,
    health: AutoHealthInputs,
    now: datetime,
    min_uptime_minutes: float,
    lookback_minutes: int,
) -> GateDCheck:
    now = _naive_utc(now)
    started_at = _naive_utc(health.process_started_at)
    uptime_minutes = (now - started_at).total_seconds() / 60.0
    if uptime_minutes < min_uptime_minutes:
        return GateDCheck("D6", False, uptime_minutes, min_uptime_minutes, "d6_process_uptime_too_short")
    if health.recent_loop_stall:
        return GateDCheck("D6", False, "recent_loop_stall", False, "d6_recent_loop_stall")

    # No index exists on (incident_type, last_occurred_at); rather than a
    # full-table SCAN, take the most recent N rows by primary key (which is
    # insertion order, i.e. chronological) and filter in Python -- the same
    # fallback the spec calls for. N is generously large relative to this
    # table's expected write rate.
    lookback_start = now - timedelta(minutes=lookback_minutes)
    with session_factory() as session:
        recent_rows = (
            session.query(RuntimeIncident.incident_type, RuntimeIncident.last_occurred_at)
            .order_by(RuntimeIncident.id.desc())
            .limit(200)
            .all()
        )
    severe_hit = any(
        incident_type == "severe_protection_incident" and _naive_utc(last_occurred_at) >= lookback_start
        for incident_type, last_occurred_at in recent_rows
    )
    if severe_hit:
        return GateDCheck("D6", False, "severe_protection_incident", False, "d6_severe_protection_incident")
    return GateDCheck("D6", True, uptime_minutes, min_uptime_minutes, None)


# ---------------------------------------------------------------------------
# D7: auto time window (spec section 4, row D7)
# ---------------------------------------------------------------------------


def _auto_window_minutes_for(action_kind: str | None, config: OncallRemediationConfig) -> int:
    if action_kind == "full_exit":
        return config.auto_exit_window_minutes
    if action_kind == "partial_take_profit":
        return config.auto_partial_tp_window_minutes
    if action_kind == "move_stop_to_break_even":
        return config.auto_break_even_window_minutes
    if action_kind == "adjust_stop_loss":
        return config.auto_stop_window_minutes
    return 0


def check_d7_auto_window(
    *, action_kind: str | None, posted_at: datetime, now: datetime, config: OncallRemediationConfig
) -> GateDCheck:
    window_minutes = _auto_window_minutes_for(action_kind, config)
    posted_at = _naive_utc(posted_at)
    now = _naive_utc(now)
    elapsed = (now - posted_at).total_seconds() / 60.0
    if elapsed > window_minutes:
        return GateDCheck("D7", False, elapsed, window_minutes, "d7_auto_window_expired")
    return GateDCheck("D7", True, elapsed, window_minutes, None)


# ---------------------------------------------------------------------------
# D8: auto limits (spec section 4, row D8)
# ---------------------------------------------------------------------------

#: Mirrors ``oncall_remediation._REAL_EXECUTION_PREDICATE``: only count a
#: proposal that actually reached (or is still in) the exchange-write path.
def _real_auto_execution_predicate():
    return or_(
        OncallRemediationProposal.state.in_(("executing", "succeeded", "uncertain")),
        OncallRemediationProposal.management_batch_id.is_not(None),
    )


def check_d8_auto_limits(
    session_factory: sessionmaker,
    *,
    lifecycle_id: int | None,
    chat_id: int | None,
    now: datetime,
    config: OncallRemediationConfig,
    exclude_proposal_id: int | None,
) -> GateDCheck:
    now = _naive_utc(now)
    day_start, day_end = _beijing_day_bounds_utc(now)
    with session_factory() as session:
        base_query = session.query(OncallRemediationProposal).filter(
            OncallRemediationProposal.execution_origin == "auto",
            _real_auto_execution_predicate(),
            OncallRemediationProposal.executing_at.is_not(None),
        )
        if exclude_proposal_id is not None:
            base_query = base_query.filter(OncallRemediationProposal.id != exclude_proposal_id)

        today_count = (
            base_query.filter(
                OncallRemediationProposal.executing_at >= day_start,
                OncallRemediationProposal.executing_at < day_end,
            )
            .count()
        )
        if today_count >= config.auto_daily_cap:
            return GateDCheck("D8", False, today_count, config.auto_daily_cap, "d8_auto_daily_cap")

        if chat_id is not None:
            chat_count = (
                base_query.join(RawMessage, RawMessage.id == OncallRemediationProposal.raw_message_id)
                .filter(
                    RawMessage.chat_id == chat_id,
                    OncallRemediationProposal.executing_at >= day_start,
                    OncallRemediationProposal.executing_at < day_end,
                )
                .count()
            )
            if chat_count >= config.auto_per_chat_daily_cap:
                return GateDCheck(
                    "D8", False, chat_count, config.auto_per_chat_daily_cap, "d8_auto_per_chat_daily_cap"
                )

        if lifecycle_id is not None:
            last_executing_at = (
                base_query.filter(OncallRemediationProposal.lifecycle_id == lifecycle_id)
                .with_entities(func.max(OncallRemediationProposal.executing_at))
                .scalar()
            )
            if last_executing_at is not None:
                since_minutes = (now - _naive_utc(last_executing_at)).total_seconds() / 60.0
                if since_minutes < config.auto_cooldown_minutes:
                    return GateDCheck(
                        "D8", False, since_minutes, config.auto_cooldown_minutes, "d8_auto_cooldown"
                    )

    return GateDCheck("D8", True, {"today": today_count}, None, None)


# ---------------------------------------------------------------------------
# D9: action enabled (spec section 4, row D9)
# ---------------------------------------------------------------------------


def check_d9_action_enabled(*, action_kind: str | None, config: OncallRemediationConfig) -> GateDCheck:
    enabled = bool(action_kind) and action_kind in config.auto_actions
    return GateDCheck("D9", enabled, action_kind, sorted(config.auto_actions), None if enabled else "d9_action_not_enabled")


# ---------------------------------------------------------------------------
# Control-layer check: total kill switch / auto suspension
# ---------------------------------------------------------------------------


def check_auto_not_suspended(session_factory: sessionmaker) -> GateDCheck:
    with session_factory() as session:
        control = session.get(OncallRemediationControl, 1)
        suspended = bool(control.auto_suspended) if control is not None else False
    if suspended:
        return GateDCheck("D0", False, True, False, "auto_suspended")
    return GateDCheck("D0", True, False, False, None)


# ---------------------------------------------------------------------------
# Aggregate runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateDOutcome:
    passed: bool
    checks: tuple[GateDCheck, ...]
    first_failure_reason: str | None
    break_even_branch_prediction: str


def run_gate_d(
    session_factory: sessionmaker,
    *,
    config: OncallRemediationConfig,
    action: PositionRemediationAction,
    posted_at: datetime,
    now: datetime,
    deepcoin_client,
    settings,
    health: AutoHealthInputs,
    chat_id: int | None,
    exclude_proposal_id: int | None,
) -> GateDOutcome:
    """Run D0/D1/D2/D3/D4/D6/D7/D8/D9 in a fixed order and stop at the first
    failure (fail-fast; every check is still cheap and side-effect-free, so
    early-exit only saves the later, more expensive DB reads)."""

    checks: list[GateDCheck] = []

    def _run(fn: Callable[[], GateDCheck]) -> GateDCheck:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - fail closed on any internal error
            result = GateDCheck(
                "D?", False, f"internal_error:{type(exc).__name__}", None, "d_internal_error"
            )
        checks.append(result)
        return result

    order: tuple[Callable[[], GateDCheck], ...] = (
        lambda: check_d9_action_enabled(action_kind=action.action_kind, config=config),
        lambda: check_auto_not_suspended(session_factory),
        lambda: check_d1_reason_whitelist(
            session_factory,
            raw_message_id=action.raw_message_id,
            instruction_item_id=action.evidence.get("instruction_item_id"),
        ),
        lambda: check_d2_no_successor_message(
            session_factory,
            raw_message_id=action.raw_message_id,
            lifecycle_id=action.lifecycle_id,
            strategy_instance_id=action.strategy_instance_id,
            posted_at=posted_at,
        ),
        lambda: check_d3_position_untouched(session_factory, action=action, posted_at=posted_at),
        lambda: check_d4_stop_distance_with_threshold(
            action=action,
            deepcoin_client=deepcoin_client,
            settings=settings,
            now=now,
            min_distance_pct=config.auto_min_stop_distance_pct,
        ),
        lambda: check_d6_system_health(
            session_factory,
            health=health,
            now=now,
            min_uptime_minutes=config.auto_min_process_uptime_minutes,
            lookback_minutes=config.auto_health_lookback_minutes,
        ),
        lambda: check_d7_auto_window(action_kind=action.action_kind, posted_at=posted_at, now=now, config=config),
        lambda: check_d8_auto_limits(
            session_factory,
            lifecycle_id=action.lifecycle_id,
            chat_id=chat_id,
            now=now,
            config=config,
            exclude_proposal_id=exclude_proposal_id,
        ),
    )

    first_failure: str | None = None
    for step in order:
        result = _run(step)
        if not result.passed and first_failure is None:
            first_failure = result.reason_code

    return GateDOutcome(
        passed=first_failure is None,
        checks=tuple(checks),
        first_failure_reason=first_failure,
        break_even_branch_prediction=predict_break_even_branch(action),
    )


# ---------------------------------------------------------------------------
# Redaction (copied from oncall_codex.py -- see module docstring in that file
# and the phase-4 spec section 4 "脱敏正则的来源": that module cannot be
# imported here without pulling in the Codex-runner dependency surface this
# module must stay free of, so the three regexes are duplicated verbatim and
# a test asserts byte-for-byte equality against oncall_codex's copies.)
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"
BOT_TOKEN_RE = re.compile(r"\d{8,}:[A-Za-z0-9_-]{30,}")
KEYED_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|passphrase|token|authorization)(\s*[=:]\s*)"
    r"(?!\[REDACTED\])(\S+)"
)
LONG_OPAQUE_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])"
)


def redact(value: str) -> tuple[str, int]:
    text = str(value)
    hits = 0
    text, count = KEYED_SECRET_RE.subn(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text
    )
    hits += count
    text, count = BOT_TOKEN_RE.subn(REDACTED, text)
    hits += count
    text, count = LONG_OPAQUE_RE.subn(REDACTED, text)
    hits += count
    return text, hits


def redact_structure(payload: Any) -> tuple[Any, int]:
    """Redact every string in a JSON-shaped structure, dict keys too."""

    total = 0

    def _walk(node: Any) -> Any:
        nonlocal total
        if isinstance(node, str):
            redacted, count = redact(node)
            total += count
            return redacted
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                redacted_key, count = redact(str(key))
                total += count
                result[redacted_key] = _walk(value)
            return result
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(payload), total

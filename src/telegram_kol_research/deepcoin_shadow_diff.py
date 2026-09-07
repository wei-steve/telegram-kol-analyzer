"""Compare each shadow chain against the ledgers production actually writes.

Phase 4 of the REST+WebSocket program, and the phase's main deliverable: a
per-chain difference report answering, in numbers, how the deterministic chain
compares with the inference-based attribution running in production today --
**how many more it identifies, how many fewer, and how many it gets wrong**.
Without that, switching entry and protection over in phases 5 and 6 would be a
blind cutover.

Everything here reads. It writes ``deepcoin_shadow_diffs`` through the same
shadow-only session guard the chain builder uses, and touches no other table.

``timing_only`` is a category of its own on purpose. Shadow and ledger reaching
the same conclusion at different times is not a defect: the lead is the benefit
being measured, and burying it inside a generic "mismatch" bucket would hide the
one number phase 5's decision rests on.

Ownership questions -- "does anything in this system know this order id?" --
always go through :mod:`deepcoin_shadow_ownership`, which consults every ledger
that stores an exchange identifier. Phase 3 answered that question from the
binding tables alone and reported four of this system's own TPSL stops as
somebody else's; in an attribution report that error direction is the expensive
one.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from telegram_kol_research.deepcoin_shadow_binding import (
    CONFIDENCE_EXACT,
    shadow_only_session,
)
from telegram_kol_research.deepcoin_shadow_ownership import (
    SystemOwnedIds,
    load_system_owned_ids,
)
from telegram_kol_research.models import DeepcoinShadowBinding, DeepcoinShadowDiff

logger = logging.getLogger(__name__)

DIFF_SHADOW_ONLY = "shadow_only"
DIFF_LEDGER_ONLY = "ledger_only"
DIFF_POS_ID_MISMATCH = "pos_id_mismatch"
DIFF_PROTECTION_ORD_ID_MISMATCH = "protection_ord_id_mismatch"
DIFF_SIDE_MISMATCH = "side_mismatch"
DIFF_SIZE_MISMATCH = "size_mismatch"
DIFF_PRICE_MISMATCH = "price_mismatch"
DIFF_TIMING_ONLY = "timing_only"

SHADOW_DIFF_KINDS = (
    DIFF_SHADOW_ONLY,
    DIFF_LEDGER_ONLY,
    DIFF_POS_ID_MISMATCH,
    DIFF_PROTECTION_ORD_ID_MISMATCH,
    DIFF_SIDE_MISMATCH,
    DIFF_SIZE_MISMATCH,
    DIFF_PRICE_MISMATCH,
    DIFF_TIMING_ONLY,
)

_RELATIVE_TOLERANCE = Decimal("1e-9")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any) -> Decimal | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def _close_enough(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return False
    if left == right:
        return True
    scale = max(abs(left), abs(right), Decimal(1))
    return abs(left - right) <= scale * _RELATIVE_TOLERANCE


def _normalize_side(value: Any) -> str | None:
    text = (_text(value) or "").lower()
    if text in {"long", "buy"}:
        return "long"
    if text in {"short", "sell"}:
        return "short"
    return None


@dataclass
class LedgerChainView:
    """What the existing ledgers say about one entry order id.

    Assembled read-only from the five ledgers the phase 4 plan names plus
    ``position_protection_legs``, which is where a planned protection leg keeps
    its exchange id before the verified ledger row exists.
    """

    main_ord_id: str
    execution_binding_id: int | None = None
    execution_order_leg_id: int | None = None
    pos_id: str | None = None
    side: str | None = None
    symbol: str | None = None
    protection_ord_ids: frozenset[str] = frozenset()
    protection_prices: dict[str, str] = field(default_factory=dict)
    protection_sizes: dict[str, str] = field(default_factory=dict)
    pos_id_known_at: datetime | None = None
    protection_known_at: datetime | None = None

    @property
    def exists(self) -> bool:
        return self.execution_order_leg_id is not None


@dataclass
class ShadowDiffRecord:
    """One difference, ready to persist. Values are text; nothing is inferred."""

    shadow_binding_id: int | None
    diff_kind: str
    subject: str
    shadow_value: str | None
    ledger_value: str | None
    lead_seconds: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def load_ledger_chain_view(
    session_factory: Callable[[], Any], main_ord_id: str
) -> LedgerChainView:
    """Read every ledger fact about one entry order id. Reads only."""

    from sqlalchemy import select

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionProtectionLedger,
        PositionProtectionLeg,
        PositionTakeProfitOrder,
        TriggerProtectionIntent,
    )

    view = LedgerChainView(main_ord_id=main_ord_id)
    protection_ids: set[str] = set()
    prices: dict[str, str] = {}
    sizes: dict[str, str] = {}
    known_stamps: list[datetime] = []
    with session_factory() as session:
        leg = session.execute(
            select(ExecutionOrderLeg)
            .where(
                ExecutionOrderLeg.order_id == main_ord_id,
                ExecutionOrderLeg.purpose == "entry",
            )
            .order_by(ExecutionOrderLeg.id)
        ).scalars().first()
        if leg is None:
            return view
        view.execution_order_leg_id = int(leg.id)
        view.execution_binding_id = int(leg.execution_binding_id)
        view.pos_id = _text(leg.pos_id)
        view.pos_id_known_at = leg.last_verified_at or leg.updated_at
        binding = session.get(ExecutionBinding, int(leg.execution_binding_id))
        if binding is not None:
            view.side = _normalize_side(binding.side)
            view.symbol = _text(binding.symbol)

        for row in session.execute(
            select(PositionProtectionLedger).where(
                PositionProtectionLedger.execution_order_leg_id == leg.id
            )
        ).scalars():
            order_id = _text(row.order_id)
            if order_id is None:
                continue
            protection_ids.add(order_id)
            if _text(row.trigger_price) is not None:
                prices[order_id] = str(row.trigger_price)
            if _text(row.size_text) is not None:
                sizes[order_id] = str(row.size_text)
            if row.first_seen_at is not None:
                known_stamps.append(row.first_seen_at)

        for row in session.execute(
            select(PositionTakeProfitOrder).where(
                PositionTakeProfitOrder.execution_order_leg_id == leg.id
            )
        ).scalars():
            order_id = _text(row.order_id)
            if order_id is None:
                continue
            protection_ids.add(order_id)
            prices.setdefault(order_id, str(row.trigger_price))
            if _text(row.size_text) is not None:
                sizes.setdefault(order_id, str(row.size_text))
            if row.created_at is not None:
                known_stamps.append(row.created_at)

        for row in session.execute(
            select(PositionProtectionLeg).where(
                PositionProtectionLeg.execution_order_leg_id == leg.id
            )
        ).scalars():
            order_id = _text(row.exchange_order_id)
            if order_id is None:
                continue
            protection_ids.add(order_id)
            if _text(row.planned_trigger_price) is not None:
                prices.setdefault(order_id, str(row.planned_trigger_price))
            if _text(row.planned_size) is not None:
                sizes.setdefault(order_id, str(row.planned_size))
            if row.created_at is not None:
                known_stamps.append(row.created_at)

        for row in session.execute(
            select(TriggerProtectionIntent).where(
                TriggerProtectionIntent.execution_order_leg_id == leg.id
            )
        ).scalars():
            for candidate in (row.adopted_order_id, row.parent_trigger_order_id):
                order_id = _text(candidate)
                if order_id is not None:
                    protection_ids.add(order_id)
            if row.created_at is not None:
                known_stamps.append(row.created_at)

    view.protection_ord_ids = frozenset(protection_ids)
    view.protection_prices = prices
    view.protection_sizes = sizes
    view.protection_known_at = min(known_stamps) if known_stamps else None
    return view


def _lead_seconds(shadow_at: datetime | None, ledger_at: datetime | None) -> float | None:
    """Seconds the ledger trailed the shadow. Negative means the ledger won.

    Timestamps reach here from two sources -- one written as an aware UTC value
    and read back naive by SQLite, one possibly still aware in memory -- so both
    are normalised to naive UTC before subtracting. Getting this wrong raises
    inside the difference pass instead of producing a number.
    """

    if shadow_at is None or ledger_at is None:
        return None
    left = shadow_at.astimezone(UTC).replace(tzinfo=None) if shadow_at.tzinfo else shadow_at
    right = ledger_at.astimezone(UTC).replace(tzinfo=None) if ledger_at.tzinfo else ledger_at
    return round((right - left).total_seconds(), 3)


def compare_chain(
    shadow: DeepcoinShadowBinding,
    ledger: LedgerChainView,
    *,
    owned: SystemOwnedIds,
) -> list[ShadowDiffRecord]:
    """Return every difference between one shadow chain and the ledger view.

    Object-level disagreement (one side knows an object the other has never
    heard of) is ``shadow_only`` / ``ledger_only``. Field-level disagreement
    about an object both sides know is one of the ``*_mismatch`` kinds. The two
    are never mixed, because they mean different things for phases 5 and 6: the
    first says the two paths see different worlds, the second says they see the
    same world differently.
    """

    diffs: list[ShadowDiffRecord] = []
    shadow_id = int(shadow.id) if shadow.id is not None else None
    shadow_exact = str(shadow.binding_confidence) == CONFIDENCE_EXACT
    shadow_protection = frozenset(
        part for part in str(shadow.protection_ord_id or "").split(",") if part
    )
    try:
        shadow_evidence = json.loads(shadow.evidence_json or "{}")
    except (TypeError, ValueError):
        shadow_evidence = {}

    if shadow_exact and not ledger.exists:
        diffs.append(
            ShadowDiffRecord(
                shadow_binding_id=shadow_id,
                diff_kind=DIFF_SHADOW_ONLY,
                subject="binding",
                shadow_value=str(shadow.main_ord_id),
                ledger_value=None,
                evidence={
                    "pos_id": shadow.pos_id,
                    "protection_ord_ids": sorted(shadow_protection),
                    "order_id_known_to_any_ledger": owned.owns_order(
                        shadow.main_ord_id
                    ),
                },
            )
        )
        return diffs

    if ledger.exists and not shadow_exact:
        diffs.append(
            ShadowDiffRecord(
                shadow_binding_id=shadow_id,
                diff_kind=DIFF_LEDGER_ONLY,
                subject="binding",
                shadow_value=str(shadow.refusal_reason or "unverified"),
                ledger_value=str(ledger.execution_binding_id),
                evidence={
                    "ledger_pos_id": ledger.pos_id,
                    "ledger_protection_ord_ids": sorted(ledger.protection_ord_ids),
                    "shadow_stage": shadow.stage,
                    "criteria": shadow_evidence.get("criteria", {}),
                },
            )
        )
        return diffs

    if not ledger.exists:
        # Shadow could not verify it either; there is nothing to compare and
        # nothing either side is claiming. Not a difference.
        return diffs

    shadow_pos_id = _text(shadow.pos_id)
    if shadow_pos_id is not None and ledger.pos_id is not None:
        if shadow_pos_id != ledger.pos_id:
            diffs.append(
                ShadowDiffRecord(
                    shadow_binding_id=shadow_id,
                    diff_kind=DIFF_POS_ID_MISMATCH,
                    subject="pos_id",
                    shadow_value=shadow_pos_id,
                    ledger_value=ledger.pos_id,
                    evidence={"main_ord_id": shadow.main_ord_id},
                )
            )
    elif shadow_pos_id is not None and ledger.pos_id is None:
        diffs.append(
            ShadowDiffRecord(
                shadow_binding_id=shadow_id,
                diff_kind=DIFF_SHADOW_ONLY,
                subject="pos_id",
                shadow_value=shadow_pos_id,
                ledger_value=None,
                evidence={"main_ord_id": shadow.main_ord_id},
            )
        )
    elif shadow_pos_id is None and ledger.pos_id is not None:
        diffs.append(
            ShadowDiffRecord(
                shadow_binding_id=shadow_id,
                diff_kind=DIFF_LEDGER_ONLY,
                subject="pos_id",
                shadow_value=None,
                ledger_value=ledger.pos_id,
                evidence={"main_ord_id": shadow.main_ord_id},
            )
        )

    shadow_side = _normalize_side(shadow.side)
    if shadow_side is not None and ledger.side is not None and shadow_side != ledger.side:
        diffs.append(
            ShadowDiffRecord(
                shadow_binding_id=shadow_id,
                diff_kind=DIFF_SIDE_MISMATCH,
                subject="side",
                shadow_value=shadow_side,
                ledger_value=ledger.side,
                evidence={"main_ord_id": shadow.main_ord_id},
            )
        )

    # Protection is a set on both sides: ``set-position-sltp`` produces more
    # than one protection order for one posId, so a single-value comparison
    # would report a difference wherever production is simply doing its job.
    if shadow_protection != ledger.protection_ord_ids:
        if shadow_protection and ledger.protection_ord_ids:
            diffs.append(
                ShadowDiffRecord(
                    shadow_binding_id=shadow_id,
                    diff_kind=DIFF_PROTECTION_ORD_ID_MISMATCH,
                    subject="protection_ord_id_set",
                    shadow_value=",".join(sorted(shadow_protection)),
                    ledger_value=",".join(sorted(ledger.protection_ord_ids)),
                    evidence={
                        "only_in_shadow": sorted(
                            shadow_protection - ledger.protection_ord_ids
                        ),
                        "only_in_ledger": sorted(
                            ledger.protection_ord_ids - shadow_protection
                        ),
                    },
                )
            )
        for order_id in sorted(shadow_protection - ledger.protection_ord_ids):
            if ledger.protection_ord_ids:
                continue
            diffs.append(
                ShadowDiffRecord(
                    shadow_binding_id=shadow_id,
                    diff_kind=DIFF_SHADOW_ONLY,
                    subject=f"protection_ord_id:{order_id}",
                    shadow_value=order_id,
                    ledger_value=None,
                    evidence={
                        "order_id_known_to_any_ledger": owned.owns_order(order_id)
                    },
                )
            )
        for order_id in sorted(ledger.protection_ord_ids - shadow_protection):
            if shadow_protection:
                continue
            diffs.append(
                ShadowDiffRecord(
                    shadow_binding_id=shadow_id,
                    diff_kind=DIFF_LEDGER_ONLY,
                    subject=f"protection_ord_id:{order_id}",
                    shadow_value=None,
                    ledger_value=order_id,
                    evidence={"shadow_refusal_reason": shadow.refusal_reason},
                )
            )

    shadow_prices = _shadow_protection_prices(shadow_evidence)
    for order_id in sorted(shadow_protection & ledger.protection_ord_ids):
        ledger_price = _decimal(ledger.protection_prices.get(order_id))
        shadow_price = _decimal(shadow_prices.get(order_id))
        if (
            ledger_price is not None
            and shadow_price is not None
            and not _close_enough(shadow_price, ledger_price)
        ):
            diffs.append(
                ShadowDiffRecord(
                    shadow_binding_id=shadow_id,
                    diff_kind=DIFF_PRICE_MISMATCH,
                    subject=f"trigger_price:{order_id}",
                    shadow_value=str(shadow_price),
                    ledger_value=str(ledger_price),
                    evidence={"main_ord_id": shadow.main_ord_id},
                )
            )

    # Sizes are compared per protection order, over the orders both sides know.
    # Comparing totals would hide the case that matters -- two orders whose
    # sizes are swapped between them sum identically.
    shadow_sizes = _shadow_protection_sizes(shadow_evidence)
    for order_id in sorted(shadow_protection & ledger.protection_ord_ids):
        ledger_size = _decimal(ledger.protection_sizes.get(order_id))
        shadow_size = _decimal(shadow_sizes.get(order_id))
        if (
            ledger_size is not None
            and shadow_size is not None
            and not _close_enough(shadow_size, ledger_size)
        ):
            diffs.append(
                ShadowDiffRecord(
                    shadow_binding_id=shadow_id,
                    diff_kind=DIFF_SIZE_MISMATCH,
                    subject=f"protection_size:{order_id}",
                    shadow_value=str(shadow_size),
                    ledger_value=str(ledger_size),
                    evidence={"main_ord_id": shadow.main_ord_id},
                )
            )

    # Timing is measured only where the two paths agree. A lead reported next to
    # a disagreement would be meaningless -- they did not reach the same answer.
    if not diffs:
        for subject, shadow_at, ledger_at in (
            ("pos_id", shadow.trade_os_seen_at, ledger.pos_id_known_at),
            ("protection", shadow.tu_matched_at, ledger.protection_known_at),
        ):
            lead = _lead_seconds(shadow_at, ledger_at)
            if lead is None or lead == 0:
                continue
            diffs.append(
                ShadowDiffRecord(
                    shadow_binding_id=shadow_id,
                    diff_kind=DIFF_TIMING_ONLY,
                    subject=subject,
                    shadow_value=shadow_at.isoformat(),
                    ledger_value=ledger_at.isoformat(),
                    lead_seconds=lead,
                    evidence={
                        "main_ord_id": shadow.main_ord_id,
                        "note": (
                            "shadow ahead of ledger by lead_seconds; a negative "
                            "value means the ledger concluded first"
                        ),
                    },
                )
            )
    return diffs


def _shadow_protection_prices(evidence: dict[str, Any]) -> dict[str, str]:
    prices = evidence.get("protection_prices")
    if isinstance(prices, dict):
        return {str(key): str(value) for key, value in prices.items()}
    return {}


def _shadow_protection_sizes(evidence: dict[str, Any]) -> dict[str, str]:
    sizes = evidence.get("protection_sizes")
    if isinstance(sizes, dict):
        return {str(key): str(value) for key, value in sizes.items()}
    return {}


def persist_diffs(
    session_factory: Callable[[], Any],
    records: list[ShadowDiffRecord],
    *,
    detected_at: datetime,
) -> int:
    """Write difference rows through the shadow-only session guard."""

    from sqlalchemy import select

    written = 0
    with shadow_only_session(session_factory) as session:
        for record in records:
            existing = session.execute(
                select(DeepcoinShadowDiff).where(
                    DeepcoinShadowDiff.shadow_binding_id == record.shadow_binding_id,
                    DeepcoinShadowDiff.diff_kind == record.diff_kind,
                    DeepcoinShadowDiff.subject == record.subject,
                )
            ).scalar_one_or_none()
            evidence_json = json.dumps(
                record.evidence, ensure_ascii=False, sort_keys=True, default=str
            )
            if existing is None:
                session.add(
                    DeepcoinShadowDiff(
                        shadow_binding_id=record.shadow_binding_id,
                        diff_kind=record.diff_kind,
                        subject=record.subject,
                        shadow_value=record.shadow_value,
                        ledger_value=record.ledger_value,
                        lead_seconds=record.lead_seconds,
                        detected_at=detected_at,
                        evidence_json=evidence_json,
                        created_at=detected_at,
                    )
                )
                written += 1
            else:
                existing.shadow_value = record.shadow_value
                existing.ledger_value = record.ledger_value
                existing.lead_seconds = record.lead_seconds
                existing.detected_at = detected_at
                existing.evidence_json = evidence_json
        session.commit()
    return written


def run_shadow_diff_pass(
    session_factory: Callable[[], Any],
    *,
    now: datetime,
    shadow_binding_ids: tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Re-derive the differences for the given shadow chains (or all of them)."""

    from sqlalchemy import select

    owned = load_system_owned_ids(session_factory)
    with session_factory() as session:
        query = select(DeepcoinShadowBinding)
        if shadow_binding_ids:
            query = query.where(DeepcoinShadowBinding.id.in_(list(shadow_binding_ids)))
        shadows = list(session.execute(query).scalars())
        session.expunge_all()

    records: list[ShadowDiffRecord] = []
    for shadow in shadows:
        ledger = load_ledger_chain_view(session_factory, str(shadow.main_ord_id))
        records.extend(compare_chain(shadow, ledger, owned=owned))
    written = persist_diffs(session_factory, records, detected_at=now)
    counts: dict[str, int] = {kind: 0 for kind in SHADOW_DIFF_KINDS}
    for record in records:
        counts[record.diff_kind] = counts.get(record.diff_kind, 0) + 1
    return {
        "chains_compared": len(shadows),
        "diffs_seen": len(records),
        "diffs_written": written,
        "counts_by_kind": counts,
        "ownership_tables_read": list(owned.tables_read),
        "ownership_tables_missing": list(owned.tables_missing),
    }


def build_shadow_binding_report(
    session_factory: Callable[[], Any], *, now: datetime
) -> dict[str, Any]:
    """Counts only. No identifier, price, size or payload reaches this result.

    The endpoint that serves this is read-only and localhost-only, and the row
    detail belongs in a server-side evidence file written by the CLI exporter --
    not in an HTTP response.
    """

    from sqlalchemy import func, select

    counts_by_kind = {kind: 0 for kind in SHADOW_DIFF_KINDS}
    counts_by_confidence: dict[str, int] = {}
    counts_by_stage: dict[str, int] = {}
    counts_by_refusal: dict[str, int] = {}
    leads: list[float] = []
    with session_factory() as session:
        for kind, count in session.execute(
            select(DeepcoinShadowDiff.diff_kind, func.count(DeepcoinShadowDiff.id))
            .group_by(DeepcoinShadowDiff.diff_kind)
        ).all():
            counts_by_kind[str(kind)] = int(count)
        for confidence, count in session.execute(
            select(
                DeepcoinShadowBinding.binding_confidence,
                func.count(DeepcoinShadowBinding.id),
            ).group_by(DeepcoinShadowBinding.binding_confidence)
        ).all():
            counts_by_confidence[str(confidence)] = int(count)
        for stage, count in session.execute(
            select(DeepcoinShadowBinding.stage, func.count(DeepcoinShadowBinding.id))
            .group_by(DeepcoinShadowBinding.stage)
        ).all():
            counts_by_stage[str(stage)] = int(count)
        for reason, count in session.execute(
            select(
                DeepcoinShadowBinding.refusal_reason,
                func.count(DeepcoinShadowBinding.id),
            )
            .where(DeepcoinShadowBinding.refusal_reason.is_not(None))
            .group_by(DeepcoinShadowBinding.refusal_reason)
        ).all():
            counts_by_refusal[str(reason)] = int(count)
        for (lead,) in session.execute(
            select(DeepcoinShadowDiff.lead_seconds).where(
                DeepcoinShadowDiff.diff_kind == DIFF_TIMING_ONLY,
                DeepcoinShadowDiff.lead_seconds.is_not(None),
            )
        ).all():
            leads.append(float(lead))
        chain_total = int(
            session.execute(select(func.count(DeepcoinShadowBinding.id))).scalar() or 0
        )

    exact = counts_by_confidence.get(CONFIDENCE_EXACT, 0)
    unverified = counts_by_confidence.get("unverified", 0)
    return {
        "chain_count": chain_total,
        "exact_count": exact,
        "unverified_count": unverified,
        "exact_ratio": (0.0 if chain_total == 0 else round(exact / chain_total, 6)),
        "counts_by_diff_kind": counts_by_kind,
        "counts_by_stage": counts_by_stage,
        "counts_by_refusal_reason": counts_by_refusal,
        "shadow_only_count": counts_by_kind.get(DIFF_SHADOW_ONLY, 0),
        "ledger_only_count": counts_by_kind.get(DIFF_LEDGER_ONLY, 0),
        "timing_only_count": counts_by_kind.get(DIFF_TIMING_ONLY, 0),
        "timing_only_median_lead_seconds": (
            None if not leads else round(statistics.median(leads), 3)
        ),
        "timing_only_sample_size": len(leads),
        "now": now.isoformat(),
    }

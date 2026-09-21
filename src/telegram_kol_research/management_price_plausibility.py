"""What to do with a number a break-even message happens to carry.

The stop gate refuses an implicit-stop action that also names an explicit
price, on the reading that the two say different things and the safe answer is
to do neither.  Three years of that rule in production produced three
triggers, and in all three the action label was right and the number was never
a second stop somebody wanted: a QQ number in a signature, the low the move
had just printed, and the KOL's own cost restated.  All three cost the KOL
both the reduction and the protection they asked for, and the third left a BTC
short on its original stop for six hours.

So the number is judged here, before the gate, and the instruction executes
either way.  Per supplied price, in this order:

1. more than ten times away from the instrument's last trade -- not a price
   for this instrument at all (``implausible_magnitude``);
2. on the invalid side of the market, where a stop would trigger the instant
   it was placed (``not_a_possible_stop``);
3. placeable *and* tighter than the strategy's own break-even price, and it
   still passes every explicit-price check the gate makes -- this is a
   protection level the KOL named, so it becomes the target
   (``explicit_tighter_adopted``);
4. anything else, including a looser price and a quote we could not read
   (``superseded_by_strategy_price`` / ``ignored_quote_unavailable``).

Two boundaries survive from the magnitude-only version of this module:

* The stop gate is not touched.  This runs *before* the gate and only decides
  values the gate would have had to judge; every gate rule stays as it is.
* ``adjust_stop_loss`` never reaches here.  There the price *is* the
  instruction, the gate already compares it against the same market, and
  refusing is the right answer -- never invent a stop the message did not name.

The third boundary is new and is the one the user chose: **not being able to
judge never refuses the instruction.**  An unreadable quote used to leave the
number in place and let the gate refuse everything; it now drops the number
and breaks even on the strategy's price, because a group message that wants
out should be followed rather than second-guessed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from telegram_kol_research.break_even_reference import (
    BreakEvenReference,
    adopted_break_even_reference,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident
from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
    load_management_contract,
    management_contract_fingerprint,
    serialize_management_contract,
)

logger = logging.getLogger(__name__)

#: A price this far from the last trade is not a price for this instrument.
#: Ten times is far past any stop a KOL would set and far short of a typo that
#: a person would still want executed.
IMPLAUSIBLE_PRICE_RATIO = Decimal("10")

PRICE_IMPLAUSIBLE_INCIDENT_TYPE = "management_price_implausible"
#: Deliberately *low* and deliberately absent from every notification list:
#: this row records the system doing the right thing with a number that was
#: never a stop, and an alert for that is an alert nobody can act on.
PRICE_DISPOSED_INCIDENT_TYPE = "management_price_disposed"

IMPLAUSIBLE_MAGNITUDE = "implausible_magnitude"
NOT_A_POSSIBLE_STOP = "not_a_possible_stop"
EXPLICIT_TIGHTER_ADOPTED = "explicit_tighter_adopted"
SUPERSEDED_BY_STRATEGY_PRICE = "superseded_by_strategy_price"
IGNORED_QUOTE_UNAVAILABLE = "ignored_quote_unavailable"

CONTRACT_STOP_PRICE_FIELD = "contract_stop_price"
STOP_LOSS_TEXT_FIELD = "stop_loss_text"


@dataclass(frozen=True, slots=True)
class DisposedPrice:
    """One supplied price, and what this module decided it was."""

    field: str
    value: str
    reference_price: str | None
    ratio: str
    disposition: str

    def as_evidence(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "reference_price": self.reference_price,
            "ratio": self.ratio,
            "disposition": self.disposition,
        }


@dataclass(frozen=True, slots=True)
class PendingExplicitPrice:
    """A price that survived the market checks and awaits the strategy price.

    Which of the last two rows it lands on depends on the break-even
    reference, and that is only known once the exchange preflight has said
    which entry legs still hold a position.  The value is already removed from
    the candidate by then -- only its *meaning* is still open.
    """

    field: str
    value: str
    #: The provenance recognition recorded for this value, kept because the
    #: candidate view no longer carries it once the value is removed.
    source: str | None = None


@dataclass(frozen=True, slots=True)
class SanitizedManagementPrices:
    """The values planning should use, plus what was decided and why."""

    stop_loss_text: str | None
    stop_price_source: str | None
    management_contract_json: str | None
    management_contract_fingerprint: str | None
    findings: tuple[DisposedPrice, ...]
    reference_price: str | None
    pending: tuple[PendingExplicitPrice, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.findings) or bool(self.pending)

    @property
    def implausible_findings(self) -> tuple[DisposedPrice, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.disposition == IMPLAUSIBLE_MAGNITUDE
        )

    @property
    def disposed_findings(self) -> tuple[DisposedPrice, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.disposition != IMPLAUSIBLE_MAGNITUDE
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "reference_price": self.reference_price,
            "reference_price_source": "current_market_last",
            "max_ratio": str(IMPLAUSIBLE_PRICE_RATIO),
            "removed": [finding.as_evidence() for finding in self.findings],
        }


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return number if number.is_finite() and number > 0 else None


def quote_reference_price(quote: Any) -> Decimal | None:
    """Return the last traded price a quote proves, or ``None``."""

    if not isinstance(quote, dict):
        return None
    if quote.get("price_field") not in {"last", "lastPx"}:
        return None
    return _positive_decimal(quote.get("price"))


def price_is_implausible(value: Any, reference: Decimal | None) -> bool:
    """Whether ``value`` is more than ten times away from ``reference``."""

    price = _positive_decimal(value)
    if price is None or reference is None or reference <= 0:
        return False
    ratio = price / reference if price >= reference else reference / price
    return ratio > IMPLAUSIBLE_PRICE_RATIO


def price_could_be_a_stop(value: Any, market: Decimal | None, side: Any) -> bool:
    """Whether this price is on the side of the market a stop can live on.

    A long's stop is below the market and a short's is above it.  A price on
    the other side is not a stop that could ever protect anything -- the
    exchange would trigger it immediately -- so it is not a competing answer
    to "where should the stop go".
    """

    price = _positive_decimal(value)
    normalized_side = str(side or "").strip().lower()
    if price is None or market is None or normalized_side not in {"long", "short"}:
        return False
    return price < market if normalized_side == "long" else price > market


def price_is_tighter(value: Any, target: Any, side: Any) -> bool:
    """Whether ``value`` protects strictly more than ``target`` does."""

    price = _positive_decimal(value)
    reference = _positive_decimal(target)
    normalized_side = str(side or "").strip().lower()
    if (
        price is None
        or reference is None
        or normalized_side not in {"long", "short"}
    ):
        return False
    return price > reference if normalized_side == "long" else price < reference


def _ratio_text(value: Any, reference: Decimal | None) -> str:
    price = _positive_decimal(value)
    if price is None or reference is None:
        return "unknown"
    ratio = price / reference if price >= reference else reference / price
    return str(ratio.quantize(Decimal("0.01")))


def _market_disposition(
    value: Any, *, reference: Decimal | None, side: Any
) -> str | None:
    """The disposition the market alone decides, or ``None`` to keep judging."""

    if reference is None:
        return IGNORED_QUOTE_UNAVAILABLE
    if price_is_implausible(value, reference):
        return IMPLAUSIBLE_MAGNITUDE
    if price_could_be_a_stop(value, reference, side):
        return None
    if _positive_decimal(value) is None or str(side or "").strip().lower() not in {
        "long",
        "short",
    }:
        # Nothing about this value can be judged, and the catch-all row is
        # "drop the number and use the strategy price".
        return SUPERSEDED_BY_STRATEGY_PRICE
    return NOT_A_POSSIBLE_STOP


def sanitize_management_prices(
    *,
    stop_loss_text: str | None,
    stop_price_source: str | None,
    management_contract_json: str | None,
    management_contract_fingerprint_value: str | None,
    quote: Any,
    side: Any = None,
) -> SanitizedManagementPrices:
    """Remove every explicit price an implicit-stop instruction carries.

    Only implicit-stop actions reach this function, and for them the removal
    is unconditional: the target of a break-even stop has exactly one source,
    which is the break-even reference.  A price that could still *become* that
    reference is returned as :class:`PendingExplicitPrice` for
    :func:`dispose_pending_break_even_prices` to finish judging.
    """

    unchanged = SanitizedManagementPrices(
        stop_loss_text=stop_loss_text,
        stop_price_source=stop_price_source,
        management_contract_json=management_contract_json,
        management_contract_fingerprint=management_contract_fingerprint_value,
        findings=(),
        reference_price=None,
    )
    reference = quote_reference_price(quote)

    findings: list[DisposedPrice] = []
    pending: list[PendingExplicitPrice] = []
    contract: ManagementInstructionContract | None = None
    if management_contract_json:
        try:
            contract = load_management_contract(str(management_contract_json))
        except ValueError:
            # A contract that will not load is rejected downstream on its own
            # terms; this module never turns a parse failure into a decision.
            contract = None

    def judge(field: str, value: Any, source: Any) -> None:
        disposition = _market_disposition(value, reference=reference, side=side)
        if disposition is None:
            pending.append(
                PendingExplicitPrice(
                    field=field,
                    value=str(value),
                    source=None if source is None else str(source),
                )
            )
            return
        findings.append(
            DisposedPrice(
                field=field,
                value=str(value),
                reference_price=None if reference is None else str(reference),
                ratio=_ratio_text(value, reference),
                disposition=disposition,
            )
        )

    new_contract_json = management_contract_json
    new_contract_fingerprint = management_contract_fingerprint_value
    if (
        contract is not None
        and contract.stop_mode == "explicit_price"
        and contract.stop_price not in (None, "")
    ):
        judge(
            CONTRACT_STOP_PRICE_FIELD,
            contract.stop_price,
            contract.stop_price_source,
        )
        # "Not provided" is exactly the actual-entry-price contract the same
        # instruction would have carried had the message named no stop at all.
        neutralized = replace(
            contract,
            stop_mode="actual_entry_price",
            stop_price=None,
            stop_price_source=None,
        )
        new_contract_json = serialize_management_contract(neutralized)
        new_contract_fingerprint = management_contract_fingerprint(neutralized)

    new_stop_loss_text = stop_loss_text
    new_stop_price_source = stop_price_source
    if stop_loss_text not in (None, ""):
        judge(STOP_LOSS_TEXT_FIELD, stop_loss_text, stop_price_source)
        new_stop_loss_text = None
        new_stop_price_source = None

    if not findings and not pending:
        return unchanged
    return SanitizedManagementPrices(
        stop_loss_text=new_stop_loss_text,
        stop_price_source=new_stop_price_source,
        management_contract_json=new_contract_json,
        management_contract_fingerprint=new_contract_fingerprint,
        findings=tuple(findings),
        reference_price=None if reference is None else str(reference),
        pending=tuple(pending),
    )


def dispose_pending_break_even_prices(
    sanitized: SanitizedManagementPrices | None,
    *,
    side: Any,
    reference: BreakEvenReference,
    explicit_price_validator: Callable[[str, str | None], str | None] | None = None,
) -> tuple[SanitizedManagementPrices | None, BreakEvenReference]:
    """Finish judging the prices that only the strategy price can settle.

    A price strictly tighter than the strategy's own break-even price is a
    protection level the KOL asked for, so it becomes the target -- but only
    after it passes the same provenance, deviation and direction checks an
    ``adjust_stop_loss`` would have to pass.  Everything else is dropped and
    the strategy price stands.
    """

    if sanitized is None or not sanitized.pending:
        return sanitized, reference

    findings = list(sanitized.findings)
    adopted: Decimal | None = None
    for candidate in sanitized.pending:
        disposition = SUPERSEDED_BY_STRATEGY_PRICE
        if price_is_tighter(candidate.value, reference.price, side):
            refusal = (
                explicit_price_validator(candidate.value, candidate.source)
                if explicit_price_validator is not None
                else "management_stop_validator_unavailable"
            )
            if refusal is None:
                disposition = EXPLICIT_TIGHTER_ADOPTED
                price = _positive_decimal(candidate.value)
                if price is not None and (
                    adopted is None
                    or price_is_tighter(price, adopted, side)
                ):
                    adopted = price
        findings.append(
            DisposedPrice(
                field=candidate.field,
                value=candidate.value,
                reference_price=sanitized.reference_price,
                ratio=_ratio_text(
                    candidate.value,
                    _positive_decimal(sanitized.reference_price),
                ),
                disposition=disposition,
            )
        )
    resolved = (
        adopted_break_even_reference(reference, price=adopted)
        if adopted is not None
        else reference
    )
    return (
        replace(sanitized, findings=tuple(findings), pending=()),
        resolved,
    )


def _record_price_finding_incident(
    session_factory,
    *,
    incident_type: str,
    severity: str,
    findings: tuple[DisposedPrice, ...],
    raw_message_id: int,
    candidate_id: int,
    sanitized: SanitizedManagementPrices,
    now: datetime,
):
    if not findings:
        return None
    summary = json.dumps(
        {
            "component": "strategy_management",
            "reason_code": incident_type,
            "raw_message_id": int(raw_message_id),
            # The summary's fields are a fixed scalar allowlist, so the
            # dispositions ride along inside ``impact`` rather than adding a
            # key the incident schema would refuse.
            "impact": "explicit_price_treated_as_absent:"
            + ",".join(sorted({finding.disposition for finding in findings})),
            "operation": f"raw_message_{int(raw_message_id)}",
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        return record_runtime_incident(
            session_factory,
            source_kind="signal_candidate",
            source_record_id=str(int(candidate_id)),
            incident_type=incident_type,
            severity=severity,
            fingerprint=hashlib.sha256(
                f"{incident_type}:{int(candidate_id)}:"
                f"{','.join(finding.field for finding in findings)}".encode()
            ).hexdigest(),
            redacted_summary=summary,
            occurred_at=now,
            feature_policy_version="management-price-plausibility-v2",
            prompt_version="none",
            tool_policy_version="no-exchange-write",
            diagnosis_json=json.dumps(
                {
                    "observed_state": {
                        **sanitized.as_evidence(),
                        "removed": [
                            finding.as_evidence() for finding in findings
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            evidence_refs_json=json.dumps(
                [
                    f"raw_message:{int(raw_message_id)}",
                    f"signal_candidate:{int(candidate_id)}",
                ]
            ),
        )
    except Exception:  # noqa: BLE001 - the removal must survive a ledger failure
        logger.warning(
            "failed to record %s for raw_message_id=%s",
            incident_type,
            raw_message_id,
            exc_info=True,
        )
        return None


def record_price_implausible(
    session_factory,
    *,
    raw_message_id: int,
    candidate_id: int,
    sanitized: SanitizedManagementPrices,
    now: datetime,
):
    """Record one ledger row naming every price of the wrong magnitude.

    Best effort on purpose.  Removing the price is the protective act; the
    alert is how a person learns about it.  A ledger failure must never put
    the price back, so it is logged and swallowed -- the same removal is also
    written into the batch's own target snapshot.
    """

    return _record_price_finding_incident(
        session_factory,
        incident_type=PRICE_IMPLAUSIBLE_INCIDENT_TYPE,
        severity="high",
        findings=sanitized.implausible_findings,
        raw_message_id=raw_message_id,
        candidate_id=candidate_id,
        sanitized=sanitized,
        now=now,
    )


def record_price_disposed(
    session_factory,
    *,
    raw_message_id: int,
    candidate_id: int,
    sanitized: SanitizedManagementPrices,
    now: datetime,
):
    """Record one low-severity row for every number that was not a new stop."""

    return _record_price_finding_incident(
        session_factory,
        incident_type=PRICE_DISPOSED_INCIDENT_TYPE,
        severity="low",
        findings=sanitized.disposed_findings,
        raw_message_id=raw_message_id,
        candidate_id=candidate_id,
        sanitized=sanitized,
        now=now,
    )

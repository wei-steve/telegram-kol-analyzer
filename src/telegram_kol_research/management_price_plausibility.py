"""Treat an explicit management price of the wrong magnitude as absent.

Scrubbing contact identifiers removes the known shapes of a non-price number
(:mod:`telegram_kol_research.contact_digit_scrubbing`).  This module is the
backstop for the unknown ones: whatever a number's shape, a price ten times
away from the instrument's own last traded price is not a price for that
instrument, and the safe reading of it is that the message supplied none.

Two deliberate boundaries:

* The stop gate is not touched.  This runs *before* the gate and only removes
  a value the gate would have had to judge; every gate rule stays as it is.
* No market read, no check.  An unavailable quote makes the magnitude unknown,
  and unknown never relaxes anything: the value goes to the gate unchanged and
  the gate refuses it exactly as it does today.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

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


@dataclass(frozen=True, slots=True)
class ImplausiblePrice:
    """One supplied price that the instrument's own market contradicts."""

    field: str
    value: str
    reference_price: str
    ratio: str

    def as_evidence(self) -> dict[str, str]:
        return {
            "field": self.field,
            "value": self.value,
            "reference_price": self.reference_price,
            "ratio": self.ratio,
        }


@dataclass(frozen=True, slots=True)
class SanitizedManagementPrices:
    """The values planning should use, plus what was removed and why."""

    stop_loss_text: str | None
    stop_price_source: str | None
    management_contract_json: str | None
    management_contract_fingerprint: str | None
    findings: tuple[ImplausiblePrice, ...]
    reference_price: str | None

    @property
    def changed(self) -> bool:
        return bool(self.findings)

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


def _ratio_text(value: Any, reference: Decimal) -> str:
    price = _positive_decimal(value)
    if price is None:
        return "unknown"
    ratio = price / reference if price >= reference else reference / price
    return str(ratio.quantize(Decimal("0.01")))


def sanitize_management_prices(
    *,
    stop_loss_text: str | None,
    stop_price_source: str | None,
    management_contract_json: str | None,
    management_contract_fingerprint_value: str | None,
    quote: Any,
) -> SanitizedManagementPrices:
    """Remove explicit prices the instrument's own market contradicts.

    Everything else is returned untouched, including a contract whose stop was
    already implicit.  An unusable quote returns the inputs verbatim.
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
    if reference is None:
        return unchanged

    findings: list[ImplausiblePrice] = []
    contract: ManagementInstructionContract | None = None
    if management_contract_json:
        try:
            contract = load_management_contract(str(management_contract_json))
        except ValueError:
            # A contract that will not load is rejected downstream on its own
            # terms; this module never turns a parse failure into a decision.
            contract = None

    new_contract_json = management_contract_json
    new_contract_fingerprint = management_contract_fingerprint_value
    if (
        contract is not None
        and contract.stop_mode == "explicit_price"
        and price_is_implausible(contract.stop_price, reference)
    ):
        findings.append(
            ImplausiblePrice(
                field="contract_stop_price",
                value=str(contract.stop_price),
                reference_price=str(reference),
                ratio=_ratio_text(contract.stop_price, reference),
            )
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
    if stop_loss_text not in (None, "") and price_is_implausible(
        stop_loss_text, reference
    ):
        findings.append(
            ImplausiblePrice(
                field="stop_loss_text",
                value=str(stop_loss_text),
                reference_price=str(reference),
                ratio=_ratio_text(stop_loss_text, reference),
            )
        )
        new_stop_loss_text = None
        new_stop_price_source = None

    if not findings:
        return unchanged
    return SanitizedManagementPrices(
        stop_loss_text=new_stop_loss_text,
        stop_price_source=new_stop_price_source,
        management_contract_json=new_contract_json,
        management_contract_fingerprint=new_contract_fingerprint,
        findings=tuple(findings),
        reference_price=str(reference),
    )


def record_price_implausible(
    session_factory,
    *,
    raw_message_id: int,
    candidate_id: int,
    sanitized: SanitizedManagementPrices,
    now: datetime,
):
    """Record one ledger row naming every price removed for this message.

    Best effort on purpose.  Removing the price is the protective act; the
    alert is how a person learns about it.  A ledger failure must never put
    the price back, so it is logged and swallowed -- the same removal is also
    written into the batch's own target snapshot.
    """

    if not sanitized.findings:
        return None
    summary = json.dumps(
        {
            "component": "strategy_management",
            "reason_code": PRICE_IMPLAUSIBLE_INCIDENT_TYPE,
            "raw_message_id": int(raw_message_id),
            "impact": "explicit_price_treated_as_absent",
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
            incident_type=PRICE_IMPLAUSIBLE_INCIDENT_TYPE,
            severity="high",
            fingerprint=hashlib.sha256(
                f"{PRICE_IMPLAUSIBLE_INCIDENT_TYPE}:{int(candidate_id)}:"
                f"{','.join(finding.field for finding in sanitized.findings)}".encode()
            ).hexdigest(),
            redacted_summary=summary,
            occurred_at=now,
            feature_policy_version="management-price-plausibility-v1",
            prompt_version="none",
            tool_policy_version="no-exchange-write",
            diagnosis_json=json.dumps(
                {"observed_state": sanitized.as_evidence()}, ensure_ascii=False
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
            PRICE_IMPLAUSIBLE_INCIDENT_TYPE,
            raw_message_id,
            exc_info=True,
        )
        return None

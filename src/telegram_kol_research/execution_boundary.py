"""Typed, secret-free evidence for the Deepcoin exchange-write boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
)


DEEPCOIN_WRITE_METHODS = frozenset(
    {
        "place_order",
        "trigger_order",
        "set_position_sltp",
        "cancel_position_sltp",
        "replace_order_sltp",
        "cancel_order",
        "cancel_trigger_order",
        "_set_position_sltp_unchecked",
        "_cancel_position_sltp_unchecked",
        "_place_position_close_unchecked",
    }
)

_KNOWN_NO_EFFECT_STATUSES = frozenset(
    {
        "blocked",
        "deferred",
        "skipped",
        "shadow_planned",
        "pending",
        "planned",
        "new_thread_required",
        "failed",
    }
)
_KNOWN_EFFECT_STATUSES = frozenset(
    {
        "submitted",
        "completed",
        "succeeded",
        "executed",
    }
)
#: Message-level statuses that mean "refused before any request", so long as
#: every item proves it individually. ``failed`` keeps its own rule above.
_RETRIABLE_REFUSAL_STATUSES = frozenset({"blocked", "partial_failed"})
#: Item-level payload statuses that prove the item finished without touching
#: the venue. Deliberately narrower than ``_KNOWN_NO_EFFECT_STATUSES``: that
#: set is about a *message* return value and includes ``pending`` and
#: ``planned``, which on an item mean "not finished", not "did nothing". An
#: unfinished item is a hand-off, judged separately below.
_ITEM_TERMINAL_NO_CONTACT_STATUSES = frozenset(
    {"blocked", "deferred", "skipped", "shadow_planned", "new_thread_required", "failed"}
)
#: Item statuses that mean the item is finished. ``submitted`` is deliberately
#: absent: it claims an exchange effect, so it can never be part of a proof
#: that nothing was sent.
_ITEM_FINISHED_STATUSES = frozenset({"failed", "unknown", "succeeded"})
#: Item statuses that mean somebody else still owns the work -- an async batch,
#: the entry queue, or the visibility-retry timer behind a deferred item.
_ITEM_UNFINISHED_STATUSES = frozenset({"pending", "executing"})
#: A-6c. Refusal reasons that are raised *before* the exchange authority is
#: even acquired, and therefore before anything could be planned or sent.
#: ``auto_trade_execution`` asks for the authority first and returns one of
#: these when it does not get it, so an item carrying one of them proves no
#: request reached the venue -- even though its payload has no ``status`` key
#: for the rule above to read.
#:
#: raw 15668 is why this exists: a real BTC limit long whose item failed with
#: ``entry_revision_exchange_authority_expired_blocked`` -- authority orphaned
#: by another message's revision, not a byte sent -- and which froze as
#: "outcome unknown" because its payload carried a ``message`` and no
#: ``status``. Nothing was ever in doubt about that one.
#:
#: The set is deliberately only the *acquisition* failures. Release failures
#: are excluded on purpose: a release happens after the writes, so a failure
#: there says nothing about whether the venue was contacted.
_PRE_SUBMIT_REFUSAL_REASONS = frozenset(
    {
        "entry_revision_exchange_authority_expired_blocked",
        "entry_revision_exchange_authority_blocked",
        "entry_revision_exchange_authority_busy",
        "entry_revision_exchange_authority_invalid",
        "entry_revision_exchange_authority_missing",
        "entry_revision_exchange_authority_unavailable",
    }
)
#: Payload keys that may carry the refusal reason. A ``RecoveryLiveSubmitError``
#: lands as ``message``; the structured refusals use ``reason``.
_REFUSAL_REASON_KEYS = ("reason", "message")

_KNOWN_UNKNOWN_STATUSES = frozenset(
    {
        "unknown",
        "partial_failed",
        "recovery_required",
        "in_progress",
        "reconciling",
        "operator_required",
        "awaiting_exchange",
        "submit_unknown",
        "unresolved",
        "cancelling_old_entries",
        "cancel_submitting",
        "submitting_replacements",
    }
)


@dataclass(frozen=True, eq=False)
class ExecutionBoundaryOutcome:
    """Internal lease contract; ``public_result`` remains API-compatible."""

    status: str
    exchange_effect: str
    raw_status: str
    reason_code: str | None
    evidence_refs: tuple[dict[str, Any], ...]
    public_result: dict[str, Any]

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed_safe", "outcome_unknown"}:
            raise ValueError("invalid execution boundary status")
        if self.exchange_effect not in {
            "not_started",
            "confirmed_applied",
            "confirmed_rejected",
            "outcome_unknown",
        }:
            raise ValueError("invalid exchange effect")
        if (
            self.exchange_effect == "outcome_unknown"
            and self.status != "outcome_unknown"
        ) or (
            self.exchange_effect != "outcome_unknown"
            and self.status == "outcome_unknown"
        ):
            raise ValueError("execution boundary status/effect mismatch")
        if (
            self.exchange_effect in {"confirmed_applied", "confirmed_rejected"}
            and not self.evidence_refs
        ):
            raise ValueError("confirmed exchange effect requires durable evidence")

    def __getitem__(self, key: str) -> Any:
        return self.public_result[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.public_result.get(key, default)

    def keys(self):
        return self.public_result.keys()

    def items(self):
        return self.public_result.items()

    def __iter__(self):
        return iter(self.public_result)

    def __len__(self) -> int:
        return len(self.public_result)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ExecutionBoundaryOutcome):
            return (
                self.status,
                self.exchange_effect,
                self.raw_status,
                self.reason_code,
                self.evidence_refs,
                self.public_result,
            ) == (
                other.status,
                other.exchange_effect,
                other.raw_status,
                other.reason_code,
                other.evidence_refs,
                other.public_result,
            )
        if isinstance(other, dict):
            return self.public_result == other
        return False


class ExecutionBoundaryTracker:
    """Records only method/result identifiers, never request bodies or headers."""

    def __init__(self) -> None:
        self._writes: list[dict[str, Any]] = []

    @property
    def writes(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._writes)

    def begin(self, method: str) -> int:
        ordinal = len(self._writes) + 1
        self._writes.append(
            {"method": str(method), "ordinal": ordinal, "outcome": "started"}
        )
        return ordinal

    def applied(self, ordinal: int, response: Any) -> None:
        row = self._writes[ordinal - 1]
        row["outcome"] = "confirmed_applied"
        order_id = _response_order_id(response)
        if order_id is not None:
            row["order_id"] = order_id[:128]

    def failed(self, ordinal: int, error: BaseException) -> None:
        row = self._writes[ordinal - 1]
        if isinstance(error, DeepcoinDefiniteRejection):
            row["outcome"] = "confirmed_rejected"
        else:
            # Once a write method is entered, an unclassified transport/runtime
            # failure cannot prove that the venue did not receive the request.
            row["outcome"] = "outcome_unknown"


class TrackedDeepcoinClient:
    """Transparent client proxy that observes every exchange-write method."""

    def __init__(self, client: Any, tracker: ExecutionBoundaryTracker) -> None:
        self._client = client
        self._tracker = tracker

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._client, name)
        if name not in DEEPCOIN_WRITE_METHODS or not callable(target):
            return target

        def tracked(*args: Any, **kwargs: Any) -> Any:
            ordinal = self._tracker.begin(name)
            try:
                response = target(*args, **kwargs)
            except BaseException as exc:
                self._tracker.failed(ordinal, exc)
                raise
            self._tracker.applied(ordinal, response)
            return response

        return tracked


def _response_order_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("ordId", "orderId", "order_id", "clientOrderId", "clOrdId"):
            if value.get(key) not in (None, ""):
                return str(value[key])
        for key in ("data", "result"):
            found = _response_order_id(value.get(key))
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _response_order_id(item)
            if found is not None:
                return found
    return None


def _pre_submit_refusal_reason(payload: dict[str, Any]) -> str | None:
    """The refusal code an item carries, when it proves nothing was sent.

    Exact match against a closed set, on the payload's own reason fields. No
    prefix or substring matching: a reason that merely *mentions* the
    authority is not a proof that the authority was never obtained.
    """

    for key in _REFUSAL_REASON_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value in _PRE_SUBMIT_REFUSAL_REASONS:
            return value
    return None


def _item_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("result", "error"):
        payload = item.get(key)
        if isinstance(payload, dict):
            return payload
    return None


def _items_prove_no_exchange_contact(
    result: dict[str, Any],
) -> tuple[bool, tuple[dict[str, Any], ...]]:
    """Whether every item states, in its own payload, that it never traded.

    A-6. The message-level status rolls several items into one word and loses
    the reason: twelve production attempts read ``completed`` while every item
    said ``{"reason": "kol_or_group_auto_trade_disabled", "status": "skipped"}``,
    and four read ``partial_failed`` while every item said ``status: blocked``
    with a pre-submit refusal reason. Both were frozen as "outcome unknown"
    with no evidence at all, which is the opposite of what the evidence said.

    The proof is per item and it is the item's own structured payload, not the
    rolled-up word: every item must carry a payload whose ``status`` is one the
    project already treats as having no exchange effect. One item without a
    payload, or with an effect-bearing one, and nothing is proven.
    """

    items = result.get("items")
    if not isinstance(items, list) or not items:
        return False, ()
    refs: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return False, ()
        if str(item.get("status") or "") not in _ITEM_FINISHED_STATUSES:
            # Still owned by someone else. Whatever its payload says right now,
            # it is not a finished statement about what did or did not happen.
            return False, ()
        payload = _item_payload(item)
        if payload is None:
            return False, ()
        item_status = str(payload.get("status") or "")
        refusal_reason = _pre_submit_refusal_reason(payload)
        if item_status not in _ITEM_TERMINAL_NO_CONTACT_STATUSES and (
            refusal_reason is None
        ):
            return False, ()
        item_id = item.get("item_id")
        refs.append(
            {
                "kind": "instruction_item_no_exchange_contact",
                **(
                    {"pre_submit_refusal": refusal_reason}
                    if refusal_reason is not None
                    else {}
                ),
                **(
                    {"item_id": int(item_id)}
                    if isinstance(item_id, int) and not isinstance(item_id, bool)
                    else {}
                ),
                "instruction_kind": str(item.get("instruction_kind") or ""),
                "item_status": str(item.get("status") or ""),
                "payload_status": item_status,
                # A ``RecoveryLiveSubmitError`` carries its reason as
                # ``message``; without that fallback the evidence would name
                # the refusal in one field and leave this one blank.
                "reason": str(
                    payload.get("reason")
                    or refusal_reason
                    or item.get("reason")
                    or ""
                ),
            }
        )
    return True, tuple(refs)


def _items_are_all_unfinished(result: dict[str, Any]) -> bool:
    """Whether every item is still owned by an asynchronous path.

    ``in_progress`` means the lease handed the work to a batch or an entry
    queue and returned. Those paths carry their own ledger, their own retries
    and their own alerting; the lease itself issued no request, so freezing the
    *message* on their behalf only guarantees it is never looked at again.
    """

    items = result.get("items")
    if not isinstance(items, list) or not items:
        return False
    return all(
        isinstance(item, dict)
        and str(item.get("status") or "") in _ITEM_UNFINISHED_STATUSES
        for item in items
    )


def build_execution_boundary_outcome(
    public_result: dict[str, Any],
    tracker: ExecutionBoundaryTracker,
    *,
    canonical_item_evidence: tuple[dict[str, Any], ...] = (),
) -> ExecutionBoundaryOutcome:
    """Normalize one adapter run without inferring from a raw status alone."""

    result = dict(public_result)
    raw_status = str(result.get("status") or "unknown")
    reason = result.get("reason")
    reason_code = str(reason)[:256] if reason is not None else None
    writes = tracker.writes
    item_refs = _canonical_item_evidence(result, canonical_item_evidence)
    item_effect = _aggregate_canonical_item_effect(item_refs)
    write_outcomes = {str(item["outcome"]) for item in writes}
    if "outcome_unknown" in write_outcomes or "started" in write_outcomes:
        exchange_effect = "outcome_unknown"
    elif len(writes) == 1 and write_outcomes == {"confirmed_rejected"}:
        exchange_effect = "confirmed_rejected"
    elif write_outcomes == {"confirmed_applied"} and raw_status in _KNOWN_EFFECT_STATUSES:
        exchange_effect = "confirmed_applied"
    elif writes:
        # Mixed applied/rejected legs or a return status that contradicts an
        # observed applied write cannot prove the aggregate venue outcome.
        exchange_effect = "outcome_unknown"
    elif raw_status == "completed" and item_refs:
        exchange_effect = item_effect
    elif not writes and raw_status in _KNOWN_NO_EFFECT_STATUSES:
        exchange_effect = "not_started"
    else:
        # Unknown return vocabularies and effect-bearing statuses without a
        # tracked write are deliberately frozen rather than guessed.
        exchange_effect = "outcome_unknown"

    if raw_status in _KNOWN_UNKNOWN_STATUSES:
        exchange_effect = "outcome_unknown"
    elif raw_status in _KNOWN_EFFECT_STATUSES and not writes and not item_refs:
        exchange_effect = "outcome_unknown"
    elif raw_status not in (
        _KNOWN_NO_EFFECT_STATUSES | _KNOWN_EFFECT_STATUSES | _KNOWN_UNKNOWN_STATUSES
    ):
        exchange_effect = "outcome_unknown"

    # A-6. Only reached with no tracked write: everything above this point,
    # and every path where ``writes`` is non-empty, is unchanged.
    deterministic_refs: tuple[dict[str, Any], ...] = ()
    handed_off = False
    if not writes:
        proven, deterministic_refs = _items_prove_no_exchange_contact(result)
        if proven and exchange_effect == "outcome_unknown":
            exchange_effect = "not_started"
        elif (
            not proven
            and exchange_effect == "outcome_unknown"
            and raw_status == "in_progress"
            and _items_are_all_unfinished(result)
        ):
            exchange_effect = "not_started"
            handed_off = True

    if exchange_effect == "outcome_unknown":
        status = "outcome_unknown"
    elif exchange_effect == "not_started" and (
        raw_status == "failed"
        or (deterministic_refs and raw_status in _RETRIABLE_REFUSAL_STATUSES)
    ):
        # A deterministic refusal is a fact, not an unknown: record it as
        # failed_safe so the message can be tried again once whatever blocked
        # it is resolved, and carry each item's own reason as the evidence.
        status = "failed_safe"
    else:
        status = "completed"
    if handed_off:
        result["handed_off_from_status"] = raw_status
        result["status"] = "completed"
        result["reason"] = "handed_off_to_batch"
    evidence_refs = deterministic_refs + tuple(
        {
            "kind": "deepcoin_write",
            "method": str(item["method"]),
            "ordinal": int(item["ordinal"]),
            # A-6b: without the per-write outcome, a frozen attempt cannot say
            # whether a request reached the venue and went unanswered or was
            # definitely rejected -- which is the whole question a person has
            # to settle by hand afterwards.
            "outcome": str(item.get("outcome") or "started"),
            **(
                {"order_id": str(item["order_id"])}
                if item.get("order_id") is not None
                else {}
            ),
        }
        for item in writes
    ) + item_refs
    return ExecutionBoundaryOutcome(
        status=status,
        exchange_effect=exchange_effect,
        raw_status=raw_status,
        reason_code=reason_code,
        evidence_refs=evidence_refs,
        public_result=result,
    )


def _canonical_item_evidence(
    result: dict[str, Any],
    evidence: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Accept only complete, verified execution-contract evidence."""

    items = result.get("items")
    if not isinstance(items, list) or not items:
        return ()
    item_ids: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            return ()
        item_id = item.get("item_id")
        item_status = str(item.get("status") or "")
        if (
            not isinstance(item_id, int)
            or isinstance(item_id, bool)
            or item_status not in {"submitted", "succeeded"}
        ):
            return ()
        item_ids.add(int(item_id))
    refs: list[dict[str, Any]] = []
    observed_ids: set[int] = set()
    for ref in evidence:
        if not isinstance(ref, dict):
            return ()
        item_id = ref.get("item_id")
        contract_id = ref.get("contract_id")
        attempted = ref.get("attempted_exchange_write")
        terminal_kind = ref.get("terminal_kind")
        completion_scope = ref.get("completion_scope")
        if (
            ref.get("kind") != "instruction_execution_contract"
            or not isinstance(item_id, int)
            or isinstance(item_id, bool)
            or not isinstance(contract_id, int)
            or isinstance(contract_id, bool)
            or not isinstance(attempted, bool)
            or terminal_kind not in {
                "verified_entry",
                "verified_management",
                "verified_cancel",
                "verified_exit",
                "verified_refusal",
            }
            or completion_scope not in {"full", "partial"}
            or int(item_id) in observed_ids
        ):
            return ()
        observed_ids.add(int(item_id))
        refs.append(
            {
                "kind": "instruction_execution_contract",
                "contract_id": int(contract_id),
                "item_id": int(item_id),
                "attempted_exchange_write": attempted,
                "terminal_kind": str(terminal_kind),
                "completion_scope": str(completion_scope),
            }
        )
    if observed_ids != item_ids:
        return ()
    return tuple(refs)


def _aggregate_canonical_item_effect(
    refs: tuple[dict[str, Any], ...],
) -> str:
    effects: set[str] = set()
    for ref in refs:
        if ref["completion_scope"] != "full":
            return "outcome_unknown"
        if not ref["attempted_exchange_write"]:
            effects.add("not_started")
        elif ref["terminal_kind"] == "verified_refusal":
            effects.add("confirmed_rejected")
        else:
            effects.add("confirmed_applied")
    exchange_effects = effects - {"not_started"}
    if not exchange_effects:
        return "not_started"
    if len(exchange_effects) == 1:
        return next(iter(exchange_effects))
    return "outcome_unknown"

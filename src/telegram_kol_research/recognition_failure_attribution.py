"""Why a recognised message produced no lifecycle change.

A-8. One branch in ``message_recognition`` used to answer this question with a
single label, ``识别失败`` / "MiMo lifecycle event could not be applied safely",
and the A-8 inventory showed what that label was hiding. Of 181 such rows in
auto_trade groups, **none** was a recognition failure: MiMo answered every
time. What varied was what happened next, and the four outcomes need different
handling:

* the message says nothing to act on ("继续拿着不变", "带好止盈止损") -- 29 rows;
* the model named no target at all -- 55 rows;
* the target it named cannot be verified: a lifecycle with no execution
  binding, or one that had already exited -- 51 rows;
* a real instruction on a real live position that we nonetheless failed to
  apply -- the residue, and the only one that deserves the word "failed".

Collapsing all four into "识别失败" cost twice. It hid the real losses (ten
management instructions, including two stop-loss resets and a "take half off
and protect the rest"), and it fed a wrong diagnosis: A-7 task 4 was written
around a "notify_only special case" that the data does not support.

This module answers only the question of *which* of the four happened. It
decides nothing about execution and touches nothing -- the caller records the
verdict and the alerting layer decides what a human needs to see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Nothing in the message asks for an action. Not a failure; not alerted.
NO_ACTIONABLE_INTENT = "no_actionable_intent"

#: The model named no lifecycle to act on. Not alerted: without a target there
#: is nothing to lose track of, and these stopped occurring after 2026-07-27.
NO_TARGET_NAMED = "no_target_named"

#: A real instruction aimed at a lifecycle we cannot stand behind -- no
#: execution binding, or already exited. Alerted: an instruction was real and
#: went nowhere.
TARGET_NOT_VERIFIABLE = "target_not_verifiable"

#: The v2 instruction contract rejected the payload. Alerted.
CONTRACT_INVALID = "contract_invalid"

#: A real instruction, a target we could verify, and it still did not apply.
#: The only one that is genuinely a failure, and the one worth waking someone
#: for. The A-8 inventory found zero of these, which is why it needs an alarm:
#: if it starts happening, nobody would otherwise notice.
APPLY_FAILED = "lifecycle_apply_failed"

#: Reasons a person is told about, in auto_trade groups only.
ALERTED_REASONS = frozenset({CONTRACT_INVALID, TARGET_NOT_VERIFIABLE, APPLY_FAILED})

#: The prefix that carries a verdict through ``message_recognitions.reason``.
REASON_PREFIX = "authoritative_lifecycle_not_applied"


@dataclass(frozen=True, slots=True)
class LifecycleApplicationVerdict:
    reason_code: str
    detail: str

    @property
    def recognition_reason(self) -> str:
        return f"{REASON_PREFIX}:{self.reason_code}"


def classify_unapplied_lifecycle_event(
    *,
    intent: str | None,
    target_lifecycle_id: int | None,
    target_verified: bool | None,
    target_detail: str = "",
) -> LifecycleApplicationVerdict:
    """Name the outcome, most-forgiving first.

    Order matters and is not arbitrary. ``no_actionable_intent`` is asked
    first because when the message asks for nothing, the state of the target
    cannot make it a loss -- alerting on a "hold what you have" aimed at a
    ghost would be noise, and noise is what buried the real cases last time.
    Only once we know a real action was asked for does the target's
    verifiability decide between "we could not aim it" and "we aimed it and
    still failed".
    """

    if not intent or intent == "none":
        return LifecycleApplicationVerdict(NO_ACTIONABLE_INTENT, intent or "none")
    if target_lifecycle_id is None:
        return LifecycleApplicationVerdict(NO_TARGET_NAMED, intent)
    if target_verified is not True:
        return LifecycleApplicationVerdict(
            TARGET_NOT_VERIFIABLE, target_detail or "unverified"
        )
    return LifecycleApplicationVerdict(APPLY_FAILED, intent)


def reason_code_from_recognition_reason(reason: Any) -> str | None:
    """Read a verdict back out of a stored recognition row.

    Contract rejections are written by a different call site with their own
    prefix, so both shapes are recognised here and nowhere else.
    """

    text = str(reason or "")
    if text.startswith("authoritative_instruction_contract_invalid"):
        return CONTRACT_INVALID
    if text.startswith(f"{REASON_PREFIX}:"):
        code = text.split(":", 1)[1].strip()
        return code or None
    return None

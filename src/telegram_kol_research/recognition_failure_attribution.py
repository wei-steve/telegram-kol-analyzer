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

#: A-8b. A-8 split the one branch that reported "the lifecycle event did not
#: apply", and left every *other* writer of ``识别失败`` falling back to the
#: legacy blanket reason -- the fallback that exists so pre-A-8 rows keep
#: their meaning. raw 15702 and 15703 landed that way on 2026-09-09, hours
#: after A-8 shipped, which is how the gap was found. These are the rest of
#: the refusals, each with its own name.
#:
#: A fraction we could read as a number but not as a share of the position:
#: "close some" with no proportion, or a proportion that contradicts the
#: text. A real management instruction, refused rather than guessed at.
MANAGEMENT_FRACTION_INVALID = "management_fraction_invalid"

#: The model named one symbol while every price in the message belongs to
#: another -- BTC with entries in the ETH thousands. The strategy is real and
#: is sent to manual review; before this nobody was told it existed.
SYMBOL_PRICE_SCALE_CONFLICT = "symbol_price_scale_conflict"

#: The message was an image we could not read at all: the file never
#: downloaded, or OCR returned nothing. In a group that trades, an unread
#: image can be an entry nobody ever saw.
MEDIA_UNREADABLE = "media_unreadable"

#: step-18. The authoritative model produced no decision at all: the call
#: failed, or the message aged out of the recovery window before one was
#: produced. Neither reason was in ``ALERTED_REASONS``, so on 2026-09-12 fourteen
#: hours of ``402 Payment Required`` -- 16 unrecognised messages in auto_trade
#: groups -- paged nobody. They are not benign outcomes in the A-8 sense:
#: nothing was refused because the message asked for nothing; nothing was read.
MIMO_AUTHORITATIVE_FAILED = "mimo_authoritative_failed"
GAP_RECOVERY_EXPIRED = "authoritative_gap_recovery_expired"

#: Every reason meaning "no authoritative decision was produced".
#:
#: A narrowing that reads as reasonable can silently exclude the one case that
#: matters most, and nothing reports the exclusion -- the third time this shape
#: has cost us (A-10b ``pos_id``, A-15-0 ``limit``, step-18). So membership is
#: not left to whoever edits ``ALERTED_REASONS``: a traversal test finds every
#: writer of a terminal ``authoritative_failed`` decision, requires each to be
#: registered in ``AUTHORITY_NOT_PRODUCED_WRITERS`` with the reason it records,
#: and requires every such reason to be alerted.
AUTHORITY_NOT_PRODUCED_REASONS = frozenset(
    {MIMO_AUTHORITATIVE_FAILED, GAP_RECOVERY_EXPIRED}
)

#: ``module.function`` of each writer of a terminal ``authoritative_failed``
#: decision, and the automation reason that outcome is recorded under.
AUTHORITY_NOT_PRODUCED_WRITERS: dict[str, str] = {
    "authoritative_recognition.assess_message_authoritatively": (
        MIMO_AUTHORITATIVE_FAILED
    ),
    "telegram_live_listener._record_expired_authoritative_recovery_gap_in_session": (
        GAP_RECOVERY_EXPIRED
    ),
}

#: Reasons a person is told about, in auto_trade groups only.
#:
#: Every one of them means "something real was refused, or could not be read".
#: The two benign outcomes stay out, because the A-8 inventory is an account
#: of what happens when an alarm fires on those as well: the losses that
#: mattered spent two months buried under them.
ALERTED_REASONS = frozenset(
    {
        CONTRACT_INVALID,
        TARGET_NOT_VERIFIABLE,
        APPLY_FAILED,
        MANAGEMENT_FRACTION_INVALID,
        SYMBOL_PRICE_SCALE_CONFLICT,
        MEDIA_UNREADABLE,
        *AUTHORITY_NOT_PRODUCED_REASONS,
    }
)

#: Recognition reasons that name their own refusal, mapped to the code that
#: reports it. Matched by prefix because several carry a human sentence after
#: the code -- ``symbol_price_scale_conflict: MiMo 输出 BTC，但...``.
_REASON_PREFIX_CODES: tuple[tuple[str, str], ...] = (
    ("authoritative_instruction_contract_invalid", CONTRACT_INVALID),
    ("management_fraction_invalid", MANAGEMENT_FRACTION_INVALID),
    ("symbol_price_scale_conflict", SYMBOL_PRICE_SCALE_CONFLICT),
)

#: The image refusals write a Chinese sentence rather than a code, so they are
#: recognised by the phrases those sentences are built from.
_MEDIA_REASON_MARKERS: tuple[str, ...] = (
    "图片识别失败",
    "图片文件未下载",
    "图片未能下载",
)

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
    if text.startswith(f"{REASON_PREFIX}:"):
        code = text.split(":", 1)[1].strip()
        return code or None
    for prefix, code in _REASON_PREFIX_CODES:
        if text.startswith(prefix):
            return code
    if any(marker in text for marker in _MEDIA_REASON_MARKERS):
        return MEDIA_UNREADABLE
    return None

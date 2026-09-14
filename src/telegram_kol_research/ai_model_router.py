"""Try the models a stage is bound to, in order, until one answers.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §4.

The rule the user asked for is the simple one -- "if the first does not work,
use the second" -- so this is OpenMinis' ``FallbackStrategy.always`` rather
than its ``limited`` variant, with exactly one exception: **a failure that
happened before the request left this process is not the provider's fault**
(unreadable media, a payload that would not assemble), and swapping models
cannot help it. Everything after the request was sent does move on: transport
errors, timeouts including the 240 s total deadline, any HTTP status, an empty
body, unparseable JSON, a contract that does not validate.

Two things this module does **not** change:

* Per-model retry. A model's own retry policy (MiMo's
  ``MIMO_AUTHORITATIVE_MAX_ATTEMPTS``) runs to completion inside that model
  before the next one is tried.
* Recognition semantics. Which model answered changes nothing about what the
  answer means.

The time budget is the constraint that makes this safe for the authoritative
path. ``MIMO_REQUEST_TOTAL_DEADLINE_SECONDS`` (240 s) plus one blocked read
(60 s) has to stay inside the 300 s job-claim lease, so the whole **chain**
shares that 240 s rather than each model getting its own: every request is
given ``min(budget, remaining)``, and a model is not started at all with less
than :data:`MIN_REMAINING_SECONDS` left.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiRecognitionConfig,
    resolve_stage_models,
)


#: Below this much of the chain budget, starting another model buys nothing:
#: the request would be cut off before a provider could realistically answer,
#: and the failure would look like the provider's rather than the clock's.
MIN_REMAINING_SECONDS = 20.0

#: A provider response body appended to an error message, which runs to the
#: end of the string (see :meth:`ModelFailure.describe`).
_RESPONSE_BODY_TAIL = re.compile(r"(?i);?\s*\bresponse_body\s*=.*$")


@dataclass(frozen=True, slots=True)
class ModelFailure:
    """Why one model in the chain did not produce the answer."""

    model_id: str
    model: str
    message: str

    def describe(self) -> str:
        """This model's summary, safe to put next to another model's.

        ``_call_mimo_direct_model`` appends the provider's response body to an
        HTTP error, and the audit's secret scrubber redacts everything after
        ``response_body=`` to the end of the string. Concatenated as-is, the
        first model's body would therefore swallow every model after it and
        the error would name only one. The body is redacted in the audit
        anyway, so it is cut here before the join.
        """

        summary = _RESPONSE_BODY_TAIL.sub("", self.message).strip()
        return f"model {self.model_id}: {summary}"


@dataclass(frozen=True, slots=True)
class RouterResult:
    """What the chain did: who answered, who was abandoned, and why."""

    succeeded: bool
    model: AiModelConfig | None = None
    value: Any = None
    #: Model ids whose failure caused the router to move on to the next model.
    fallback_from: tuple[str, ...] = ()
    #: One entry per model that was tried and failed, in order.
    failures: tuple[ModelFailure, ...] = ()
    #: Model ids never started because the chain budget was spent.
    skipped_for_budget: tuple[str, ...] = ()

    @property
    def used_fallback(self) -> bool:
        return bool(self.fallback_from)

    @property
    def error_message(self) -> str | None:
        """Every model's own failure summary, or ``None`` if one answered.

        A single-model chain reports exactly the message that model raised, so
        nothing about existing failure text changes when no fallback is
        configured. A longer chain names each model, because "it failed" with
        two models in play does not say which.
        """

        if self.succeeded or not self.failures:
            return None
        if len(self.failures) == 1:
            return self.failures[0].message
        return " | ".join(failure.describe() for failure in self.failures)


def resolve_stage_chain(
    config: AiRecognitionConfig,
    stage_key: str,
) -> list[AiModelConfig]:
    """The ordered, usable models bound to one stage.

    An empty list means the stage has no model it can call -- which behaves
    exactly as "the provider is not configured" always has.
    """

    return resolve_stage_models(config, stage_key)


def always_fallback(error: BaseException) -> bool:
    return True


def request_reached_provider(error: BaseException) -> bool:
    """The default classifier for provider calls made by this project.

    ``_call_mimo_direct_model`` attaches a
    ``MimoProviderAttemptTelemetry`` to everything it raises, whose
    ``provider_request_made`` is ``False`` only for failures that happened
    before the request was sent. Anything without that marker is assumed to
    have reached the provider, because trying the next model is the safer
    guess when we cannot tell.
    """

    telemetry = getattr(error, "mimo_provider_attempt_telemetry", None)
    made = getattr(telemetry, "provider_request_made", None)
    if made is None:
        return True
    return bool(made)


async def async_run_with_fallback(
    chain: Sequence[AiModelConfig],
    attempt: Callable[..., Any],
    *,
    budget_seconds: float | None = None,
    min_remaining_seconds: float = MIN_REMAINING_SECONDS,
    classify: Callable[[BaseException], bool] = always_fallback,
    monotonic: Callable[[], float] = time.monotonic,
) -> RouterResult:
    """:func:`run_with_fallback` for an ``await``-able attempt.

    Same decisions, same :class:`RouterResult`; the only difference is that
    ``attempt`` is awaited. It exists because ``strategy_alert`` runs on the
    event loop, and pushing its HTTP call through ``to_thread`` only to get a
    fallback would put a blocking client where an async one already works. A
    test pins the two against each other.
    """

    started = monotonic()
    failures: list[ModelFailure] = []
    fallback_from: list[str] = []
    models = list(chain)
    for index, model in enumerate(models):
        deadline: float | None = None
        if budget_seconds is not None:
            remaining = float(budget_seconds) - (monotonic() - started)
            if index > 0 and remaining < float(min_remaining_seconds):
                return RouterResult(
                    succeeded=False,
                    fallback_from=tuple(fallback_from),
                    failures=tuple(failures),
                    skipped_for_budget=tuple(item.id for item in models[index:]),
                )
            deadline = max(0.0, min(float(budget_seconds), remaining))
        try:
            value = await attempt(model, deadline_seconds=deadline)
        except Exception as exc:  # noqa: BLE001 - the chain decides, not the type
            failures.append(
                ModelFailure(
                    model_id=model.id,
                    model=model.model,
                    message=str(exc) or type(exc).__name__,
                )
            )
            if not classify(exc):
                break
            if index + 1 < len(models):
                fallback_from.append(model.id)
            continue
        return RouterResult(
            succeeded=True,
            model=model,
            value=value,
            fallback_from=tuple(fallback_from),
            failures=tuple(failures),
        )
    return RouterResult(
        succeeded=False,
        fallback_from=tuple(fallback_from),
        failures=tuple(failures),
    )


def run_with_fallback(
    chain: Sequence[AiModelConfig],
    attempt: Callable[..., Any],
    *,
    budget_seconds: float | None = None,
    min_remaining_seconds: float = MIN_REMAINING_SECONDS,
    classify: Callable[[BaseException], bool] = always_fallback,
    monotonic: Callable[[], float] = time.monotonic,
) -> RouterResult:
    """Call ``attempt(model, deadline_seconds=...)`` down the chain.

    ``attempt`` returns the answer or raises. ``deadline_seconds`` is the
    wall-clock ceiling this model may use -- ``None`` when no budget was
    given. ``classify`` decides whether one failure should move to the next
    model; returning ``False`` ends the chain with that failure recorded.

    The first model always runs, however little budget is left: a chain that
    refuses to try anything is worse than one late request.
    """

    started = monotonic()
    failures: list[ModelFailure] = []
    fallback_from: list[str] = []
    models = list(chain)
    for index, model in enumerate(models):
        deadline: float | None = None
        if budget_seconds is not None:
            remaining = float(budget_seconds) - (monotonic() - started)
            if index > 0 and remaining < float(min_remaining_seconds):
                return RouterResult(
                    succeeded=False,
                    fallback_from=tuple(fallback_from),
                    failures=tuple(failures),
                    skipped_for_budget=tuple(
                        item.id for item in models[index:]
                    ),
                )
            deadline = max(0.0, min(float(budget_seconds), remaining))
        try:
            value = attempt(model, deadline_seconds=deadline)
        except Exception as exc:  # noqa: BLE001 - the chain decides, not the type
            failures.append(
                ModelFailure(
                    model_id=model.id,
                    model=model.model,
                    message=str(exc) or type(exc).__name__,
                )
            )
            if not classify(exc):
                break
            if index + 1 < len(models):
                fallback_from.append(model.id)
            continue
        return RouterResult(
            succeeded=True,
            model=model,
            value=value,
            fallback_from=tuple(fallback_from),
            failures=tuple(failures),
        )
    return RouterResult(
        succeeded=False,
        fallback_from=tuple(fallback_from),
        failures=tuple(failures),
    )

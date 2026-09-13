"""A daily one-token question to the MiMo provider: can it answer right now.

2026-09-12 (A line, step-18): the balance ran out at 03:00Z and the only way
anyone found out the provider worked again was a hand-made ``max_tokens=1``
request after the top-up. The outage detector in ``mimo_provider_health`` reads
the recognition audit, so it knows only what recognition happened to try; this
probe asks the provider directly, once a day and once per worker start, and
leaves a journal line either way.

It deliberately stays outside recognition:

* **No business rows.** It writes neither ``mimo_recognition_attempts`` nor
  ``ai_prompt_invocations``. A probe answer is therefore not recovery evidence
  for the outage derivation, by design (step 4 ruling): one token answering
  does not prove a recognition request would, and a 400-class defect is
  invisible to it.
* **The same provider settings as recognition.** The model comes from the same
  ``ai_recognition.yaml`` through ``_find_mimo_model``; the key travels only in
  the request header and never reaches a log line or an incident summary --
  failures are reported by class, kind, HTTP status and exception *type*.

What counts as an answer was checked against the production provider on
2026-09-13: ``200`` with one choice whose ``message.content`` is the empty
string and ``finish_reason`` is ``length``. So a non-empty ``choices`` list is
the whole test; demanding content would call every healthy probe a failure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.mimo_provider_health import (
    RESPONSE_INVALID,
    _aware,
    _default_capture,
    _incident_recorded,
    classify_provider_failure,
)


logger = logging.getLogger(__name__)

PROBE_FAILED_INCIDENT_TYPE = "mimo_provider_probe_failed"
PROBE_INTERVAL = timedelta(hours=24)
PROBE_TIMEOUT_SECONDS = 30.0
PROBE_MAX_TOKENS = 1

#: Failure classes of the probe that are not the provider's doing.
MODEL_NOT_CONFIGURED = "mimo_model_not_configured"
CONFIG_UNREADABLE = "ai_config_unreadable"


class ProbeResponseInvalid(ValueError):
    """A 2xx answer without the ``choices`` list every chat completion has."""


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    ok: bool
    latency_ms: int
    http_status: int | None = None
    failure_class: str | None = None
    kind: str | None = None
    error_type: str | None = None


def probe_mimo_provider(
    model_config: Any,
    *,
    client_factory: Callable[..., Any] = httpx.Client,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeOutcome:
    """Send one ``max_tokens=1`` chat completion and classify the result."""

    headers = {"Content-Type": "application/json"}
    if model_config.api_key:
        headers["Authorization"] = f"Bearer {model_config.api_key}"
    started = time.perf_counter()
    http_status: int | None = None
    try:
        with client_factory(timeout=timeout_seconds) as client:
            response = client.post(
                f"{model_config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": model_config.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": PROBE_MAX_TOKENS,
                },
            )
            http_status = int(response.status_code)
            response.raise_for_status()
            data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ProbeResponseInvalid("probe answer carries no choices")
    except Exception as exc:
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        failure = classify_provider_failure(exc)
        failure_class = failure.failure_class if failure is not None else None
        if failure_class is None and isinstance(exc, ValueError):
            # Undecodable JSON or no choices: the provider answered, badly.
            failure_class = RESPONSE_INVALID
        return ProbeOutcome(
            ok=False,
            latency_ms=latency_ms,
            http_status=(
                failure.http_status
                if failure is not None and failure.http_status is not None
                else http_status
            ),
            failure_class=failure_class,
            kind=failure.kind if failure is not None else None,
            error_type=type(exc).__name__,
        )
    return ProbeOutcome(
        ok=True,
        latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        http_status=http_status,
    )


def run_mimo_provider_probe(
    session_factory: sessionmaker,
    *,
    config_loader: Callable[[], Any],
    now: datetime | None = None,
    probe: Callable[..., ProbeOutcome] = probe_mimo_provider,
    capture: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Probe once; log the outcome; raise ``mimo_provider_probe_failed`` at
    most once per UTC day. Meant to run in a thread.

    A configuration that cannot be read, or has no MiMo model, is reported as
    a failed probe: recognition cannot work either, and it would otherwise be
    a skipped check that looks like a healthy one.
    """

    current = _aware(now or datetime.now(UTC))
    try:
        config = config_loader()
    except Exception as exc:
        outcome = ProbeOutcome(
            ok=False,
            latency_ms=0,
            failure_class=CONFIG_UNREADABLE,
            error_type=type(exc).__name__,
        )
    else:
        from telegram_kol_research.recognition_experiments import _find_mimo_model

        model = _find_mimo_model(config)
        if model is None:
            outcome = ProbeOutcome(ok=False, latency_ms=0, failure_class=MODEL_NOT_CONFIGURED)
        else:
            outcome = probe(model)
    if outcome.ok:
        logger.info(
            "mimo provider probe ok http_status=%s latency_ms=%s",
            outcome.http_status,
            outcome.latency_ms,
        )
        return {
            "state": "probe_ok",
            "http_status": outcome.http_status,
            "latency_ms": outcome.latency_ms,
        }
    logger.warning(
        "mimo provider probe failed failure_class=%s kind=%s http_status=%s "
        "error_type=%s latency_ms=%s",
        outcome.failure_class,
        outcome.kind,
        outcome.http_status,
        outcome.error_type,
        outcome.latency_ms,
    )
    source_record_id = f"probe_{current:%Y%m%d}"
    if _incident_recorded(
        session_factory,
        incident_type=PROBE_FAILED_INCIDENT_TYPE,
        source_record_id=source_record_id,
    ):
        return {"state": "probe_failed_already_alerted", "failure_class": outcome.failure_class}
    (capture or _default_capture("capture_mimo_provider_probe_failed"))(
        session_factory,
        outcome=outcome,
        source_record_id=source_record_id,
        occurred_at=current,
    )
    return {"state": "probe_failed_alerted", "failure_class": outcome.failure_class}

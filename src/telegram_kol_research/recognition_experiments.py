"""Side-channel AI recognition experiments."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_endpoints import (
    chat_completions_url,
    provider_append_v1,
)
from telegram_kol_research.ai_model_router import (
    MIN_REMAINING_SECONDS,
    resolve_stage_chain,
    run_with_fallback,
)
from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiRecognitionConfig,
    load_ai_recognition_config,
)
from telegram_kol_research.ai_stage_catalog import AUTHORITATIVE_STAGE
from telegram_kol_research.contextual_message_window import (
    build_contextual_message_window,
    render_authoritative_context,
)
from telegram_kol_research.media_retention import resolve_media_path
from telegram_kol_research.message_evidence import (
    build_current_message_input_fingerprint,
    build_message_input_fingerprint,
)
from telegram_kol_research.mimo_recognition_runs import (
    complete_mimo_run,
    record_mimo_attempt,
    start_mimo_run,
)
from telegram_kol_research.mimo_v2_contract import (
    MimoV2ContractError,
    MimoV2Result,
    parse_mimo_v2_payload,
)
from telegram_kol_research.mimo_v2_execution_adapter import (
    AdaptedMimoV2Payload,
    MimoV2ExecutionAdapterError,
    _execution_projection,
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import MediaAsset, RawMessage, RecognitionExperiment, utc_now
from telegram_kol_research.prompt_composition import compose_trading_prompt
from telegram_kol_research.prompt_defaults import seed_default_prompt_registry
from telegram_kol_research.prompt_registry import (
    PromptInvocationRecord,
    record_prompt_invocation,
)


MIMO_DIRECT_EXPERIMENT_NAME = "mimo_direct_v1"
MIMO_DIRECT_PROMPT_VERSION = "mimo_direct_v1"
MIMO_AUTHORITATIVE_PROMPT_VERSION = "mimo_authoritative_v1"
MIMO_V2_CONTRACT_VERSION = "mimo-authoritative-v2"
MIMO_AUTHORITATIVE_MAX_ATTEMPTS = 2
MIMO_AUTHORITATIVE_RETRY_DELAY_SECONDS = 1.0
MIMO_V2_MAX_ATTEMPTS = 3
MIMO_V2_MAX_RETRY_DELAY_SECONDS = 60.0
MIMO_EXPERIMENT_STATUSES = {
    "是策略",
    "非策略",
    "识别失败",
    "入场确认",
    "取消入场",
    "离场信号",
    "仓位管理",
    "策略调整",
}

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ExperimentRunStats:
    considered: int = 0
    skipped_existing: int = 0
    skipped_no_input: int = 0
    succeeded: int = 0
    failed: int = 0


@dataclass(frozen=True)
class MimoAuthoritativeResult:
    raw_message_id: int
    payload: dict[str, Any]
    input_kind: str
    model: str
    status: str
    error_message: str | None = None
    prompt_versions: dict[str, int] = field(default_factory=dict)
    contract_version: str = "v1"
    run_id: int | None = None
    fallback_from: str | None = None
    projection_fingerprint: str | None = None
    provider_attempt_telemetry: tuple[MimoProviderAttemptTelemetry, ...] = ()
    #: One entry per model of the stage chain that was actually tried, in
    #: order. The v1 audit writes one ``mimo_recognition_attempts`` row per
    #: entry, so a fallback is visible as its own attempt rather than hidden
    #: inside the aggregate of the model that failed.
    model_attempts: tuple["MimoModelAttempt", ...] = ()

    @property
    def is_actionable(self) -> bool:
        if self.error_message or self.status == "识别失败":
            return False
        lifecycle = self.payload.get("lifecycle_event")
        if isinstance(lifecycle, dict):
            event_type = str(lifecycle.get("event_type") or "none")
            if event_type != "none" and float(lifecycle.get("confidence") or 0.0) >= 0.7:
                return True
        return self.status == "是策略" and float(self.payload.get("confidence") or 0.0) >= 0.7


@dataclass(frozen=True, slots=True)
class MimoModelAttempt:
    """What one model of the chain did, including its own retries."""

    model_id: str
    model: str
    succeeded: bool
    error_message: str | None
    telemetry: tuple[MimoProviderAttemptTelemetry, ...]
    started_at: Any
    completed_at: Any
    duration_ms: int


@dataclass(frozen=True, slots=True)
class MimoV2InferenceResult:
    raw_message_id: int
    run_id: int
    parsed_result: MimoV2Result | None
    adapted_result: AdaptedMimoV2Payload | None
    input_kind: str
    model: str
    prompt_versions: dict[str, int]
    error_code: str | None = None
    error_message: str | None = None
    response_size_bytes: int = 0

    @property
    def succeeded(self) -> bool:
        return self.parsed_result is not None and self.error_code is None


class _MimoV2InvalidJson(ValueError):
    def __init__(self, message: str, *, response_payload: Any | None = None):
        super().__init__(message)
        self.response_payload = response_payload


class _MimoModelFailed(RuntimeError):
    """One model in the chain is finished, including its own retries.

    ``request_made`` is what decides whether the next model is tried: a
    failure that never left this process says nothing about the provider, and
    changing model cannot fix it (design §4).
    """

    def __init__(self, message: str, *, request_made: bool):
        super().__init__(message)
        self.request_made = bool(request_made)


def _should_try_next_model(error: BaseException) -> bool:
    if isinstance(error, _MimoModelFailed):
        return error.request_made
    return True


class _OrdinalCounter:
    """Attempt ordinals run across the whole chain, not per model.

    ``record_mimo_attempt`` enforces "the next ordinal", and the audit is one
    append-only sequence per run, so model B's first request is ordinal 3 when
    model A used two.
    """

    def __init__(self) -> None:
        self._value = 0

    def next(self) -> int:
        self._value += 1
        return self._value


@dataclass(frozen=True, slots=True)
class _V2Success:
    attempt_ordinal: int
    payload: dict[str, Any]
    parsed: Any
    adapted: Any
    response_payload: Any


@dataclass(frozen=True, slots=True)
class _V2Terminal:
    """A verdict that ends the chain rather than moving to the next model."""

    result: "MimoV2InferenceResult"


def resolve_authoritative_chain(
    config: AiRecognitionConfig,
) -> list[AiModelConfig]:
    """The models bound to ``authoritative_recognition``, in order.

    A configuration that carries no v2 model table at all (one built by hand
    in a test, or by code that predates the stage bindings) falls back to the
    rule this function replaced: the entry whose id or model name is
    ``mimo-v2.5``.
    """

    chain = resolve_stage_chain(config, AUTHORITATIVE_STAGE)
    if chain or (getattr(config, "models", None) or ()):
        return chain
    legacy = _legacy_mimo_model(config)
    return [legacy] if legacy is not None else []


def _legacy_mimo_model(config: AiRecognitionConfig) -> AiModelConfig | None:
    for model in getattr(config, "ai_models", ()) or ():
        if model.id == "mimo-v2.5" or model.model == "mimo-v2.5":
            return model
    return None


def _remaining_deadline(
    deadline_seconds: float | None,
    started: float,
) -> float | None:
    """What is left of a slice of the chain budget, never below zero.

    Recomputed before **every** request, not once per model: a model gets one
    slice of the 240 s and its own retries and retry delays come out of that
    same slice. Without this, a first attempt that ran 239 s without tripping
    the ceiling would hand its retry a fresh 240 s, and two requests plus a
    blocked read would run past the 300 s job claim lease -- the exact failure
    the single-request ceiling was added to stop.
    """

    if deadline_seconds is None:
        return None
    return max(0.0, float(deadline_seconds) - (time.monotonic() - started))


def _request_mimo_v2(
    requester: Callable[..., Any] | None,
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    model_config: AiModelConfig,
    prompt: str,
    media_root: str | Path,
    context_text: str,
    deadline_seconds: float | None,
) -> Any:
    """One provider request, with the chain's remaining time as its ceiling.

    A caller-supplied ``requester`` keeps the signature it always had: the
    deadline is an implementation detail of the real HTTP call, and test stubs
    do not have a clock to honour.
    """

    if requester is not None:
        return requester(
            raw_message=raw_message,
            media_assets=media_assets,
            model_config=model_config,
            prompt=prompt,
            media_root=media_root,
            context_text=context_text,
            json_mode=True,
            disable_thinking=True,
        )
    return _call_mimo_direct_model(
        raw_message=raw_message,
        media_assets=media_assets,
        model_config=model_config,
        prompt=prompt,
        media_root=media_root,
        context_text=context_text,
        json_mode=True,
        disable_thinking=True,
        total_deadline_seconds=deadline_seconds,
    )


@dataclass(frozen=True, slots=True)
class MimoProviderAttemptTelemetry:
    """Best-effort audit metadata that never participates in recognition."""

    provider_request_made: bool = True
    provider_usage: Mapping[str, Any] | None = None
    request_component_bytes: Mapping[str, Any] | None = None
    #: Why this request failed, classified from the exception rather than its
    #: text (``mimo_provider_health``): the provider would not serve us, it
    #: rejected the request, or it answered with an invalid payload. ``None``
    #: on success and on failures that cannot be named. Alerting only -- it
    #: does not change what recognition decides.
    failure_class: str | None = None
    failure_kind: str | None = None
    http_status: int | None = None


class _MimoProviderPayload(dict[str, Any]):
    """Parsed provider payload retaining the raw HTTP response size."""

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        response_size_bytes: int,
        telemetry: MimoProviderAttemptTelemetry,
    ):
        super().__init__(payload)
        self.response_size_bytes = max(0, int(response_size_bytes))
        self.mimo_provider_attempt_telemetry = telemetry


def run_mimo_direct_experiment(
    session_factory: sessionmaker,
    *,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path = "config/ai_recognition.yaml",
    media_root: str | Path = "data/media",
    limit: int = 100,
    input_kind: Literal["all", "text", "image"] = "all",
    rerun: bool = False,
) -> ExperimentRunStats:
    config = ai_recognition_config or load_ai_recognition_config(ai_recognition_config_path)
    model_config = _find_mimo_model(config)
    if model_config is None or not model_config.provider.is_configured:
        raise RuntimeError("MiMo model is not configured in AI config.")
    composition = _build_mimo_experiment_prompt(session_factory, config)

    stats = ExperimentRunStats()
    with session_factory() as session:
        messages = _load_experiment_messages(
            session,
            limit=limit,
            input_kind=input_kind,
            experiment_name=MIMO_DIRECT_EXPERIMENT_NAME,
            rerun=rerun,
        )
        for raw_message in messages:
            stats = _replace(stats, considered=stats.considered + 1)
            media_assets = (
                session.query(MediaAsset)
                .filter(MediaAsset.raw_message_id == raw_message.id)
                .order_by(MediaAsset.id.asc())
                .all()
            )
            actual_input_kind = _resolve_input_kind(raw_message, media_assets, media_root=media_root)
            if actual_input_kind == "empty":
                stats = _replace(stats, skipped_no_input=stats.skipped_no_input + 1)
                continue
            error_message: str | None = None
            try:
                payload = _call_mimo_direct_model(
                    raw_message=raw_message,
                    media_assets=media_assets,
                    model_config=model_config,
                    prompt=composition.system_prompt,
                    media_root=media_root,
                )
                _upsert_experiment_result(
                    session,
                    raw_message=raw_message,
                    model_config=model_config,
                    input_kind=actual_input_kind,
                    payload=payload,
                    error_message=None,
                )
                stats = _replace(stats, succeeded=stats.succeeded + 1)
            except Exception as exc:
                error_message = str(exc)
                _upsert_experiment_result(
                    session,
                    raw_message=raw_message,
                    model_config=model_config,
                    input_kind=actual_input_kind,
                    payload={},
                    error_message=error_message,
                )
                stats = _replace(stats, failed=stats.failed + 1)
            session.commit()
            record_prompt_invocation(
                session_factory,
                PromptInvocationRecord(
                    feature="recognition_experiment",
                    correlation_key=f"experiment:{raw_message.id}:mimo_direct",
                    raw_message_id=raw_message.id,
                    chat_id=raw_message.chat_id,
                    model=model_config.model,
                    prompt_versions=composition.version_map,
                    status="failed" if error_message else "completed",
                    error_message=error_message,
                ),
            )
    return stats


def run_mimo_direct_for_message(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path = "config/ai_recognition.yaml",
    media_root: str | Path = "data/media",
) -> RecognitionExperiment | None:
    config = ai_recognition_config or load_ai_recognition_config(ai_recognition_config_path)
    model_config = _find_mimo_model(config)
    if model_config is None or not model_config.provider.is_configured:
        return None
    composition = _build_mimo_experiment_prompt(session_factory, config)

    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        if raw_message is None:
            raise LookupError("raw message not found")
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == raw_message.id)
            .order_by(MediaAsset.id.asc())
            .all()
        )
        input_kind = _resolve_input_kind(raw_message, media_assets, media_root=media_root)
        if input_kind == "empty":
            return None
        error_message: str | None = None
        try:
            payload = _call_mimo_direct_model(
                raw_message=raw_message,
                media_assets=media_assets,
                model_config=model_config,
                prompt=composition.system_prompt,
                media_root=media_root,
            )
            result = _upsert_experiment_result(
                session,
                raw_message=raw_message,
                model_config=model_config,
                input_kind=input_kind,
                payload=payload,
                error_message=None,
            )
        except Exception as exc:
            error_message = str(exc)
            result = _upsert_experiment_result(
                session,
                raw_message=raw_message,
                model_config=model_config,
                input_kind=input_kind,
                payload={},
                error_message=error_message,
            )
        session.commit()
        session.refresh(result)
        session.expunge(result)
        record_prompt_invocation(
            session_factory,
            PromptInvocationRecord(
                feature="recognition_experiment",
                correlation_key=f"experiment:{raw_message.id}:mimo_direct",
                raw_message_id=raw_message.id,
                chat_id=raw_message.chat_id,
                model=model_config.model,
                prompt_versions=composition.version_map,
                status="failed" if error_message else "completed",
                error_message=error_message,
            ),
        )
        return result


def infer_mimo_authoritative_v2(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path = "config/ai_recognition.yaml",
    media_root: str | Path = "data/media",
    context_text: str | None = None,
    requester: Callable[..., Any] | None = None,
    max_attempts: int = MIMO_AUTHORITATIVE_MAX_ATTEMPTS,
    retry_delay_seconds: float = MIMO_AUTHORITATIVE_RETRY_DELAY_SECONDS,
) -> MimoV2InferenceResult:
    """Call and audit one strict MiMo v2 analysis without execution writes.

    The stage's whole chain is walked: a model keeps its own retries, and only
    when it is finished does the next model start. The run records the model
    that answered (the chain head when none did); every attempt row records
    the model it actually called.
    """

    attempts = _validated_mimo_v2_max_attempts(max_attempts)
    retry_delay = _validated_mimo_v2_retry_delay(retry_delay_seconds)
    active_config = config or load_ai_recognition_config(
        ai_recognition_config_path
    )
    seed_default_prompt_registry(session_factory, active_config)
    chain = resolve_authoritative_chain(active_config)
    model_config = chain[0] if chain else None
    model = model_config.model if model_config is not None else "mimo-v2.5"

    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError("raw message not found")
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == int(raw_message_id))
            .order_by(MediaAsset.id.asc())
            .all()
        )
        input_kind = _resolve_input_kind(
            raw_message,
            media_assets,
            media_root=media_root,
        )
        input_fingerprint = build_message_input_fingerprint(
            raw_message,
            media_assets,
            media_root=media_root,
        )
        effective_context = (
            context_text
            if context_text is not None
            else _build_authoritative_context(session, raw_message)
        )
        chat_id = int(raw_message.chat_id)

    composition = compose_trading_prompt(
        session_factory,
        model_kind="mimo",
        context=effective_context,
        contract_version="v2",
    )
    analysis_input_fingerprint = _mimo_v2_analysis_input_fingerprint(
        message_input_fingerprint=input_fingerprint,
        context_text=composition.context,
    )
    run = start_mimo_run(
        session_factory,
        raw_message_id=int(raw_message_id),
        run_kind="v2_authoritative",
        contract_version=MIMO_V2_CONTRACT_VERSION,
        model=model,
        input_kind=input_kind,
        input_fingerprint=analysis_input_fingerprint,
        prompt_versions=composition.version_map,
    )

    if model_config is None or not model_config.provider.is_configured:
        return _complete_v2_failure(
            session_factory,
            raw_message_id=int(raw_message_id),
            chat_id=chat_id,
            run_id=run.id,
            input_kind=input_kind,
            model=model,
            prompt_versions=composition.version_map,
            error_code="provider_http_error",
            error_message="MiMo model is not configured",
        )
    if input_kind == "empty":
        return _complete_v2_failure(
            session_factory,
            raw_message_id=int(raw_message_id),
            chat_id=chat_id,
            run_id=run.id,
            input_kind=input_kind,
            model=model,
            prompt_versions=composition.version_map,
            error_code="contract_validation_failed",
            error_message="message has no readable text or image",
        )
    try:
        unreadable_images = [
            asset
            for asset in media_assets
            if _is_image_asset(asset)
            and _media_asset_to_data_url(asset, media_root=media_root) is None
        ]
    except (OSError, RuntimeError):
        unreadable_images = [
            asset for asset in media_assets if _is_image_asset(asset)
        ]
    if unreadable_images:
        # A failure before any request was sent. Another model cannot help it,
        # so the chain is never started (design §4).
        return _complete_v2_failure(
            session_factory,
            raw_message_id=int(raw_message_id),
            chat_id=chat_id,
            run_id=run.id,
            input_kind=input_kind,
            model=model,
            prompt_versions=composition.version_map,
            error_code="image_unavailable",
            error_message="image media is declared but unavailable or unreadable",
        )

    ordinals = _OrdinalCounter()
    last_failure = {
        "error_code": "provider_http_error",
        "error_message": "MiMo provider request failed",
    }

    def _input_changed_result(attempt_model: str) -> MimoV2InferenceResult:
        return _complete_v2_failure(
            session_factory,
            raw_message_id=int(raw_message_id),
            chat_id=chat_id,
            run_id=run.id,
            input_kind=input_kind,
            model=attempt_model,
            prompt_versions=composition.version_map,
            error_code="input_changed_during_analysis",
            error_message="message input changed during MiMo analysis",
        )

    def _input_is_current() -> bool:
        return _mimo_v2_input_is_current(
            session_factory,
            raw_message_id=int(raw_message_id),
            media_root=media_root,
            expected_fingerprint=analysis_input_fingerprint,
            expected_context=composition.context,
            rebuild_context=context_text is None,
        )

    def _attempt_model(
        candidate: AiModelConfig,
        *,
        deadline_seconds: float | None = None,
    ) -> Any:
        error_code = "provider_http_error"
        error_message = "MiMo provider request failed"
        request_made = True
        previous_ordinal: int | None = None
        model_started = time.monotonic()
        for model_attempt in range(1, attempts + 1):
            ordinal = ordinals.next()
            attempt_started_at = utc_now()
            started = time.perf_counter()
            response_payload: Any | None = None
            try:
                response_payload = _request_mimo_v2(
                    requester,
                    raw_message=raw_message,
                    media_assets=media_assets,
                    model_config=candidate,
                    prompt=composition.system_prompt,
                    media_root=media_root,
                    context_text=composition.context,
                    deadline_seconds=_remaining_deadline(
                        deadline_seconds, model_started
                    ),
                )
                payload = _coerce_mimo_v2_payload(response_payload)
                parsed = parse_mimo_v2_payload(payload)
                adapted = adapt_mimo_v2_to_current_payload(parsed)
            except (TimeoutError, httpx.TimeoutException) as exc:
                error_code = "provider_timeout"
                error_message = str(exc) or "MiMo provider timed out"
                request_made = _provider_attempt_telemetry(exc).provider_request_made
                attempt = _record_v2_attempt(
                    session_factory,
                    run_id=run.id,
                    ordinal=ordinal,
                    retry_of_ordinal=previous_ordinal,
                    model=candidate.model,
                    status="timeout",
                    error_code=error_code,
                    error_message=error_message,
                    started_at=attempt_started_at,
                    started_monotonic=started,
                    telemetry_source=exc,
                )
                error_message = attempt.error_message or error_message
                if not _input_is_current():
                    return _V2Terminal(_input_changed_result(candidate.model))
                previous_ordinal = ordinal
                if model_attempt < attempts:
                    _sleep_before_mimo_retry(retry_delay)
                    continue
                break
            except (MimoV2ContractError, MimoV2ExecutionAdapterError) as exc:
                error_code = "contract_validation_failed"
                error_message = str(exc) or "MiMo v2 contract validation failed"
                attempt = _record_v2_attempt(
                    session_factory,
                    run_id=run.id,
                    ordinal=ordinal,
                    retry_of_ordinal=previous_ordinal,
                    model=candidate.model,
                    status="contract_failure",
                    error_code=error_code,
                    error_message=error_message,
                    response_payload=response_payload,
                    started_at=attempt_started_at,
                    started_monotonic=started,
                    telemetry_source=response_payload,
                )
                error_message = attempt.error_message or error_message
                if not _input_is_current():
                    return _V2Terminal(_input_changed_result(candidate.model))
                # The same malformed response is deterministic; only transport
                # failures are retried so fallback can start without added delay.
                break
            except (_MimoV2InvalidJson, json.JSONDecodeError, ValueError) as exc:
                error_code = "invalid_json"
                error_message = str(exc) or "MiMo response is not valid JSON"
                invalid_response = (
                    exc.response_payload
                    if isinstance(exc, _MimoV2InvalidJson)
                    else response_payload
                )
                attempt = _record_v2_attempt(
                    session_factory,
                    run_id=run.id,
                    ordinal=ordinal,
                    retry_of_ordinal=previous_ordinal,
                    model=candidate.model,
                    status="invalid_json",
                    error_code=error_code,
                    error_message=error_message,
                    response_payload=invalid_response,
                    started_at=attempt_started_at,
                    started_monotonic=started,
                    telemetry_source=(
                        response_payload if response_payload is not None else exc
                    ),
                )
                error_message = attempt.error_message or error_message
                if not _input_is_current():
                    return _V2Terminal(_input_changed_result(candidate.model))
                # JSON shape errors are deterministic for this response and should
                # fail fast into the guarded fallback path.
                break
            except Exception as exc:
                error_code = "provider_http_error"
                error_message = str(exc) or "MiMo provider request failed"
                request_made = _provider_attempt_telemetry(exc).provider_request_made
                attempt = _record_v2_attempt(
                    session_factory,
                    run_id=run.id,
                    ordinal=ordinal,
                    retry_of_ordinal=previous_ordinal,
                    model=candidate.model,
                    status="http_error",
                    error_code=error_code,
                    error_message=error_message,
                    started_at=attempt_started_at,
                    started_monotonic=started,
                    telemetry_source=exc,
                )
                error_message = attempt.error_message or error_message
                if not _input_is_current():
                    return _V2Terminal(_input_changed_result(candidate.model))
                previous_ordinal = ordinal
                if model_attempt < attempts:
                    _sleep_before_mimo_retry(retry_delay)
                    continue
                break
            else:
                attempt = _record_v2_attempt(
                    session_factory,
                    run_id=run.id,
                    ordinal=ordinal,
                    retry_of_ordinal=previous_ordinal,
                    model=candidate.model,
                    status="completed",
                    response_payload=payload,
                    started_at=attempt_started_at,
                    started_monotonic=started,
                    telemetry_source=response_payload,
                )
                if not _input_is_current():
                    return _V2Terminal(_input_changed_result(candidate.model))
                return _V2Success(
                    attempt_ordinal=attempt.ordinal,
                    payload=payload,
                    parsed=parsed,
                    adapted=adapted,
                    response_payload=response_payload,
                )
        last_failure["error_code"] = error_code
        last_failure["error_message"] = error_message
        raise _MimoModelFailed(error_message, request_made=request_made)

    routed = run_with_fallback(
        chain,
        _attempt_model,
        budget_seconds=MIMO_REQUEST_TOTAL_DEADLINE_SECONDS,
        min_remaining_seconds=MIN_REMAINING_SECONDS,
        classify=_should_try_next_model,
    )
    if routed.succeeded:
        outcome = routed.value
        if isinstance(outcome, _V2Terminal):
            return outcome.result
        answered_model = routed.model.model if routed.model is not None else model
        if routed.used_fallback:
            logger.warning(
                "mimo authoritative fell back to another model raw_message_id=%s "
                "run_id=%s contract=v2 from=%s to=%s",
                raw_message_id,
                run.id,
                ",".join(routed.fallback_from),
                answered_model,
            )
        canonical_payload = json.loads(outcome.adapted.canonical_v2_json)
        completed = complete_mimo_run(
            session_factory,
            run_id=run.id,
            status="completed",
            selected_ordinal=outcome.attempt_ordinal,
            canonical_payload=canonical_payload,
            projection_payload=_execution_projection(outcome.adapted.payload),
            became_authoritative=True,
            model=answered_model,
        )
        _record_mimo_v2_prompt_invocation(
            session_factory,
            raw_message_id=int(raw_message_id),
            chat_id=chat_id,
            run_id=run.id,
            model=answered_model,
            prompt_versions=composition.version_map,
            status="completed",
            error_message=None,
        )
        return MimoV2InferenceResult(
            raw_message_id=int(raw_message_id),
            run_id=completed.id,
            parsed_result=outcome.parsed,
            adapted_result=outcome.adapted,
            input_kind=input_kind,
            model=answered_model,
            prompt_versions=dict(composition.version_map),
            response_size_bytes=_provider_response_size(outcome.response_payload),
        )

    # Every model failed. The run keeps the chain head as its model, and the
    # error names each model that was tried.
    return _complete_v2_failure(
        session_factory,
        raw_message_id=int(raw_message_id),
        chat_id=chat_id,
        run_id=run.id,
        input_kind=input_kind,
        model=model,
        prompt_versions=composition.version_map,
        error_code=str(last_failure["error_code"]),
        error_message=(
            routed.error_message or str(last_failure["error_message"])
        ),
    )


def _coerce_mimo_v2_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        return dict(response)
    if isinstance(response, str):
        try:
            return _parse_json_object(response)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise _MimoV2InvalidJson(
                str(exc) or "MiMo response is not valid JSON",
                response_payload=response,
            ) from exc
    raise _MimoV2InvalidJson(
        "MiMo response JSON is not an object",
        response_payload=response,
    )


def _provider_response_size(response: Any) -> int:
    explicit = getattr(response, "response_size_bytes", None)
    if (
        isinstance(explicit, int)
        and not isinstance(explicit, bool)
        and explicit >= 0
    ):
        return explicit
    if isinstance(response, str):
        return len(response.encode("utf-8"))
    if isinstance(response, Mapping):
        return len(
            json.dumps(
                response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return 0


def _record_v2_attempt(
    session_factory: sessionmaker,
    *,
    run_id: int,
    ordinal: int,
    status: str,
    started_at,
    started_monotonic: float,
    retry_of_ordinal: int | None = None,
    model: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    response_payload: Any | None = None,
    telemetry_source: Any | None = None,
):
    completed_at = utc_now()
    telemetry = _provider_attempt_telemetry(telemetry_source)
    return record_mimo_attempt(
        session_factory,
        run_id=run_id,
        ordinal=ordinal,
        # Only a retry of the *same* model is a retry. The first request of a
        # fallback model is a new attempt, not a repeat of the one before it.
        retry_of_ordinal=retry_of_ordinal,
        model=model,
        status=status,
        error_code=error_code,
        error_message=error_message,
        response_payload=response_payload,
        duration_ms=max(0, round((time.perf_counter() - started_monotonic) * 1000)),
        started_at=started_at,
        completed_at=completed_at,
        attempt_phase="v2_authoritative",
        provider_request_count=int(telemetry.provider_request_made),
        provider_usage=_provider_usage_audit((telemetry,)),
        request_component_bytes=_request_component_bytes_audit(telemetry),
    )


def _validated_mimo_v2_max_attempts(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MIMO_V2_MAX_ATTEMPTS
    ):
        raise ValueError(
            f"max_attempts must be between 1 and {MIMO_V2_MAX_ATTEMPTS}"
        )
    return value


def _validated_mimo_v2_retry_delay(value: float) -> float:
    if isinstance(value, bool):
        raise ValueError("retry_delay_seconds must be nonnegative")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("retry_delay_seconds must be nonnegative") from exc
    if (
        not math.isfinite(normalized)
        or normalized < 0
        or normalized > MIMO_V2_MAX_RETRY_DELAY_SECONDS
    ):
        raise ValueError(
            "retry_delay_seconds must be finite and between 0 and "
            f"{MIMO_V2_MAX_RETRY_DELAY_SECONDS:g}"
        )
    return normalized


def _mimo_v2_analysis_input_fingerprint(
    *,
    message_input_fingerprint: str,
    context_text: str,
) -> str:
    canonical = json.dumps(
        {
            "message_input_fingerprint": message_input_fingerprint,
            "context_text": context_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _mimo_v2_input_is_current(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    media_root: str | Path,
    expected_fingerprint: str,
    expected_context: str,
    rebuild_context: bool,
) -> bool:
    try:
        current_message_fingerprint = build_current_message_input_fingerprint(
            session_factory,
            raw_message_id,
            media_root=media_root,
        )
        current_context = (
            str(
                build_authoritative_context_for_message(
                    session_factory,
                    raw_message_id,
                )
            )
            if rebuild_context
            else expected_context
        )
    except (LookupError, OSError):
        return False
    current = _mimo_v2_analysis_input_fingerprint(
        message_input_fingerprint=current_message_fingerprint,
        context_text=current_context,
    )
    return current == expected_fingerprint


def _sleep_before_mimo_retry(delay_seconds: float) -> None:
    if float(delay_seconds) > 0:
        time.sleep(float(delay_seconds))


def _complete_v2_failure(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    chat_id: int,
    run_id: int,
    input_kind: str,
    model: str,
    prompt_versions: Mapping[str, int],
    error_code: str,
    error_message: str,
) -> MimoV2InferenceResult:
    failed = complete_mimo_run(
        session_factory,
        run_id=run_id,
        status="failed",
        selected_ordinal=None,
        final_error_code=error_code,
        final_error_message=error_message,
    )
    _record_mimo_v2_prompt_invocation(
        session_factory,
        raw_message_id=raw_message_id,
        chat_id=chat_id,
        run_id=run_id,
        model=model,
        prompt_versions=prompt_versions,
        status="failed",
        error_message=failed.final_error_message,
    )
    return MimoV2InferenceResult(
        raw_message_id=raw_message_id,
        run_id=failed.id,
        parsed_result=None,
        adapted_result=None,
        input_kind=input_kind,
        model=model,
        prompt_versions=dict(prompt_versions),
        error_code=failed.final_error_code,
        error_message=failed.final_error_message,
    )


def _record_mimo_v2_prompt_invocation(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    chat_id: int,
    run_id: int,
    model: str,
    prompt_versions: Mapping[str, int],
    status: str,
    error_message: str | None,
) -> None:
    try:
        record_prompt_invocation(
            session_factory,
            PromptInvocationRecord(
                feature="message_recognition",
                correlation_key=f"recognition:{raw_message_id}:mimo:v2:{run_id}",
                raw_message_id=raw_message_id,
                chat_id=chat_id,
                model=model,
                prompt_versions=dict(prompt_versions),
                status=status,
                error_message=error_message,
            ),
        )
    except Exception as exc:
        logger.warning(
            "MiMo v2 prompt invocation audit failed: "
            "raw_message_id=%s run_id=%s error=%s",
            raw_message_id,
            run_id,
            type(exc).__name__,
        )


def run_mimo_authoritative_for_message(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path = "config/ai_recognition.yaml",
    media_root: str | Path = "data/media",
    context_text: str | None = None,
) -> MimoAuthoritativeResult:
    config = ai_recognition_config or load_ai_recognition_config(ai_recognition_config_path)
    seed_default_prompt_registry(session_factory, config)
    chain = resolve_authoritative_chain(config)
    model_config = chain[0] if chain else None
    if model_config is None or not model_config.provider.is_configured:
        return MimoAuthoritativeResult(
            raw_message_id=raw_message_id,
            payload={},
            input_kind="unknown",
            model=(model_config.model if model_config is not None else "mimo-v2.5"),
            status="识别失败",
            error_message="MiMo model is not configured",
        )

    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        if raw_message is None:
            raise LookupError("raw message not found")
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == raw_message.id)
            .order_by(MediaAsset.id.asc())
            .all()
        )
        input_kind = _resolve_input_kind(raw_message, media_assets, media_root=media_root)
        if input_kind == "empty":
            return MimoAuthoritativeResult(
                raw_message_id=raw_message_id,
                payload={},
                input_kind=input_kind,
                model=model_config.model,
                status="识别失败",
                error_message="message has no readable text or image",
            )
        unreadable_images = [
            asset
            for asset in media_assets
            if _is_image_asset(asset)
            and _media_asset_to_data_url(asset, media_root=media_root) is None
        ]
        if unreadable_images:
            error_message = "image media is declared but unavailable or unreadable"
            experiment = _upsert_experiment_result(
                session,
                raw_message=raw_message,
                model_config=model_config,
                input_kind=input_kind,
                payload={},
                error_message=error_message,
                prompt_version=MIMO_AUTHORITATIVE_PROMPT_VERSION,
            )
            session.commit()
            return MimoAuthoritativeResult(
                raw_message_id=raw_message_id,
                payload={},
                input_kind=input_kind,
                model=model_config.model,
                status=experiment.status,
                error_message=error_message,
            )
        payload: dict[str, Any] = {}
        error_message: str | None = None
        effective_context = context_text or _build_authoritative_context(session, raw_message)
        composition = compose_trading_prompt(
            session_factory,
            model_kind="mimo",
            context=effective_context,
        )
        (
            payload,
            error_message,
            provider_attempt_telemetry,
            answered_model,
            model_attempts,
        ) = _call_mimo_authoritative_over_chain(
            chain,
            raw_message=raw_message,
            media_assets=media_assets,
            prompt=composition.system_prompt,
            media_root=media_root,
            context_text=composition.context,
        )
        if len(model_attempts) > 1:
            logger.warning(
                "mimo authoritative fell back to another model raw_message_id=%s "
                "contract=v1 tried=%s answered=%s",
                raw_message_id,
                ",".join(item.model_id for item in model_attempts),
                answered_model.model if answered_model is not None else "none",
            )
        used_model = answered_model or model_config
        experiment = _upsert_experiment_result(
            session,
            raw_message=raw_message,
            model_config=used_model,
            input_kind=input_kind,
            payload=payload,
            error_message=error_message,
            prompt_version=MIMO_AUTHORITATIVE_PROMPT_VERSION,
        )
        session.commit()
        record_prompt_invocation(
            session_factory,
            PromptInvocationRecord(
                feature="message_recognition",
                correlation_key=f"recognition:{raw_message_id}:mimo",
                raw_message_id=raw_message_id,
                chat_id=raw_message.chat_id,
                model=used_model.model,
                prompt_versions=composition.version_map,
                status="failed" if error_message else "completed",
                error_message=error_message,
            ),
        )
        return MimoAuthoritativeResult(
            raw_message_id=raw_message_id,
            payload=payload,
            input_kind=input_kind,
            model=used_model.model,
            status=experiment.status,
            error_message=error_message,
            prompt_versions=composition.version_map,
            provider_attempt_telemetry=provider_attempt_telemetry,
            model_attempts=model_attempts,
        )


def _call_mimo_authoritative_over_chain(
    chain: list[AiModelConfig],
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    prompt: str,
    media_root: str | Path,
    context_text: str,
    max_attempts: int = MIMO_AUTHORITATIVE_MAX_ATTEMPTS,
    retry_delay_seconds: float = MIMO_AUTHORITATIVE_RETRY_DELAY_SECONDS,
) -> tuple[
    dict[str, Any],
    str | None,
    tuple[MimoProviderAttemptTelemetry, ...],
    AiModelConfig | None,
    tuple[MimoModelAttempt, ...],
]:
    """Walk the stage chain; each model finishes its own retries first.

    The whole chain shares ``MIMO_REQUEST_TOTAL_DEADLINE_SECONDS``, because
    that ceiling plus one blocked read is what has to fit inside the 300 s job
    claim lease -- giving each model its own 240 s would put a second full
    deadline inside the same lease, which is exactly what the single-model
    retry rule already refuses to do.
    """

    records: list[MimoModelAttempt] = []

    def _attempt(
        candidate: AiModelConfig,
        *,
        deadline_seconds: float | None = None,
    ) -> tuple[dict[str, Any], tuple[MimoProviderAttemptTelemetry, ...]]:
        started_at = utc_now()
        started = time.perf_counter()
        payload, error_message, telemetry = _call_mimo_authoritative_with_retry(
            raw_message=raw_message,
            media_assets=media_assets,
            model_config=candidate,
            prompt=prompt,
            media_root=media_root,
            context_text=context_text,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            total_deadline_seconds=deadline_seconds,
        )
        records.append(
            MimoModelAttempt(
                model_id=candidate.id,
                model=candidate.model,
                succeeded=error_message is None,
                error_message=error_message,
                telemetry=telemetry,
                started_at=started_at,
                completed_at=utc_now(),
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )
        )
        if error_message is not None:
            raise _MimoModelFailed(
                error_message,
                request_made=any(
                    item.provider_request_made for item in telemetry
                ),
            )
        return payload, telemetry

    routed = run_with_fallback(
        chain,
        _attempt,
        budget_seconds=MIMO_REQUEST_TOTAL_DEADLINE_SECONDS,
        min_remaining_seconds=MIN_REMAINING_SECONDS,
        classify=_should_try_next_model,
    )
    if routed.succeeded:
        payload, telemetry = routed.value
        return payload, None, telemetry, routed.model, tuple(records)
    return (
        {},
        routed.error_message or "MiMo provider request failed",
        tuple(item for record in records for item in record.telemetry),
        None,
        tuple(records),
    )


def _call_mimo_authoritative_with_retry(
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    model_config: AiModelConfig,
    prompt: str,
    media_root: str | Path,
    context_text: str,
    max_attempts: int = MIMO_AUTHORITATIVE_MAX_ATTEMPTS,
    retry_delay_seconds: float = MIMO_AUTHORITATIVE_RETRY_DELAY_SECONDS,
    total_deadline_seconds: float | None = None,
) -> tuple[
    dict[str, Any],
    str | None,
    tuple[MimoProviderAttemptTelemetry, ...],
]:
    errors: list[str] = []
    provider_attempts: list[MimoProviderAttemptTelemetry] = []
    attempts = max(1, max_attempts)
    model_started = time.monotonic()
    for attempt in range(1, attempts + 1):
        telemetry_recorded = False
        try:
            payload = _call_mimo_direct_model(
                raw_message=raw_message,
                media_assets=media_assets,
                model_config=model_config,
                prompt=prompt,
                media_root=media_root,
                context_text=context_text,
                total_deadline_seconds=_remaining_deadline(
                    total_deadline_seconds, model_started
                ),
            )
            provider_attempts.append(_provider_attempt_telemetry(payload))
            telemetry_recorded = True
            _validate_authoritative_payload(payload)
            return payload, None, tuple(provider_attempts)
        except Exception as exc:
            if not telemetry_recorded:
                provider_attempts.append(
                    _classified_attempt_telemetry(
                        _provider_attempt_telemetry(exc),
                        exc,
                    )
                )
            else:
                # The provider answered 2xx and the payload failed validation:
                # the provider is up, the answer is what is wrong.
                from dataclasses import replace as _replace

                from telegram_kol_research.mimo_provider_health import (
                    RESPONSE_INVALID,
                )

                provider_attempts[-1] = _replace(
                    provider_attempts[-1],
                    failure_class=RESPONSE_INVALID,
                )
            errors.append(str(exc))
            if isinstance(exc, MimoRequestDeadlineExceeded):
                # A second full deadline would not fit inside the job claim
                # lease; the queue's own retry is the next attempt.
                break
            if attempt >= attempts:
                break
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)
    if len(errors) == 1:
        return {}, errors[0], tuple(provider_attempts)
    return (
        {},
        f"MiMo failed after {len(errors)} attempts: "
        + " | ".join(f"attempt {idx + 1}: {error}" for idx, error in enumerate(errors)),
        tuple(provider_attempts),
    )


def _validate_authoritative_payload(payload: dict[str, Any]) -> None:
    if str(payload.get("recognition_result") or "") not in {"是策略", "非策略", "识别失败"}:
        raise ValueError("MiMo response has invalid recognition_result")
    for field in ("strategy", "lifecycle_event", "input_reading"):
        if not isinstance(payload.get(field), dict):
            raise ValueError(f"MiMo response missing {field}")


def _load_experiment_messages(
    session,
    *,
    limit: int,
    input_kind: str,
    experiment_name: str,
    rerun: bool,
) -> list[RawMessage]:
    query = session.query(RawMessage).order_by(RawMessage.posted_at.desc(), RawMessage.id.desc())
    if input_kind == "text":
        query = query.filter(RawMessage.text.isnot(None), RawMessage.text != "")
    elif input_kind == "image":
        query = query.join(MediaAsset, MediaAsset.raw_message_id == RawMessage.id)
    if not rerun:
        completed_ids = (
            select(RecognitionExperiment.raw_message_id)
            .select_from(RecognitionExperiment)
            .filter(RecognitionExperiment.experiment_name == experiment_name)
        )
        query = query.filter(RawMessage.id.not_in(completed_ids))
    if input_kind == "image":
        query = query.distinct()
    return query.limit(max(limit, 1)).all()


#: Wall-clock ceiling for one MiMo request, from sending it to its last byte.
#:
#: ``timeout_seconds`` is handed to httpx, whose timeouts are per operation:
#: a response that trickles a byte every few seconds never trips a read
#: timeout. On 2026-09-12 run 7253 took 606 s that way; the queue reclaimed its
#: job after 5 minutes, a second run succeeded, and the first thread wrote its
#: failure ten minutes later -- which paged a person about an outage that was
#: not happening. Measured against production (30 days, 6159 successful
#: calls): 11 took over 240 s end to end, and only 1 of those was a single
#: request, so 240 s costs about one legitimate success a month. A request
#: that hits the ceiling is not retried (``_call_mimo_authoritative_with_retry``)
#: because the last blocked read can still add one per-read timeout, and
#: 240 s + 60 s must stay within the 300 s job claim lease; a test pins that.
MIMO_REQUEST_TOTAL_DEADLINE_SECONDS = 240.0


class MimoRequestDeadlineExceeded(TimeoutError):
    """One request ran past ``MIMO_REQUEST_TOTAL_DEADLINE_SECONDS``.

    A ``TimeoutError``, so provider classification names it a timeout.
    """


def _read_response_within_deadline(
    response: Any,
    *,
    started: float,
    total_deadline_seconds: float | None = None,
) -> bytes:
    """Read the streamed body, checking the wall clock after every chunk.

    Verified against a local trickle server before use: a client closed from
    another thread does not interrupt a blocked read (the request ran on to
    the 60 s per-read timeout), while a check between chunks aborts at the
    ceiling.
    """

    ceiling = (
        float(total_deadline_seconds)
        if total_deadline_seconds is not None
        else MIMO_REQUEST_TOTAL_DEADLINE_SECONDS
    )
    chunks: list[bytes] = []
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        elapsed = time.monotonic() - started
        if elapsed > ceiling:
            raise MimoRequestDeadlineExceeded(
                f"MiMo request exceeded its {ceiling:.0f}s "
                f"total deadline after {elapsed:.0f}s"
            )
    return b"".join(chunks)


def _call_mimo_direct_model(
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    model_config: AiModelConfig,
    prompt: str = "",
    media_root: str | Path,
    context_text: str = "",
    json_mode: bool = False,
    disable_thinking: bool = False,
    total_deadline_seconds: float | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if model_config.api_key:
        headers["Authorization"] = f"Bearer {model_config.api_key}"
    try:
        payload = _build_mimo_payload(
            raw_message=raw_message,
            media_assets=media_assets,
            prompt=prompt,
            model=model_config.model,
            media_root=media_root,
            context_text=context_text,
            json_mode=json_mode,
            disable_thinking=disable_thinking,
        )
    except Exception as exc:
        _attach_provider_attempt_telemetry(
            exc,
            MimoProviderAttemptTelemetry(provider_request_made=False),
        )
        raise
    try:
        request_component_bytes = _measure_mimo_request_component_bytes(
            payload=payload,
            raw_message=raw_message,
            context_text=context_text,
        )
    except Exception:
        request_component_bytes = {
            "available": False,
            "reason": "request_component_measurement_failed",
        }
    telemetry = MimoProviderAttemptTelemetry(
        provider_request_made=False,
        provider_usage=None,
        request_component_bytes=request_component_bytes,
    )
    try:
        with httpx.Client(timeout=model_config.timeout_seconds) as client:
            telemetry = MimoProviderAttemptTelemetry(
                provider_request_made=True,
                provider_usage=None,
                request_component_bytes=request_component_bytes,
            )
            request_started = time.monotonic()
            with client.stream(
                "POST",
                chat_completions_url(
                    model_config.base_url, provider_append_v1(model_config)
                ),
                json=payload,
                headers=headers,
            ) as response:
                body = _read_response_within_deadline(
                    response,
                    started=request_started,
                    total_deadline_seconds=total_deadline_seconds,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    # A streamed response has no ``.text`` once read; the body
                    # we already hold is the same bytes.
                    response_body = body.decode("utf-8", errors="replace")[:1200]
                    raise RuntimeError(
                        f"{exc}; response_body={response_body}"
                    ) from exc
            data = json.loads(body)
        usage = data.get("usage") if isinstance(data, Mapping) else None
        telemetry = MimoProviderAttemptTelemetry(
            provider_request_made=True,
            provider_usage=dict(usage) if isinstance(usage, Mapping) else None,
            request_component_bytes=request_component_bytes,
        )
    except Exception as exc:
        _attach_provider_attempt_telemetry(exc, telemetry)
        raise
    try:
        content = _extract_chat_content(data)
        response_size_bytes = len(body)
        return _MimoProviderPayload(
            _parse_json_object(content),
            response_size_bytes=response_size_bytes,
            telemetry=telemetry,
        )
    except Exception as exc:
        _attach_provider_attempt_telemetry(exc, telemetry)
        raise


def _attach_provider_attempt_telemetry(
    error: Exception,
    telemetry: MimoProviderAttemptTelemetry,
) -> None:
    try:
        setattr(error, "mimo_provider_attempt_telemetry", telemetry)
    except Exception:
        pass


def _provider_attempt_telemetry(value: Any) -> MimoProviderAttemptTelemetry:
    telemetry = getattr(value, "mimo_provider_attempt_telemetry", None)
    if isinstance(telemetry, MimoProviderAttemptTelemetry):
        return telemetry
    return MimoProviderAttemptTelemetry(provider_request_made=True)


def _classified_attempt_telemetry(
    telemetry: MimoProviderAttemptTelemetry,
    error: BaseException,
) -> MimoProviderAttemptTelemetry:
    """Name why a request that reached the provider failed (step-18).

    A failure before any request was sent (payload assembly, unreadable
    media) says nothing about the provider and stays unclassified.
    """

    if not telemetry.provider_request_made:
        return telemetry
    from dataclasses import replace as _replace

    from telegram_kol_research.mimo_provider_health import (
        classify_provider_failure,
    )

    failure = classify_provider_failure(error)
    if failure is None:
        return telemetry
    return _replace(
        telemetry,
        failure_class=failure.failure_class,
        failure_kind=failure.kind,
        http_status=failure.http_status,
    )


def _provider_usage_audit(
    attempts: tuple[MimoProviderAttemptTelemetry, ...],
) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    request_number = 0
    for telemetry in attempts:
        if not telemetry.provider_request_made:
            continue
        request_number += 1
        usage = telemetry.provider_usage
        if isinstance(usage, Mapping):
            try:
                raw_usage = json.loads(
                    json.dumps(
                        usage,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            except (TypeError, ValueError):
                raw_usage = None
            if isinstance(raw_usage, dict):
                requests.append(
                    {
                        "available": True,
                        "request_number": request_number,
                        "usage": raw_usage,
                    }
                )
                continue
            reason = "provider_usage_serialization_failed"
        else:
            reason = "provider_usage_not_returned"
        requests.append(
            {
                "available": False,
                "reason": reason,
                "request_number": request_number,
            }
        )
    return {"requests": requests}


def _latest_provider_request_telemetry(
    attempts: tuple[MimoProviderAttemptTelemetry, ...],
) -> MimoProviderAttemptTelemetry:
    for telemetry in reversed(attempts):
        if telemetry.provider_request_made:
            return telemetry
    return MimoProviderAttemptTelemetry(provider_request_made=False)


def _request_component_bytes_audit(
    telemetry: MimoProviderAttemptTelemetry,
) -> dict[str, Any]:
    value = telemetry.request_component_bytes
    if isinstance(value, Mapping):
        return dict(value)
    return {
        "available": False,
        "reason": "request_payload_not_observed",
    }


def _canonical_json_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _measure_mimo_request_component_bytes(
    *,
    payload: Mapping[str, Any],
    raw_message: RawMessage,
    context_text: str,
) -> dict[str, Any]:
    system_prompt = ""
    image_urls: list[str] = []
    messages = payload.get("messages")
    if isinstance(messages, list) and len(messages) >= 2:
        system = messages[0]
        if isinstance(system, Mapping):
            system_prompt = str(system.get("content") or "")
        user = messages[1]
        if isinstance(user, Mapping) and isinstance(user.get("content"), list):
            for part in user["content"]:
                if not isinstance(part, Mapping) or part.get("type") != "image_url":
                    continue
                image_url = part.get("image_url")
                if isinstance(image_url, Mapping) and isinstance(image_url.get("url"), str):
                    image_urls.append(image_url["url"])
    normalized_context = context_text.strip()
    current_message_text = (raw_message.text or "").strip() or "(empty)"
    direct_reply = _extract_rendered_reply_context(normalized_context)
    total = _canonical_json_bytes(payload)
    system_bytes = _canonical_json_bytes(system_prompt)
    current_bytes = _canonical_json_bytes(current_message_text)
    image_bytes = _canonical_json_bytes(image_urls) if image_urls else 0
    context_bytes = _canonical_json_bytes(normalized_context) if normalized_context else 0
    component_total = system_bytes + current_bytes + image_bytes + context_bytes
    structural_overhead_bytes = total - component_total
    if structural_overhead_bytes < 0:
        raise ValueError("request component partition exceeds total bytes")
    return {
        "available": True,
        "encoding": "utf-8-canonical-json-v1",
        "request_total_bytes": total,
        "system_prompt_bytes": system_bytes,
        "current_message_text_bytes": current_bytes,
        "image_evidence_bytes": image_bytes,
        "authoritative_context_bytes": context_bytes,
        "direct_reply_bytes": (
            _canonical_json_bytes(direct_reply) if direct_reply is not None else 0
        ),
        "direct_reply_included_in_authoritative_context": True,
        "structural_overhead_bytes": structural_overhead_bytes,
    }


def _extract_rendered_reply_context(context_text: str) -> str | None:
    marker = "Reply context:\n"
    start = context_text.find(marker)
    if start < 0:
        return None
    value_start = start + len(marker)
    value_end = context_text.find("\n\n", value_start)
    if value_end < 0:
        value_end = len(context_text)
    return context_text[value_start:value_end]


def _build_mimo_payload(
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    model: str,
    prompt: str = "",
    media_root: str | Path = "data/media",
    context_text: str = "",
    json_mode: bool = False,
    disable_thinking: bool = False,
) -> dict[str, Any]:
    image_parts: list[tuple[int, int | None, str]] = []
    for image_index, media_asset in enumerate(media_assets, start=1):
        data_url = _media_asset_to_data_url(media_asset, media_root=media_root)
        if data_url:
            image_parts.append((image_index, media_asset.id, data_url))
    user_text = (
        f"Message metadata:\n"
        f"chat_id={raw_message.chat_id}\n"
        f"message_id={raw_message.message_id}\n"
        f"sender={raw_message.sender_name or 'Unknown'}\n\n"
        f"Text/caption:\n{(raw_message.text or '').strip() or '(empty)'}"
    )
    if image_parts:
        image_map = [
            {"image_index": index, "asset_id": asset_id}
            for index, asset_id, _ in image_parts
        ]
        user_text = (
            f"{user_text}\n\nAttached image sequence:\n"
            f"{json.dumps(image_map, ensure_ascii=False, sort_keys=True)}"
        )
    if context_text.strip():
        user_text = f"{user_text}\n\n{context_text.strip()}"
    user_parts: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for _, _, data_url in image_parts:
        user_parts.append({"type": "image_url", "image_url": {"url": data_url}})
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_parts if len(user_parts) > 1 else user_text},
        ],
        "temperature": 0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    return payload


def _build_authoritative_context(session, raw_message: RawMessage) -> str:
    return render_authoritative_context(
        build_contextual_message_window(
            session,
            raw_message_id=int(raw_message.id),
        )
    )


def build_authoritative_context_for_message(
    session_factory: sessionmaker,
    raw_message_id: int,
) -> Any:
    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        if raw_message is None:
            raise LookupError("raw message not found")
        return _build_authoritative_context(session, raw_message)


def _upsert_experiment_result(
    session,
    *,
    raw_message: RawMessage,
    model_config: AiModelConfig,
    input_kind: str,
    payload: dict[str, Any],
    error_message: str | None,
    prompt_version: str = MIMO_DIRECT_PROMPT_VERSION,
) -> RecognitionExperiment:
    existing = (
        session.query(RecognitionExperiment)
        .filter(
            RecognitionExperiment.raw_message_id == raw_message.id,
            RecognitionExperiment.experiment_name == MIMO_DIRECT_EXPERIMENT_NAME,
        )
        .one_or_none()
    )
    now = utc_now()
    if existing is None:
        existing = RecognitionExperiment(
            raw_message_id=raw_message.id,
            experiment_name=MIMO_DIRECT_EXPERIMENT_NAME,
            model=model_config.model,
            prompt_version=prompt_version,
            input_kind=input_kind,
            status="识别失败",
            created_at=now,
        )
        session.add(existing)
    input_reading = payload.get("input_reading") if isinstance(payload.get("input_reading"), dict) else {}
    strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
    status = str(payload.get("recognition_result") or ("识别失败" if error_message else "识别失败")).strip()
    if status not in MIMO_EXPERIMENT_STATUSES:
        status = "识别失败"
    existing.model = model_config.model
    existing.prompt_version = prompt_version
    existing.input_kind = input_kind
    existing.status = status
    existing.reason = str(payload.get("reason") or "").strip() or None
    existing.observed_text = str(input_reading.get("observed_text") or "").strip() or None
    existing.strategy_json = (
        json.dumps(strategy, ensure_ascii=False, sort_keys=True)
        if _has_meaningful_strategy_fields(strategy)
        else None
    )
    existing.confidence = float(payload.get("confidence") or 0.0)
    existing.raw_response_json = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else None
    existing.error_message = error_message
    existing.updated_at = now
    return existing


def _media_asset_to_data_url(media_asset: MediaAsset, *, media_root: str | Path = "data/media") -> str | None:
    if not media_asset.local_path:
        return None
    path = resolve_media_path(media_asset.local_path, media_root=media_root)
    if path is None or not path.exists():
        return None
    if path.stat().st_size <= 0:
        return None
    mime_type = media_asset.mime_type or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _resolve_input_kind(
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    *,
    media_root: str | Path = "data/media",
) -> str:
    has_text = bool((raw_message.text or "").strip())
    has_image = any(_is_image_asset(asset) for asset in media_assets)
    if has_text and has_image:
        return "text+image"
    if has_image:
        return "image"
    if has_text:
        return "text"
    return "empty"


def _is_image_asset(media_asset: MediaAsset) -> bool:
    kind = str(media_asset.kind or "").strip().lower()
    mime_type = str(media_asset.mime_type or "").strip().lower()
    return "photo" in kind or "image" in kind or mime_type.startswith("image/")


def _build_mimo_experiment_prompt(
    session_factory: sessionmaker,
    config: AiRecognitionConfig,
) -> str:
    seed_default_prompt_registry(session_factory, config)
    return compose_trading_prompt(
        session_factory,
        model_kind="mimo",
        context="",
    )


def _has_meaningful_strategy_fields(strategy: dict[str, Any]) -> bool:
    return any(value not in (None, "", [], {}) for value in strategy.values())


def _find_mimo_model(config: AiRecognitionConfig) -> AiModelConfig | None:
    """The model the authoritative stage would call first.

    The name is kept because the daily probe, the prompt centre's MiMo test
    and the side-channel experiments all ask this same question, and because
    it is what those call sites already import. What changed is the answer:
    the head of the ``authoritative_recognition`` chain rather than a
    hard-coded search for ``mimo-v2.5``.
    """

    chain = resolve_authoritative_chain(config)
    return chain[0] if chain else _legacy_mimo_model(config)


def _extract_chat_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON is not an object")
    return parsed


def _replace(stats: ExperimentRunStats, **changes: int) -> ExperimentRunStats:
    values = {
        "considered": stats.considered,
        "skipped_existing": stats.skipped_existing,
        "skipped_no_input": stats.skipped_no_input,
        "succeeded": stats.succeeded,
        "failed": stats.failed,
    }
    values.update(changes)
    return ExperimentRunStats(**values)

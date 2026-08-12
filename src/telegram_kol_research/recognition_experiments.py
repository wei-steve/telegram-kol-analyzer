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

from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiRecognitionConfig,
    load_ai_recognition_config,
)
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


class _MimoProviderPayload(dict[str, Any]):
    """Parsed provider payload retaining the raw HTTP response size."""

    def __init__(self, payload: Mapping[str, Any], *, response_size_bytes: int):
        super().__init__(payload)
        self.response_size_bytes = max(0, int(response_size_bytes))


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
    """Call and audit one strict MiMo v2 analysis without execution writes."""

    attempts = _validated_mimo_v2_max_attempts(max_attempts)
    retry_delay = _validated_mimo_v2_retry_delay(retry_delay_seconds)
    active_config = config or load_ai_recognition_config(
        ai_recognition_config_path
    )
    seed_default_prompt_registry(session_factory, active_config)
    model_config = _find_mimo_model(active_config)
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

    request = requester or _call_mimo_direct_model
    last_error_code = "provider_http_error"
    last_error_message = "MiMo provider request failed"
    for ordinal in range(1, attempts + 1):
        attempt_started_at = utc_now()
        started = time.perf_counter()
        response_payload: Any | None = None
        try:
            response_payload = request(
                raw_message=raw_message,
                media_assets=media_assets,
                model_config=model_config,
                prompt=composition.system_prompt,
                media_root=media_root,
                context_text=composition.context,
                json_mode=True,
                disable_thinking=True,
            )
            payload = _coerce_mimo_v2_payload(response_payload)
            parsed = parse_mimo_v2_payload(payload)
            adapted = adapt_mimo_v2_to_current_payload(parsed)
        except (TimeoutError, httpx.TimeoutException) as exc:
            last_error_code = "provider_timeout"
            last_error_message = str(exc) or "MiMo provider timed out"
            attempt = _record_v2_attempt(
                session_factory,
                run_id=run.id,
                ordinal=ordinal,
                status="timeout",
                error_code=last_error_code,
                error_message=last_error_message,
                started_at=attempt_started_at,
                started_monotonic=started,
            )
            last_error_message = attempt.error_message or last_error_message
            if not _mimo_v2_input_is_current(
                session_factory,
                raw_message_id=int(raw_message_id),
                media_root=media_root,
                expected_fingerprint=analysis_input_fingerprint,
                expected_context=composition.context,
                rebuild_context=context_text is None,
            ):
                return _complete_v2_failure(
                    session_factory,
                    raw_message_id=int(raw_message_id),
                    chat_id=chat_id,
                    run_id=run.id,
                    input_kind=input_kind,
                    model=model,
                    prompt_versions=composition.version_map,
                    error_code="input_changed_during_analysis",
                    error_message="message input changed during MiMo analysis",
                )
            if ordinal < attempts:
                _sleep_before_mimo_retry(retry_delay)
                continue
            break
        except (MimoV2ContractError, MimoV2ExecutionAdapterError) as exc:
            last_error_code = "contract_validation_failed"
            last_error_message = str(exc) or "MiMo v2 contract validation failed"
            attempt = _record_v2_attempt(
                session_factory,
                run_id=run.id,
                ordinal=ordinal,
                status="contract_failure",
                error_code=last_error_code,
                error_message=last_error_message,
                response_payload=response_payload,
                started_at=attempt_started_at,
                started_monotonic=started,
            )
            last_error_message = attempt.error_message or last_error_message
            if not _mimo_v2_input_is_current(
                session_factory,
                raw_message_id=int(raw_message_id),
                media_root=media_root,
                expected_fingerprint=analysis_input_fingerprint,
                expected_context=composition.context,
                rebuild_context=context_text is None,
            ):
                return _complete_v2_failure(
                    session_factory,
                    raw_message_id=int(raw_message_id),
                    chat_id=chat_id,
                    run_id=run.id,
                    input_kind=input_kind,
                    model=model,
                    prompt_versions=composition.version_map,
                    error_code="input_changed_during_analysis",
                    error_message="message input changed during MiMo analysis",
                )
            # The same malformed response is deterministic; only transport
            # failures are retried so fallback can start without added delay.
            break
        except (_MimoV2InvalidJson, json.JSONDecodeError, ValueError) as exc:
            last_error_code = "invalid_json"
            last_error_message = str(exc) or "MiMo response is not valid JSON"
            invalid_response = (
                exc.response_payload
                if isinstance(exc, _MimoV2InvalidJson)
                else response_payload
            )
            attempt = _record_v2_attempt(
                session_factory,
                run_id=run.id,
                ordinal=ordinal,
                status="invalid_json",
                error_code=last_error_code,
                error_message=last_error_message,
                response_payload=invalid_response,
                started_at=attempt_started_at,
                started_monotonic=started,
            )
            last_error_message = attempt.error_message or last_error_message
            if not _mimo_v2_input_is_current(
                session_factory,
                raw_message_id=int(raw_message_id),
                media_root=media_root,
                expected_fingerprint=analysis_input_fingerprint,
                expected_context=composition.context,
                rebuild_context=context_text is None,
            ):
                return _complete_v2_failure(
                    session_factory,
                    raw_message_id=int(raw_message_id),
                    chat_id=chat_id,
                    run_id=run.id,
                    input_kind=input_kind,
                    model=model,
                    prompt_versions=composition.version_map,
                    error_code="input_changed_during_analysis",
                    error_message="message input changed during MiMo analysis",
                )
            # JSON shape errors are deterministic for this response and should
            # fail fast into the guarded fallback path.
            break
        except Exception as exc:
            last_error_code = "provider_http_error"
            last_error_message = str(exc) or "MiMo provider request failed"
            attempt = _record_v2_attempt(
                session_factory,
                run_id=run.id,
                ordinal=ordinal,
                status="http_error",
                error_code=last_error_code,
                error_message=last_error_message,
                started_at=attempt_started_at,
                started_monotonic=started,
            )
            last_error_message = attempt.error_message or last_error_message
            if not _mimo_v2_input_is_current(
                session_factory,
                raw_message_id=int(raw_message_id),
                media_root=media_root,
                expected_fingerprint=analysis_input_fingerprint,
                expected_context=composition.context,
                rebuild_context=context_text is None,
            ):
                return _complete_v2_failure(
                    session_factory,
                    raw_message_id=int(raw_message_id),
                    chat_id=chat_id,
                    run_id=run.id,
                    input_kind=input_kind,
                    model=model,
                    prompt_versions=composition.version_map,
                    error_code="input_changed_during_analysis",
                    error_message="message input changed during MiMo analysis",
                )
            if ordinal < attempts:
                _sleep_before_mimo_retry(retry_delay)
                continue
            break
        else:
            attempt = _record_v2_attempt(
                session_factory,
                run_id=run.id,
                ordinal=ordinal,
                status="completed",
                response_payload=payload,
                started_at=attempt_started_at,
                started_monotonic=started,
            )
            if not _mimo_v2_input_is_current(
                session_factory,
                raw_message_id=int(raw_message_id),
                media_root=media_root,
                expected_fingerprint=analysis_input_fingerprint,
                expected_context=composition.context,
                rebuild_context=context_text is None,
            ):
                return _complete_v2_failure(
                    session_factory,
                    raw_message_id=int(raw_message_id),
                    chat_id=chat_id,
                    run_id=run.id,
                    input_kind=input_kind,
                    model=model,
                    prompt_versions=composition.version_map,
                    error_code="input_changed_during_analysis",
                    error_message="message input changed during MiMo analysis",
                )
            canonical_payload = json.loads(adapted.canonical_v2_json)
            completed = complete_mimo_run(
                session_factory,
                run_id=run.id,
                status="completed",
                selected_ordinal=attempt.ordinal,
                canonical_payload=canonical_payload,
                projection_payload=_execution_projection(adapted.payload),
                became_authoritative=True,
            )
            _record_mimo_v2_prompt_invocation(
                session_factory,
                raw_message_id=int(raw_message_id),
                chat_id=chat_id,
                run_id=run.id,
                model=model,
                prompt_versions=composition.version_map,
                status="completed",
                error_message=None,
            )
            return MimoV2InferenceResult(
                raw_message_id=int(raw_message_id),
                run_id=completed.id,
                parsed_result=parsed,
                adapted_result=adapted,
                input_kind=input_kind,
                model=model,
                prompt_versions=dict(composition.version_map),
                response_size_bytes=_provider_response_size(response_payload),
            )

    return _complete_v2_failure(
        session_factory,
        raw_message_id=int(raw_message_id),
        chat_id=chat_id,
        run_id=run.id,
        input_kind=input_kind,
        model=model,
        prompt_versions=composition.version_map,
        error_code=last_error_code,
        error_message=last_error_message,
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
    error_code: str | None = None,
    error_message: str | None = None,
    response_payload: Any | None = None,
):
    completed_at = utc_now()
    return record_mimo_attempt(
        session_factory,
        run_id=run_id,
        ordinal=ordinal,
        retry_of_ordinal=ordinal - 1 if ordinal > 1 else None,
        status=status,
        error_code=error_code,
        error_message=error_message,
        response_payload=response_payload,
        duration_ms=max(0, round((time.perf_counter() - started_monotonic) * 1000)),
        started_at=started_at,
        completed_at=completed_at,
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
    model_config = _find_mimo_model(config)
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
        payload, error_message = _call_mimo_authoritative_with_retry(
            raw_message=raw_message,
            media_assets=media_assets,
            model_config=model_config,
            prompt=composition.system_prompt,
            media_root=media_root,
            context_text=composition.context,
        )
        experiment = _upsert_experiment_result(
            session,
            raw_message=raw_message,
            model_config=model_config,
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
                model=model_config.model,
                prompt_versions=composition.version_map,
                status="failed" if error_message else "completed",
                error_message=error_message,
            ),
        )
        return MimoAuthoritativeResult(
            raw_message_id=raw_message_id,
            payload=payload,
            input_kind=input_kind,
            model=model_config.model,
            status=experiment.status,
            error_message=error_message,
            prompt_versions=composition.version_map,
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
) -> tuple[dict[str, Any], str | None]:
    errors: list[str] = []
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            payload = _call_mimo_direct_model(
                raw_message=raw_message,
                media_assets=media_assets,
                model_config=model_config,
                prompt=prompt,
                media_root=media_root,
                context_text=context_text,
            )
            _validate_authoritative_payload(payload)
            return payload, None
        except Exception as exc:
            errors.append(str(exc))
            if attempt >= attempts:
                break
            if retry_delay_seconds > 0:
                time.sleep(retry_delay_seconds)
    if len(errors) == 1:
        return {}, errors[0]
    return {}, (
        f"MiMo failed after {len(errors)} attempts: "
        + " | ".join(f"attempt {idx + 1}: {error}" for idx, error in enumerate(errors))
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
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if model_config.api_key:
        headers["Authorization"] = f"Bearer {model_config.api_key}"
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
    with httpx.Client(timeout=model_config.timeout_seconds) as client:
        response = client.post(
            f"{model_config.base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            response_body = exc.response.text[:1200]
            raise RuntimeError(f"{exc}; response_body={response_body}") from exc
        data = response.json()
    content = _extract_chat_content(data)
    raw_response_content = getattr(response, "content", None)
    if isinstance(raw_response_content, bytes):
        response_size_bytes = len(raw_response_content)
    else:
        response_size_bytes = len(
            json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return _MimoProviderPayload(
        _parse_json_object(content),
        response_size_bytes=response_size_bytes,
    )


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
    for model in config.ai_models:
        if model.id == "mimo-v2.5" or model.model == "mimo-v2.5":
            return model
    return None


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

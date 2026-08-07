"""Immediate message-level strategy recognition."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from datetime import timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import (
    AiProviderConfig,
    AiRecognitionConfig,
    load_ai_recognition_config,
)
from telegram_kol_research.config import (
    MultiTargetManagementConfig,
    load_multi_target_management_config,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    MediaAsset,
    MessageInstructionItem,
    MessageRecognition,
    ManagementMessageTarget,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    TradingSetting,
    TradeIdea,
    utc_now,
)
from telegram_kol_research.lifecycle_exit_intents import (
    has_live_execution_binding,
    record_lifecycle_exit_intent,
)
from telegram_kol_research.message_instruction_items import (
    create_message_instruction_items_in_session,
)
from telegram_kol_research.management_directives import (
    FULL_EXIT_ACTIONS,
    build_management_instruction_contract,
    multi_target_action_policy,
    resolve_management_directive,
)
from telegram_kol_research.management_scope import (
    ManagementScopeError,
    resolve_management_scope_in_session,
)
from telegram_kol_research.management_message_targets import (
    management_decision_fingerprint,
    management_parameter_fingerprint,
    project_management_targets_in_session,
)
from telegram_kol_research.strategy_management_contracts import (
    management_contract_fingerprint,
    serialize_management_contract,
)
from telegram_kol_research.raw_ingest import NormalizedMessageRecord
from telegram_kol_research.recognition_profiles import BITCOIN_JUNZHANG_PROFILE
from telegram_kol_research.parsing.text_parser import parse_signal_text
from telegram_kol_research.prompt_composition import compose_trading_prompt
from telegram_kol_research.prompt_defaults import seed_default_prompt_registry
from telegram_kol_research.prompt_registry import (
    PromptInvocationRecord,
    record_prompt_invocation,
)


BLOCKED_SYMBOLS = {
    "QQ",
    "VX",
    "WX",
    "VIP",
    "HTTP",
    "HTTPS",
}

logger = logging.getLogger(__name__)

EXIT_SYMBOL_ALIASES = {
    "BTCUSDT": "BTC",
    "BTC": "BTC",
    "XBT": "BTC",
    "大饼": "BTC",
    "比特币": "BTC",
    "ETHUSDT": "ETH",
    "ETH": "ETH",
    "以太币": "ETH",
    "以太": "ETH",
    "SOLUSDT": "SOL",
    "SOL": "SOL",
    "SOLANA": "SOL",
    "HYPEUSDT": "HYPE",
    "HYPE": "HYPE",
    "DOGEUSDT": "DOGE",
    "DOGE": "DOGE",
    "BNBUSDT": "BNB",
    "BNB": "BNB",
    "XRPUSDT": "XRP",
    "XRP": "XRP",
    "ADAUSDT": "ADA",
    "ADA": "ADA",
    "SUIUSDT": "SUI",
    "SUI": "SUI",
    "LINKUSDT": "LINK",
    "LINK": "LINK",
}

DUPLICATE_ACTIVE_STRATEGY_WINDOW_HOURS = 72
BITCOIN_JUNZHANG_CHAT_ID = BITCOIN_JUNZHANG_PROFILE.chat_id
BITCOIN_JUNZHANG_PARSE_SOURCE = BITCOIN_JUNZHANG_PROFILE.parse_source
LOW_CONFIDENCE_GROUP_EXIT_GENERATION = "low_confidence_group_exit:v1"
LOW_CONFIDENCE_GROUP_EXIT_CUTOFF_KEY = "low_confidence_group_exit_cutoff"

ENTRY_TERMS = [
    "建仓",
    "入场",
    "进场",
    "挂单",
    "现价开",
    "市价",
    "开仓",
    "open",
    "entry",
    "enter",
]

POSITION_MANAGEMENT_TERMS = [
    "继续持有",
    "持有",
    "浮亏",
    "浮盈",
    "上推",
    "保护价",
    "分批止盈",
    "减仓",
    "补仓",
    "别追",
    "不要逆势加仓",
]

OCR_RECAP_REFERENCE_TERMS = [
    "盈利",
    "已盈利",
    "已经盈利",
    "会员空单盈利",
    "会员多单盈利",
    "参考",
    "复盘",
    "所长分享",
]


@dataclass(frozen=True)
class MessageRecognitionResult:
    raw_message_id: int
    status: str
    summary: str | None = None
    reason: str | None = None
    ai_payload: dict[str, Any] | None = None
    parse_source: str | None = None


@dataclass(frozen=True)
class _SymbolPriceScaleConflict:
    original_symbol: str
    suggested_symbol: str
    reason: str


def recognize_message_now(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path = "config/ai_recognition.yaml",
) -> MessageRecognitionResult:
    """Run V1 immediate recognition for one raw message and persist the result."""

    config = ai_recognition_config or load_ai_recognition_config(ai_recognition_config_path)
    seed_default_prompt_registry(session_factory, config)
    deepseek_composition = compose_trading_prompt(
        session_factory, model_kind="deepseek", context=""
    )
    mimo_composition = compose_trading_prompt(
        session_factory, model_kind="mimo", context=""
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
        if _has_video_like_media(media_assets):
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="视频消息默认跳过",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        text = raw_message.text or ""
        if text.strip() and _looks_like_trading_education_content(text):
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="交易教学/经验总结内容，不作为策略信号处理。",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        if text.strip() and config.text_provider.is_configured:
            ai_event_result = _apply_ai_lifecycle_event_if_matched(
                session,
                raw_message=raw_message,
                config=config,
                system_prompt=deepseek_composition.system_prompt,
                session_factory=session_factory,
                prompt_versions=deepseek_composition.version_map,
            )
            if ai_event_result is not None:
                _upsert_recognition(session, ai_event_result, engine=config.text_provider.model)
                session.commit()
                return ai_event_result
            if _apply_lifecycle_transition_signal_if_matched(session, raw_message, text):
                result = MessageRecognitionResult(
                    raw_message_id=raw_message.id,
                    status="非策略",
                    reason="本地规则识别到明确入场/取消/离场消息，已更新匹配的策略状态。",
                    parse_source="text",
                )
                _upsert_recognition(session, result)
                session.commit()
                return result

        if text.strip():
            kol_profile_result = _apply_bitcoin_junzhang_profile_if_matched(
                session,
                raw_message,
            )
            if kol_profile_result is not None:
                _upsert_recognition(session, kol_profile_result)
                session.commit()
                return kol_profile_result

        if (
            text.strip()
            and not config.text_provider.is_configured
            and _apply_lifecycle_transition_signal_if_matched(session, raw_message, text)
        ):
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="本地规则识别到明确入场/取消/离场消息，已更新匹配的策略状态。",
                parse_source="text",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        if _has_image_like_media(media_assets) and config.image_provider.is_configured:
            if _is_glm_ocr_model(config.image_provider.model):
                result = _recognize_with_glm_ocr(
                    raw_message=raw_message,
                    media_assets=media_assets,
                    config=config,
                    session=session,
                    text_system_prompt=deepseek_composition.system_prompt,
                    session_factory=session_factory,
                    prompt_versions=deepseek_composition.version_map,
                )
            else:
                result = _invoke_with_prompt_audit(
                    session_factory=session_factory,
                    raw_message=raw_message,
                    model=config.image_provider.model,
                    prompt_versions=mimo_composition.version_map,
                    call=lambda: _recognize_with_ai_provider(
                        raw_message=raw_message,
                        media_assets=media_assets,
                        config=config,
                        provider=config.image_provider,
                        parse_source="image_ai",
                        system_prompt=mimo_composition.system_prompt,
                    ),
                )
                _persist_ai_result(session, raw_message, result, engine=config.image_provider.model)
            session.commit()
            return result

        if (raw_message.text or "").strip() and config.text_provider.is_configured:
            result = _invoke_with_prompt_audit(
                session_factory=session_factory,
                raw_message=raw_message,
                model=config.text_provider.model,
                prompt_versions=deepseek_composition.version_map,
                call=lambda: _recognize_with_ai_provider(
                    raw_message=raw_message,
                    media_assets=[],
                    config=config,
                    provider=config.text_provider,
                    parse_source="text_ai",
                    system_prompt=deepseek_composition.system_prompt,
                ),
            )
            _persist_ai_result(session, raw_message, result, engine=config.text_provider.model)
            session.commit()
            return result

        if _has_image_like_media(media_assets) and not (raw_message.text or "").strip():
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="待识别",
                reason="图片识别等待 OCR/AI 接入",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        parsed = parse_signal_text(raw_message.text or "")
        if _apply_lifecycle_transition_signal_if_matched(session, raw_message, raw_message.text or ""):
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="本地规则识别到明确入场/取消/离场消息，已更新匹配的策略状态。",
                parse_source="text",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        if not _is_actionable_entry_signal(raw_message.text or "", parsed):
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="未识别到可执行新入场策略",
            )
            _upsert_recognition(session, result)
            session.commit()
            return result

        candidate = _upsert_signal_candidate(session, raw_message, parsed)
        _ensure_lifecycle_record(session, raw_message, candidate)
        result = MessageRecognitionResult(
            raw_message_id=raw_message.id,
            status="是策略",
            summary=_format_candidate_summary(candidate),
        )
        _upsert_recognition(session, result)
        session.commit()
        return result


def recognize_records_with_ai_config(
    session_factory: sessionmaker,
    records: list[NormalizedMessageRecord],
    *,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path = "config/ai_recognition.yaml",
    fallback_recognizer=None,
) -> dict[str, int]:
    """Recognize newly persisted records using the shared AI recognition config."""

    config = ai_recognition_config or load_ai_recognition_config(ai_recognition_config_path)
    if not (config.text_provider.is_configured or config.image_provider.is_configured):
        if fallback_recognizer is None:
            return {
                "inserted_candidates": 0,
                "processed_records": len(records),
                "recognized_messages": 0,
                "failed_recognitions": 0,
                "skipped_existing": 0,
            }
        return fallback_recognizer(session_factory, records)

    raw_message_ids: list[int] = []
    skipped_existing = 0
    with session_factory() as session:
        for record in records:
            raw_message = (
                session.query(RawMessage)
                .filter(
                    RawMessage.chat_id == record.chat_id,
                    RawMessage.message_id == record.message_id,
                )
                .one_or_none()
            )
            if raw_message is None:
                continue
            existing_recognition = (
                session.query(MessageRecognition)
                .filter(MessageRecognition.raw_message_id == raw_message.id)
                .one_or_none()
            )
            if existing_recognition is not None:
                skipped_existing += 1
                continue
            raw_message_ids.append(raw_message.id)

    recognized_messages = 0
    failed_recognitions = 0
    inserted_candidates = 0
    for raw_message_id in raw_message_ids:
        try:
            result = recognize_message_now(
                session_factory,
                raw_message_id=raw_message_id,
                ai_recognition_config=config,
            )
        except Exception:
            failed_recognitions += 1
            continue
        recognized_messages += 1
        if result.parse_source in {"text_ai", "image_ai"} and result.summary:
            inserted_candidates += 1

    return {
        "inserted_candidates": inserted_candidates,
        "processed_records": len(records),
        "recognized_messages": recognized_messages,
        "failed_recognitions": failed_recognitions,
        "skipped_existing": skipped_existing,
    }


def filter_records_by_inserted_message_keys(
    records: list[NormalizedMessageRecord],
    stats: dict[str, Any],
) -> list[NormalizedMessageRecord]:
    """Return records that were inserted by the preceding persistence call."""

    inserted_keys = {
        (int(chat_id), int(message_id))
        for chat_id, message_id in stats.get("inserted_message_keys", [])
    }
    if not inserted_keys:
        return []
    return [
        record
        for record in records
        if (record.chat_id, record.message_id) in inserted_keys
    ]


def _upsert_signal_candidate(session, raw_message: RawMessage, parsed) -> SignalCandidate:
    candidate = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message.id)
        .filter(SignalCandidate.event_type == "entry_signal")
        .order_by(SignalCandidate.id.asc())
        .first()
    )
    if candidate is None:
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            source_id=None,
            parse_source="text",
            review_status="pending",
        )
        session.add(candidate)

    candidate.symbol = parsed.symbol
    candidate.side = parsed.side
    candidate.event_type = parsed.event_type
    candidate.target_lifecycle_id = None
    candidate.management_action = None
    candidate.management_fraction = None
    candidate.recognition_generation = None
    candidate.entry_text = _format_entry_range(parsed.entry_range)
    candidate.stop_loss_text = _format_number(parsed.stop_loss)
    candidate.take_profit_text = " / ".join(
        _format_number(value) for value in parsed.take_profits
    ) or None
    candidate.leverage_text = parsed.leverage
    candidate.confidence = parsed.confidence
    return candidate


def _is_glm_ocr_model(model: str) -> bool:
    """Check if the configured image model is GLM-OCR (Zhipu layout parsing API)."""
    return model.strip().lower() == "glm-ocr"


def _call_glm_ocr_api(provider: AiProviderConfig, data_url: str) -> str:
    """Call the Zhipu GLM-OCR layout_parsing API and return recognized Markdown text.

    The GLM-OCR endpoint is a dedicated document-parsing API, not the standard
    chat completions endpoint.  It accepts a ``file`` parameter (URL or base64
    data URL) and returns ``md_results`` containing the extracted text.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"

    payload: dict[str, Any] = {
        "model": "glm-ocr",
        "file": data_url,
    }

    api_url = f"{provider.base_url.rstrip('/')}/layout_parsing"
    with httpx.Client(timeout=provider.timeout_seconds) as client:
        response = client.post(api_url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    return (data.get("md_results") or "").strip()


def _recognize_with_glm_ocr(
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    config: AiRecognitionConfig,
    session,
    text_system_prompt: str,
    session_factory: sessionmaker,
    prompt_versions: dict[str, int],
) -> MessageRecognitionResult:
    """Recognize image messages using GLM-OCR in a two-step pipeline.

    1. Send each image to the GLM-OCR layout_parsing API to extract text.
    2. If a text AI provider is configured, feed the OCR text (plus any
       caption) through the text provider for strategy recognition.
    3. Otherwise fall back to the local rule parser on the OCR output.
    """
    # ── Guard: all media files must be available locally ──────────────
    missing_reason = _media_missing_reason(media_assets)
    if missing_reason:
        result = MessageRecognitionResult(
            raw_message_id=raw_message.id,
            status="识别失败",
            reason=missing_reason,
            parse_source="image_ai",
        )
        _upsert_recognition(
            session, result, engine=config.image_provider.model,
        )
        return result

    # ── Step 1: extract text from all image assets via GLM-OCR ──────────
    ocr_parts: list[str] = []
    for asset in media_assets:
        data_url = _media_asset_to_data_url(asset)
        if not data_url:
            continue
        try:
            ocr_text = _call_glm_ocr_api(config.image_provider, data_url)
            if ocr_text:
                asset.ocr_text = ocr_text  # persist OCR result for web display
                ocr_parts.append(ocr_text)
        except Exception as exc:
            # Log and continue – one failing image should not block the rest
            error_text = f"[OCR 失败: {exc}]"
            asset.ocr_text = error_text
            ocr_parts.append(error_text)

    # Merge caption and OCR results
    caption = (raw_message.text or "").strip()
    merged_text = "\n".join(
        part for part in ([caption] + ocr_parts) if part
    )

    if not merged_text.strip():
        result = MessageRecognitionResult(
            raw_message_id=raw_message.id,
            status="识别失败",
            reason="图片识别失败：GLM-OCR 未能提取到文字内容",
            parse_source="image_ai",
        )
        _upsert_recognition(
            session, result, engine=config.image_provider.model,
        )
        return result

    # ── Step 2: strategy recognition on the extracted text ──────────────
    if config.text_provider.is_configured:
        # Use text AI provider for strategy recognition
        result = _invoke_with_prompt_audit(
            session_factory=session_factory,
            raw_message=raw_message,
            model=config.text_provider.model,
            prompt_versions=prompt_versions,
            call=lambda: _recognize_text_with_ai_provider(
                raw_message=raw_message,
                merged_text=merged_text,
                config=config,
                system_prompt=text_system_prompt,
            ),
        )
        if (
            caption
            and ocr_parts
            and result.status == "是策略"
            and _caption_looks_like_ocr_recap_reference(caption)
        ):
            result = MessageRecognitionResult(
                raw_message_id=raw_message.id,
                status="非策略",
                reason=(
                    "图文消息正文是盈利/参考/复盘语境，且正文自身不构成完整新开仓策略；"
                    "图片 OCR 中的策略文字按历史截图处理，不创建新策略。"
                ),
                ai_payload=result.ai_payload,
                parse_source="image_ai",
            )
            _upsert_recognition(session, result, engine=config.text_provider.model)
            return result
        if caption and ocr_parts and result.status != "是策略":
            try:
                text_only_result = _invoke_with_prompt_audit(
                    session_factory=session_factory,
                    raw_message=raw_message,
                    model=config.text_provider.model,
                    prompt_versions=prompt_versions,
                    call=lambda: _recognize_text_with_ai_provider(
                        raw_message=raw_message,
                        merged_text=caption,
                        config=config,
                        system_prompt=text_system_prompt,
                    ),
                )
            except Exception:
                text_only_result = None
            if text_only_result is not None and text_only_result.status == "是策略":
                _persist_ai_result(
                    session,
                    raw_message,
                    text_only_result,
                    engine=config.text_provider.model,
                )
                return text_only_result
        _persist_ai_result(
            session, raw_message, result, engine=config.text_provider.model,
        )
        # Tag the parse_source to reflect the image→ocr→text_ai pipeline
        object.__setattr__(result, "parse_source", "image_ai")
        return result

    # Fall back to local rule parser
    parsed = parse_signal_text(merged_text)
    if not _is_actionable_entry_signal(merged_text, parsed):
        result = MessageRecognitionResult(
            raw_message_id=raw_message.id,
            status="非策略",
            reason="GLM-OCR 识别完成，但未检测到可执行新入场策略",
            parse_source="image_ai",
        )
        _upsert_recognition(
            session, result, engine=config.image_provider.model,
        )
        return result

    candidate = _upsert_signal_candidate(session, raw_message, parsed)
    result = MessageRecognitionResult(
        raw_message_id=raw_message.id,
        status="是策略",
        summary=_format_candidate_summary(candidate),
        parse_source="image_ai",
    )
    _upsert_recognition(session, result, engine=config.image_provider.model)
    return result


def _recognize_text_with_ai_provider(
    *,
    raw_message: RawMessage,
    merged_text: str,
    config: AiRecognitionConfig,
    system_prompt: str,
) -> MessageRecognitionResult:
    """Send plain text (already merged with OCR output) through the text AI
    provider for strategy recognition."""
    payload = _build_ai_recognition_payload(
        raw_message=raw_message,
        media_assets=[],
        prompt=system_prompt,
        model=config.text_provider.model,
    )
    # Override the user message content with the merged text
    payload["messages"][1]["content"] = merged_text

    provider = config.text_provider
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"

    with httpx.Client(timeout=provider.timeout_seconds) as client:
        response = client.post(
            _chat_completions_url(provider.base_url),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()

    content = _extract_ai_content(data)
    parsed = _parse_ai_result_json(content)
    _repair_ai_strategy_entry_from_text(parsed, merged_text)
    return _result_from_ai_payload(
        raw_message_id=raw_message.id,
        payload=parsed,
        parse_source="text_ai",
    )


def _recognize_with_ai_provider(
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    config: AiRecognitionConfig,
    provider: AiProviderConfig,
    parse_source: str,
    system_prompt: str,
) -> MessageRecognitionResult:
    if media_assets and not any(_media_asset_to_data_url(asset) for asset in media_assets):
        return MessageRecognitionResult(
            raw_message_id=raw_message.id,
            status="识别失败",
            reason="图片文件未下载到本地，请重新同步该消息后再识别",
        )
    payload = _build_ai_recognition_payload(
        raw_message=raw_message,
        media_assets=media_assets,
        prompt=system_prompt,
        model=provider.model,
    )
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    with httpx.Client(timeout=provider.timeout_seconds) as client:
        response = client.post(
            _chat_completions_url(provider.base_url),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
    content = _extract_ai_content(data)
    parsed = _parse_ai_result_json(content)
    _repair_ai_strategy_entry_from_text(parsed, raw_message.text or "")
    return _result_from_ai_payload(
        raw_message_id=raw_message.id,
        payload=parsed,
        parse_source=parse_source,
    )


def _build_ai_recognition_payload(
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    prompt: str,
    model: str,
) -> dict[str, Any]:
    user_text = (
        f"Message metadata:\n"
        f"chat_id={raw_message.chat_id}\n"
        f"message_id={raw_message.message_id}\n"
        f"sender={raw_message.sender_name or 'Unknown'}\n\n"
        f"Text/caption:\n{(raw_message.text or '').strip() or '(empty)'}"
    )
    user_parts: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for media_asset in media_assets:
        data_url = _media_asset_to_data_url(media_asset)
        if data_url:
            user_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            )
    user_content: str | list[dict[str, Any]] = user_parts if len(user_parts) > 1 else user_text
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
    }


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _apply_ai_lifecycle_event_if_matched(
    session,
    *,
    raw_message: RawMessage,
    config: AiRecognitionConfig,
    system_prompt: str,
    session_factory: sessionmaker,
    prompt_versions: dict[str, int],
) -> MessageRecognitionResult | None:
    context = _load_lifecycle_event_context(session, raw_message)
    if not context["active_strategies"]:
        return None

    try:
        decision = _invoke_with_prompt_audit(
            session_factory=session_factory,
            raw_message=raw_message,
            model=config.text_provider.model,
            prompt_versions=prompt_versions,
            call=lambda: _call_lifecycle_event_ai(
                raw_message=raw_message,
                context=context,
                config=config,
                system_prompt=system_prompt,
            ),
        )
    except Exception:
        return None

    if not _apply_lifecycle_event_decision(session, raw_message, decision):
        return None

    event_type = str(decision.get("event_type") or "").strip()
    if (
        event_type == "cancel_entry"
        and _cancel_signal_applies_to_all_matches(raw_message.text or "")
        and _parse_explicit_cancel_signal(raw_message.text or "") is not None
    ):
        _apply_lifecycle_transition_signal_if_matched(
            session,
            raw_message,
            raw_message.text or "",
        )
    reason = str(decision.get("reason") or "").strip()
    return MessageRecognitionResult(
        raw_message_id=raw_message.id,
        status="非策略",
        reason=reason or f"AI 判定为策略生命周期事件：{event_type}",
        ai_payload={"lifecycle_event": decision},
        parse_source="lifecycle_ai",
    )


def _invoke_with_prompt_audit(
    *,
    session_factory: sessionmaker,
    raw_message: RawMessage,
    model: str,
    prompt_versions: dict[str, int],
    call,
):
    error_message: str | None = None
    try:
        return call()
    except Exception as exc:
        error_message = str(exc)
        raise
    finally:
        record_prompt_invocation(
            session_factory,
            PromptInvocationRecord(
                feature="message_recognition",
                correlation_key=f"recognition:{raw_message.id}:legacy:{model}",
                raw_message_id=raw_message.id,
                chat_id=raw_message.chat_id,
                model=model,
                prompt_versions=prompt_versions,
                status="failed" if error_message else "completed",
                error_message=error_message,
            ),
        )


def _load_lifecycle_event_context(session, raw_message: RawMessage) -> dict[str, Any]:
    posted_at = raw_message.posted_at or utc_now()
    since = posted_at - timedelta(hours=48)
    active_rows = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
        .filter(StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered", "expired"]))
        .filter(StrategyLifecycle.signal_at <= posted_at)
        .filter(StrategyLifecycle.signal_at >= since)
        .order_by(StrategyLifecycle.signal_at.desc(), StrategyLifecycle.id.desc())
        .limit(8)
        .all()
    )
    active_rows = [
        lifecycle
        for lifecycle in active_rows
        if lifecycle.lifecycle_status != "expired"
        or _lifecycle_has_live_execution_binding(session, lifecycle)
    ]
    active_strategies: list[dict[str, Any]] = []
    for lifecycle in active_rows:
        original = (
            session.query(RawMessage)
            .filter(RawMessage.chat_id == lifecycle.chat_id)
            .filter(RawMessage.message_id == lifecycle.message_id)
            .one_or_none()
        )
        active_strategies.append(
            {
                "lifecycle_id": lifecycle.id,
                "message_id": lifecycle.message_id,
                "status": lifecycle.lifecycle_status,
                "symbol": lifecycle.symbol,
                "side": lifecycle.side,
                "signal_at": str(lifecycle.signal_at),
                "entered_at": str(lifecycle.entered_at) if lifecycle.entered_at else None,
                "entry_range": _format_lifecycle_range(
                    lifecycle.entry_range_low,
                    lifecycle.entry_range_high,
                ),
                "entry_price_actual": lifecycle.entry_price_actual,
                "stop_loss": lifecycle.stop_loss,
                "take_profit": lifecycle.take_profit,
                "original_text": _compact_context_text(original.text if original is not None else None),
            }
        )

    recent_messages = (
        session.query(RawMessage)
        .filter(RawMessage.chat_id == raw_message.chat_id)
        .filter(RawMessage.message_id <= raw_message.message_id)
        .order_by(RawMessage.message_id.desc())
        .limit(12)
        .all()
    )
    recent_context = [
        {
            "message_id": item.message_id,
            "posted_at": str(item.posted_at) if item.posted_at else None,
            "text": _compact_context_text(item.text),
        }
        for item in reversed(recent_messages)
    ]
    reply_context: dict[str, Any] | None = None
    if raw_message.reply_to_message_id is not None:
        replied_message = (
            session.query(RawMessage)
            .filter(RawMessage.chat_id == raw_message.chat_id)
            .filter(RawMessage.message_id == raw_message.reply_to_message_id)
            .one_or_none()
        )
        replied_lifecycle = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
            .filter(StrategyLifecycle.message_id == raw_message.reply_to_message_id)
            .one_or_none()
        )
        if replied_message is not None and replied_lifecycle is not None:
            reply_context = {
                "message_id": replied_message.message_id,
                "lifecycle_id": replied_lifecycle.id,
                "lifecycle_status": replied_lifecycle.lifecycle_status,
                "symbol": replied_lifecycle.symbol,
                "side": replied_lifecycle.side,
                "entry_range": _format_lifecycle_range(
                    replied_lifecycle.entry_range_low,
                    replied_lifecycle.entry_range_high,
                ),
                "original_text": _compact_context_text(replied_message.text),
            }
    return {
        "active_strategies": active_strategies,
        "recent_messages": recent_context,
        "reply_context": reply_context,
    }


def _format_lifecycle_range(low: float | None, high: float | None) -> str | None:
    if low is None and high is None:
        return None
    if low is None:
        return _format_number(high)
    if high is None or low == high:
        return _format_number(low)
    return f"{_format_number(low)}-{_format_number(high)}"


def _compact_context_text(text: str | None, *, limit: int = 260) -> str | None:
    if not text:
        return None
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def _call_lifecycle_event_ai(
    *,
    raw_message: RawMessage,
    context: dict[str, Any],
    config: AiRecognitionConfig,
    system_prompt: str,
) -> dict[str, Any]:
    provider = config.text_provider
    payload = {
        "model": provider.model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_message": {
                            "chat_id": raw_message.chat_id,
                            "message_id": raw_message.message_id,
                            "reply_to_message_id": raw_message.reply_to_message_id,
                            "posted_at": str(raw_message.posted_at) if raw_message.posted_at else None,
                            "sender": raw_message.sender_name,
                            "text": raw_message.text or "",
                        },
                        **context,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0,
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    with httpx.Client(timeout=provider.timeout_seconds) as client:
        response = client.post(
            _chat_completions_url(provider.base_url),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
    parsed = _parse_ai_result_json(_extract_ai_content(data))
    lifecycle = parsed.get("lifecycle_event")
    return lifecycle if isinstance(lifecycle, dict) else parsed


def _is_authorized_exact_context_risk_reduction(
    decision: dict[str, Any],
    confidence: float,
) -> bool:
    """Recognize the narrow internal exception created by context resolution."""

    return (
        decision.get("_exact_context_risk_reduction_authorized") is True
        and 0.60 <= confidence < 0.70
        and str(decision.get("event_type") or "")
        in {"exit_position", "exit_full", "full_exit", "close_position"}
        and str(decision.get("management_action") or "")
        in {"exit_full", "full_exit"}
        and _int_or_none(decision.get("target_lifecycle_id")) is not None
        and decision.get("_explicit_multi_target") is not True
        and decision.get("targets") in (None, [])
    )


def _apply_lifecycle_event_decision(
    session,
    raw_message: RawMessage,
    decision: dict[str, Any],
    *,
    parse_source: str = "lifecycle_ai",
    authoritative_generation: str | None = None,
    applied_candidate_ids: set[int] | None = None,
    current_message_text: str | None = None,
    multi_target_management_config: MultiTargetManagementConfig | None = None,
) -> bool:
    instruction_text = (
        raw_message.text or ""
        if current_message_text is None
        else current_message_text
    )
    target_decisions = _expand_lifecycle_event_targets(decision)
    if target_decisions is None:
        return False
    if len(target_decisions) != 1 or target_decisions[0] is not decision:
        if _multi_target_action_is_live(
            decision,
            multi_target_management_config,
        ):
            admissions = _admit_explicit_management_targets_in_session(
                session,
                raw_message=raw_message,
                target_decisions=target_decisions,
                instruction_text=instruction_text,
            )
            target_decisions = [
                admission.decision
                for admission in admissions
                if admission.accepted
            ]
        else:
            if _effective_multi_target_action(decision) != "partial_take_profit":
                return False
            if not _validate_explicit_management_targets_in_session(
                session,
                raw_message=raw_message,
                target_decisions=target_decisions,
                instruction_text=instruction_text,
            ):
                return False
        applied = False
        for target_decision in target_decisions:
            applied = _apply_lifecycle_event_decision(
                session,
                raw_message,
                target_decision,
                parse_source=parse_source,
                authoritative_generation=authoritative_generation,
                applied_candidate_ids=applied_candidate_ids,
                current_message_text=instruction_text,
                multi_target_management_config=multi_target_management_config,
            ) or applied
        return applied

    event_type = str(decision.get("event_type") or "none").strip()
    try:
        confidence = float(decision.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if event_type == "none" or (
        confidence < 0.7
        and not _is_authorized_exact_context_risk_reduction(decision, confidence)
    ):
        return False
    exact_context_confidence = (
        confidence
        if _is_authorized_exact_context_risk_reduction(decision, confidence)
        else None
    )
    if _looks_like_trading_education_content(raw_message.text or ""):
        return False
    if event_type == "exit_position" and _exit_decision_looks_like_management_update(
        instruction_text,
        decision,
    ):
        decision = dict(decision)
        event_type = "position_update"
        decision["event_type"] = event_type
        if not str(decision.get("management_action") or "").strip():
            decision["management_action"] = _management_action_for_exit_downgrade(
                instruction_text,
                decision,
            )
        if not decision.get("take_profit") and decision.get("exit_price"):
            decision["take_profit"] = decision.get("exit_price")
        decision["exit_price"] = None

    try:
        management_directive = resolve_management_directive(
            text=instruction_text,
            lifecycle_event=decision,
        )
        if management_directive.intent == "none":
            normalized_management_action = (
                str(decision.get("management_action") or "").strip().lower()
                or "position_update"
            )
            management_fraction = None
        else:
            normalized_management_action = management_directive.intent
            management_fraction = management_directive.fraction
    except ValueError as exc:
        if str(exc) != "management_fraction_ambiguous":
            raise
        return False

    reply_target = _resolve_reply_lifecycle_target(session, raw_message)
    explicit_target_lifecycle_id = _int_or_none(
        decision.get("target_lifecycle_id")
    )
    if (
        _is_authorized_exact_context_risk_reduction(decision, confidence)
        and reply_target is not None
        and explicit_target_lifecycle_id is not None
        and int(reply_target.id) != explicit_target_lifecycle_id
    ):
        return False
    target = _resolve_lifecycle_event_target(session, raw_message, decision)
    if target is None:
        return False
    management_contract_json = None
    management_contract_fingerprint_value = None
    if management_directive.intent == "partial_then_break_even":
        contract_event = dict(decision)
        target_binding = (
            session.get(ExecutionBinding, int(target.execution_binding_id))
            if target.execution_binding_id is not None
            else None
        )
        exact_strategy_instance_id = (
            str(target_binding.strategy_instance_id)
            if target_binding is not None
            and target_binding.strategy_instance_id
            and target_binding.chat_id == target.chat_id
            and target_binding.message_id == target.message_id
            and str(target_binding.symbol).upper() == str(target.symbol).upper()
            and str(target_binding.side).lower() == str(target.side).lower()
            else None
        )
        contract_event.update(
            {
                "target_lifecycle_id": target.id,
                "strategy_instance_id": exact_strategy_instance_id,
                "symbol": target.symbol,
                "side": target.side,
            }
        )
        management_contract = build_management_instruction_contract(
            text=instruction_text,
            lifecycle_event=contract_event,
        )
        management_contract_json = serialize_management_contract(
            management_contract
        )
        management_contract_fingerprint_value = management_contract_fingerprint(
            management_contract
        )
    if event_type == "cancel_entry" and reply_target is not None:
        if reply_target.id != target.id:
            _record_reply_cancel_manual_review(
                session,
                raw_message=raw_message,
                lifecycle=reply_target,
                reason="reply_cancel_target_mismatch",
            )
            return True
        if reply_target.lifecycle_status == "entered" or (
            reply_target.lifecycle_status == "expired"
            and _lifecycle_has_live_execution_binding(session, reply_target)
        ):
            _record_reply_cancel_manual_review(
                session,
                raw_message=raw_message,
                lifecycle=reply_target,
                reason="manual_review_required",
            )
            return True
    explicit_symbol = _extract_exit_symbol(raw_message.text or "")
    if (
        not decision.get("_explicit_multi_target")
        and explicit_symbol is not None
        and target.symbol != explicit_symbol
        and not _text_mentions_exit_symbol(raw_message.text or "", target.symbol)
    ):
        return False

    event_at = raw_message.posted_at or utc_now()
    if event_type == "entry_confirm" and target.lifecycle_status == "pending_entry":
        target.lifecycle_status = "entered"
        target.entered_at = event_at
        target.entry_signal_message_id = raw_message.message_id
        entry_price = _number_or_none(decision.get("entry_price"))
        if entry_price is not None:
            target.entry_price_actual = entry_price
        target.exit_reason = None
        target.exited_at = None
        target.updated_at = utc_now()
        candidate = _upsert_entry_confirmation_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            entry_price=entry_price,
            parse_source=parse_source,
        )
        _remember_applied_candidate(session, candidate, applied_candidate_ids)
        return True

    if event_type == "cancel_entry" and target.lifecycle_status == "pending_entry":
        target.lifecycle_status = "exited"
        target.exit_reason = "cancelled"
        target.exited_at = event_at
        target.exit_signal_message_id = raw_message.message_id
        target.updated_at = utc_now()
        candidate = _upsert_close_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            parse_source=parse_source,
        )
        _remember_applied_candidate(session, candidate, applied_candidate_ids)
        return True

    if event_type == "cancel_entry" and (
        target.lifecycle_status == "entered"
        or (
            target.lifecycle_status == "expired"
            and _lifecycle_has_live_execution_binding(session, target)
        )
    ):
        if not _lifecycle_has_live_execution_binding(session, target):
            return False
        record_lifecycle_exit_intent(
            session,
            target,
            exit_message_id=raw_message.message_id,
            reason=str(decision.get("reason") or "").strip() or None,
        )
        candidate = _upsert_close_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            parse_source=parse_source,
            management_action="full_exit",
            management_fraction=None,
            recognition_generation=authoritative_generation,
            confidence=exact_context_confidence,
        )
        _remember_applied_candidate(session, candidate, applied_candidate_ids)
        return True

    if event_type == "exit_position" and (
        target.lifecycle_status == "entered"
        or (
            target.lifecycle_status == "expired"
            and _lifecycle_has_live_execution_binding(session, target)
        )
    ):
        if _lifecycle_has_live_execution_binding(session, target):
            record_lifecycle_exit_intent(
                session,
                target,
                exit_message_id=raw_message.message_id,
                reason=str(decision.get("reason") or "").strip() or None,
            )
            candidate = _upsert_close_signal_candidate(
                session,
                raw_message=raw_message,
                lifecycle=target,
                parse_source=parse_source,
                management_action=normalized_management_action,
                management_fraction=management_fraction,
                recognition_generation=authoritative_generation,
                confidence=exact_context_confidence,
            )
            _remember_applied_candidate(session, candidate, applied_candidate_ids)
            return True
        target.lifecycle_status = "exited"
        target.exit_reason = "kol_signal"
        target.exited_at = event_at
        exit_price = _number_or_none(decision.get("exit_price"))
        if exit_price is not None:
            target.exit_price_actual = exit_price
        target.exit_signal_message_id = raw_message.message_id
        target.updated_at = utc_now()
        candidate = _upsert_close_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            parse_source=parse_source,
            management_action=normalized_management_action,
            management_fraction=management_fraction,
            recognition_generation=authoritative_generation,
            confidence=exact_context_confidence,
        )
        _remember_applied_candidate(session, candidate, applied_candidate_ids)
        return True

    if event_type == "position_update" and target.lifecycle_status == "entered":
        raw_management_action = (
            str(decision.get("management_action") or "").strip() or "position_update"
        )
        management_note = str(decision.get("reason") or "").strip() or None
        explicit_stop_loss = _number_or_none(decision.get("stop_loss"))
        if explicit_stop_loss is None:
            explicit_stop_loss = _extract_explicit_stop_loss_from_management_text(
                raw_message.text
            )
        if explicit_stop_loss is not None and not _is_plausible_management_stop_loss(
            target,
            explicit_stop_loss,
        ):
            explicit_stop_loss = None
        explicit_take_profit = _normalize_strategy_text(
            decision.get("take_profit"),
            separator="/",
        )
        protective_stop = (
            _protective_stop_price(target)
            if _should_move_stop_to_protect(
                current_text=raw_message.text,
                decision=decision,
                management_action=raw_management_action,
            )
            else None
        )
        requested_stop_loss = (
            explicit_stop_loss if explicit_stop_loss is not None else protective_stop
        )
        candidate = _upsert_management_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            parse_source=parse_source,
            management_action=normalized_management_action,
            management_fraction=management_fraction,
            recognition_generation=authoritative_generation,
            requested_stop_loss=requested_stop_loss,
            stop_price_source=(
                str(decision.get("stop_price_source"))
                if decision.get("stop_price_source") not in (None, "")
                else management_directive.stop_price_source
            ),
            requested_take_profit=explicit_take_profit,
            management_contract_json=management_contract_json,
            management_contract_fingerprint=(
                management_contract_fingerprint_value
            ),
        )
        _remember_applied_candidate(session, candidate, applied_candidate_ids)
        return True

    return False


def _remember_applied_candidate(
    session,
    candidate: SignalCandidate,
    applied_candidate_ids: set[int] | None,
) -> None:
    if applied_candidate_ids is None:
        return
    session.flush()
    applied_candidate_ids.add(candidate.id)


def normalize_management_intent(
    decision: Mapping[str, Any],
    text: str,
) -> tuple[str, float | None]:
    """Normalize an actionable lifecycle decision through deterministic policy."""

    directive = resolve_management_directive(
        text=text,
        lifecycle_event=decision,
    )
    if directive.intent == "none":
        raw_action = str(decision.get("management_action") or "").strip().lower()
        return raw_action or "position_update", None
    return directive.intent, directive.fraction


def _explicit_management_fraction(
    decision: Mapping[str, Any],
    combined_text: str,
) -> float | None:
    explicit_close_fractions: list[float] = []
    for key in ("management_fraction", "close_fraction", "fraction"):
        normalized = _fraction_value(decision.get(key))
        if normalized is not None:
            explicit_close_fractions.append(normalized)

    close_percentages = re.findall(
        r"(?:止盈|减仓|平仓|平掉|出掉|出局)[^\d%\uff05]{0,12}(\d+(?:\.\d+)?)\s*[%\uff05]",
        combined_text,
    )
    explicit_close_fractions.extend(
        fraction
        for value in close_percentages
        if (fraction := _fraction_value(f"{value}%")) is not None
    )
    retained_percentages = re.findall(
        r"(?:保留|剩余|留下|留)[^\d%\uff05]{0,12}(\d+(?:\.\d+)?)\s*[%\uff05]",
        combined_text,
    )
    explicit_close_fractions.extend(
        1.0 - retained
        for value in retained_percentages
        if (retained := _fraction_value(f"{value}%")) is not None
    )
    if explicit_close_fractions:
        first = explicit_close_fractions[0]
        if any(abs(value - first) > 1e-9 for value in explicit_close_fractions[1:]):
            raise ValueError("management_fraction_ambiguous")
        return first
    if any(term in combined_text for term in ("一半", "半仓", "half")):
        return 0.5
    return None


def _fraction_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    is_percent = text.endswith(("%", "％"))
    if is_percent:
        text = text[:-1].strip()
    try:
        numeric = float(text)
    except (TypeError, ValueError):
        return None
    if is_percent or numeric > 1:
        numeric /= 100
    if 0 < numeric <= 1:
        return numeric
    return None


def _exit_decision_looks_like_management_update(
    text: str | None,
    decision: dict[str, Any],
) -> bool:
    management_action = str(decision.get("management_action") or "").strip().lower()
    if management_action in FULL_EXIT_ACTIONS:
        return False
    event_type = str(decision.get("event_type") or "").strip().lower()
    if event_type in {"exit_full", "full_exit", "close_position"}:
        return False
    instruction_text = " ".join((str(text or ""), management_action)).lower()
    if _has_full_exit_instruction(str(text or "").lower()):
        return False
    return _has_partial_take_profit_terms(
        instruction_text
    ) or _has_protective_stop_terms(instruction_text)


def _authoritative_current_message_text(
    raw_text: str | None,
    payload: Mapping[str, Any],
) -> str:
    input_reading = payload.get("input_reading")
    input_reading = input_reading if isinstance(input_reading, Mapping) else {}
    observed_text = str(input_reading.get("observed_text") or "").strip()
    parts = [str(raw_text or "").strip(), observed_text]
    return "\n".join(dict.fromkeys(part for part in parts if part))


def _management_action_for_exit_downgrade(
    text: str | None,
    decision: dict[str, Any],
) -> str:
    management_action = str(decision.get("management_action") or "").strip().lower()
    instruction_text = " ".join((str(text or ""), management_action)).lower()
    has_protective_stop = _has_protective_stop_terms(instruction_text)
    if "平加仓" in instruction_text and has_protective_stop:
        return "partial_take_profit, move_stop_to_protect"
    if has_protective_stop:
        return "move_stop_to_protect"
    if _has_partial_take_profit_terms(instruction_text):
        return "partial_take_profit"
    return "position_update"


def _has_full_exit_instruction(text: str) -> bool:
    full_exit_terms = [
        "平仓",
        "全平",
        "全部平",
        "全出",
        "全部出",
        "清仓",
        "离场",
        "临时离场",
        "先出来",
        "先出",
        "出局",
        "保本出局",
        "止盈出局",
        "止损出局",
        "全部止盈",
        "止盈了",
        "止损了",
        "close position",
        "exit position",
        "breakeven exit",
    ]
    return any(term in text for term in full_exit_terms)


def _has_partial_take_profit_terms(text: str) -> bool:
    partial_terms = [
        "第一止盈",
        "第一个止盈",
        "首个止盈",
        "止盈位",
        "止盈一部分",
        "部分止盈",
        "分批止盈",
        "提前止盈",
        "减仓",
        "平加仓",
        "partial_take_profit",
    ]
    return any(term in text for term in partial_terms)


def _has_protective_stop_terms(text: str) -> bool:
    protect_terms = [
        "移动止损",
        "止损至成本",
        "止损到成本",
        "止损移到成本",
        "止损移动到成本",
        "回成本",
        "保护成本",
        "成本价",
        "成本保护",
        "保本保护",
        "带保护",
        "推保护",
        "上推保护",
        "保护价",
        "保护止损",
        "move_stop_to_protect",
        "breakeven",
        "break even",
    ]
    return any(term in text for term in protect_terms)


def _is_plausible_management_stop_loss(
    lifecycle: StrategyLifecycle,
    stop_loss: float,
) -> bool:
    if stop_loss <= 0:
        return False
    reference_values = [
        value
        for value in (
            lifecycle.entry_price_actual,
            lifecycle.entry_range_low,
            lifecycle.entry_range_high,
            lifecycle.stop_loss,
        )
        if value is not None and value > 0
    ]
    reference_values.extend(
        _strategy_price_values(
            lifecycle.take_profit,
            entry_low=lifecycle.entry_range_low,
            entry_high=lifecycle.entry_range_high,
            stop_loss=lifecycle.stop_loss,
        )
    )
    if not reference_values:
        return True
    reference = max(reference_values)
    return reference * 0.2 <= stop_loss <= reference * 5


def _should_move_stop_to_protect(
    *,
    current_text: str | None,
    decision: dict[str, Any],
    management_action: str,
) -> bool:
    text = " ".join(
        str(part or "")
        for part in (
            current_text,
            decision.get("reason"),
            decision.get("management_action"),
            management_action,
        )
    ).lower()
    protect_terms = [
        "带保护",
        "保护止损",
        "推保护",
        "上推保护",
        "保护价",
        "保本",
        "成本保护",
        "move_stop_to_protect",
        "breakeven",
        "break even",
    ]
    return any(term in text for term in protect_terms)


def _protective_stop_price(lifecycle: StrategyLifecycle) -> float | None:
    if lifecycle.entry_price_actual is not None:
        return lifecycle.entry_price_actual
    if lifecycle.entry_range_low is not None and lifecycle.entry_range_high is not None:
        return (lifecycle.entry_range_low + lifecycle.entry_range_high) / 2
    if lifecycle.entry_range_low is not None:
        return lifecycle.entry_range_low
    if lifecycle.entry_range_high is not None:
        return lifecycle.entry_range_high
    return None


def _extract_explicit_stop_loss_from_management_text(text: str | None) -> float | None:
    if not text:
        return None
    normalized = str(text)
    lowered = normalized.lower()
    has_stop_term = (
        any(term in normalized for term in ("止损", "损位", "保护价"))
        or "stop" in lowered
        or "sl" in lowered
    )
    has_adjust_term = any(
        term in normalized
        for term in (
            "修改",
            "改",
            "调整",
            "移",
            "推",
            "上移",
            "下移",
            "设置",
            "设",
            "放",
        )
    ) or any(term in lowered for term in ("move", "adjust", "set"))
    if not has_stop_term or not has_adjust_term:
        return None

    patterns = [
        r"(?:止损|损位|保护价|stop\s*loss|stop|sl)[^0-9]{0,20}([0-9]+(?:\.\d+)?)",
        r"([0-9]+(?:\.\d+)?)[^0-9]{0,8}(?:附近)?[^0-9]{0,12}(?:止损|损位|保护价)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _resolve_lifecycle_event_target(
    session,
    raw_message: RawMessage,
    decision: dict[str, Any],
) -> StrategyLifecycle | None:
    target_id = _int_or_none(decision.get("target_lifecycle_id"))
    if target_id is not None:
        target = session.get(StrategyLifecycle, target_id)
        if target is not None and target.chat_id == raw_message.chat_id:
            return target
        return None

    event_type = str(decision.get("event_type") or "").strip()
    status = "pending_entry" if event_type in {"entry_confirm", "cancel_entry"} else "entered"
    query = session.query(StrategyLifecycle).filter(
        StrategyLifecycle.chat_id == raw_message.chat_id,
        StrategyLifecycle.lifecycle_status == status,
    )
    symbol = str(decision.get("symbol") or "").strip().upper()
    side = str(decision.get("side") or "").strip().lower()
    if symbol:
        query = query.filter(StrategyLifecycle.symbol == symbol)
    if side in {"long", "short"}:
        query = query.filter(StrategyLifecycle.side == side)
    if raw_message.posted_at is not None:
        query = query.filter(StrategyLifecycle.signal_at <= raw_message.posted_at)
        query = query.filter(StrategyLifecycle.signal_at >= raw_message.posted_at - timedelta(hours=48))
    matches = query.order_by(
        StrategyLifecycle.signal_at.desc(),
        StrategyLifecycle.id.desc(),
    ).all()
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_reply_lifecycle_target(
    session, raw_message: RawMessage
) -> StrategyLifecycle | None:
    if raw_message.reply_to_message_id is None:
        return None
    return (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
        .filter(StrategyLifecycle.message_id == raw_message.reply_to_message_id)
        .one_or_none()
    )


def _record_reply_cancel_manual_review(
    session,
    *,
    raw_message: RawMessage,
    lifecycle: StrategyLifecycle,
    reason: str,
) -> None:
    binding = (
        session.get(ExecutionBinding, lifecycle.execution_binding_id)
        if lifecycle.execution_binding_id is not None
        else None
    )
    lifecycle.management_signal_message_id = raw_message.message_id
    lifecycle.management_action = "cancel_entry_after_entry_review"
    lifecycle.management_note = (
        "Reply cancellation could not safely cancel an entered strategy; "
        "manual review is required."
    )
    lifecycle.updated_at = utc_now()
    session.add(
        ExecutionEvent(
            execution_binding_id=lifecycle.execution_binding_id,
            strategy_instance_id=(binding.strategy_instance_id if binding is not None else None),
            venue="deepcoin",
            action="reply_cancel_after_entry",
            status="blocked",
            kol_id=binding.kol_id if binding is not None else None,
            chat_id=lifecycle.chat_id,
            message_id=lifecycle.message_id,
            source_message_id=raw_message.message_id,
            symbol=lifecycle.symbol,
            side=lifecycle.side,
            pos_id=binding.pos_id if binding is not None else None,
            reason=reason,
        )
    )


def _expand_lifecycle_event_targets(
    decision: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Normalize an optional explicit multi-target lifecycle decision.

    Multiple targets are safe only when every target supplies its own immutable
    lifecycle identity.  A legacy single-target decision remains unchanged.
    """

    raw_targets = decision.get("targets")
    if raw_targets is None:
        return [decision]
    if raw_targets == [] and _int_or_none(decision.get("target_lifecycle_id")) is not None:
        return [decision]
    if not isinstance(raw_targets, list) or not raw_targets:
        return None

    targets: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    base = {
        key: value for key, value in decision.items() if key != "targets"
    }
    base["_explicit_multi_target"] = True
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            return None
        if set(raw_target) - {"target_lifecycle_id", "symbol", "side"}:
            return None
        target_id = _int_or_none(raw_target.get("target_lifecycle_id"))
        if target_id is None or target_id in seen_ids:
            return None
        seen_ids.add(target_id)
        target = dict(base)
        target.update(raw_target)
        target["target_lifecycle_id"] = target_id
        targets.append(target)
    return targets


def _validate_explicit_management_targets_in_session(
    session,
    *,
    raw_message: RawMessage,
    target_decisions: list[dict[str, Any]],
    instruction_text: str,
) -> bool:
    """Validate explicit targets against the shared closed fanout policy."""

    if len(target_decisions) < 2:
        return False
    for target_decision in target_decisions:
        event_type = str(target_decision.get("event_type") or "").strip()
        raw_action = str(
            target_decision.get("management_action") or ""
        ).strip()
        effective_action = raw_action or {
            "cancel_entry": "cancel_pending_entry",
            "exit_position": "exit_full",
            "exit_full": "exit_full",
            "full_exit": "exit_full",
            "close_position": "exit_full",
        }.get(event_type, "")
        policy = multi_target_action_policy(effective_action)
        if not policy.risk_reducing or not policy.fanout_allowed:
            return False
        try:
            directive = resolve_management_directive(
                text=instruction_text,
                lifecycle_event=target_decision,
            )
            target_id = _int_or_none(
                target_decision.get("target_lifecycle_id")
            )
            if (
                target_id is None
                or not directive.risk_reducing
                or (
                    policy.requires_fraction
                    and directive.fraction is None
                )
            ):
                return False
            resolved = resolve_management_scope_in_session(
                session,
                raw_message=raw_message,
                directive=directive,
                explicit_target_lifecycle_id=target_id,
                reply_target_lifecycle_id=None,
            )
        except (ManagementScopeError, ValueError):
            return False
        if len(resolved) != 1 or resolved[0].scope_source != "explicit":
            return False
        target = resolved[0]
        if (
            target.lifecycle_id != target_id
            or str(target_decision.get("symbol") or "").upper()
            != target.symbol
            or str(target_decision.get("side") or "").lower()
            != target.side
        ):
            return False
    return True


@dataclass(frozen=True, slots=True)
class TargetAdmission:
    decision: dict[str, Any]
    accepted: bool
    reason_code: str | None
    collision_group: str | None


def _effective_multi_target_action(decision: Mapping[str, Any]) -> str:
    action = str(decision.get("management_action") or "").strip().lower()
    if action:
        return multi_target_action_policy(action).action
    return {
        "cancel_entry": "cancel_pending_entry",
        "exit_position": "exit_full",
        "exit_full": "exit_full",
        "full_exit": "exit_full",
        "close_position": "exit_full",
    }.get(str(decision.get("event_type") or "").strip().lower(), "")


def _multi_target_action_is_live(
    decision: Mapping[str, Any],
    config: MultiTargetManagementConfig | None,
) -> bool:
    return config is not None and config.action_is_live(
        _effective_multi_target_action(decision)
    )


def _admit_explicit_management_targets_in_session(
    session,
    *,
    raw_message: RawMessage,
    target_decisions: list[dict[str, Any]],
    instruction_text: str,
) -> list[TargetAdmission]:
    admissions: list[TargetAdmission] = []
    for target_decision in target_decisions:
        try:
            with session.begin_nested():
                admission = _admit_one_explicit_management_target_in_session(
                    session,
                    raw_message=raw_message,
                    target_decision=target_decision,
                    instruction_text=instruction_text,
                )
                _persist_target_admission_in_session(
                    session,
                    raw_message_id=raw_message.id,
                    target_decision=target_decision,
                    admission=admission,
                )
            admissions.append(admission)
        except (ManagementScopeError, ValueError) as exc:
            reason_code = str(exc).strip() or "target_validation_failed"
            admission = TargetAdmission(
                decision=target_decision,
                accepted=False,
                reason_code=reason_code[:128],
                collision_group=None,
            )
            with session.begin_nested():
                _persist_target_admission_in_session(
                    session,
                    raw_message_id=raw_message.id,
                    target_decision=target_decision,
                    admission=admission,
                )
            admissions.append(admission)
    return admissions


def _admit_one_explicit_management_target_in_session(
    session,
    *,
    raw_message: RawMessage,
    target_decision: dict[str, Any],
    instruction_text: str,
) -> TargetAdmission:
    action = _effective_multi_target_action(target_decision)
    policy = multi_target_action_policy(action)
    if not policy.risk_reducing or not policy.fanout_allowed:
        raise ValueError("multi_target_action_not_allowed")
    directive = resolve_management_directive(
        text=instruction_text,
        lifecycle_event=target_decision,
    )
    target_id = _int_or_none(target_decision.get("target_lifecycle_id"))
    if target_id is None:
        raise ValueError("target_lifecycle_id_missing")
    if not directive.risk_reducing:
        raise ValueError(directive.reason_code)
    if policy.requires_fraction and directive.fraction is None:
        raise ValueError("management_fraction_required")
    resolved = resolve_management_scope_in_session(
        session,
        raw_message=raw_message,
        directive=directive,
        explicit_target_lifecycle_id=target_id,
        reply_target_lifecycle_id=None,
    )
    if len(resolved) != 1:
        raise ValueError("target_not_unique")
    target = resolved[0]
    if target.scope_source != "explicit":
        raise ValueError("target_not_verified")
    if (
        target.lifecycle_id != target_id
        or str(target_decision.get("symbol") or "").upper() != target.symbol
        or str(target_decision.get("side") or "").lower() != target.side
    ):
        raise ValueError("target_identity_mismatch")
    return TargetAdmission(
        decision=target_decision,
        accepted=True,
        reason_code=None,
        collision_group=None,
    )


def _persist_target_admission_in_session(
    session,
    *,
    raw_message_id: int,
    target_decision: Mapping[str, Any],
    admission: TargetAdmission,
) -> None:
    target_id = _int_or_none(target_decision.get("target_lifecycle_id"))
    if target_id is None:
        return
    row = (
        session.query(ManagementMessageTarget)
        .filter(
            ManagementMessageTarget.raw_message_id == int(raw_message_id),
            ManagementMessageTarget.target_lifecycle_id == target_id,
            ManagementMessageTarget.normalized_action
            == _effective_multi_target_action(target_decision),
            ManagementMessageTarget.parameter_fingerprint
            == management_parameter_fingerprint(target_decision),
        )
        .one_or_none()
    )
    if row is None:
        raise ValueError("target_projection_missing")
    row.admission_state = "admitted" if admission.accepted else "refused"
    row.closed_reason_code = admission.reason_code
    row.admitted_at = utc_now() if admission.accepted else None
    row.updated_at = utc_now()
    session.flush()


def _apply_low_confidence_group_exit_if_matched(
    session,
    raw_message: RawMessage,
    *,
    parse_source: str,
    authoritative_generation: str | None,
    applied_candidate_ids: set[int] | None,
) -> bool:
    """Apply the operator-approved cautious-exit policy without broad matching."""

    scope = _low_confidence_group_exit_scope(raw_message.text or "")
    if (
        scope is None
        or not _low_confidence_group_exit_is_enabled(session, raw_message)
        or _looks_like_trading_education_content(raw_message.text or "")
    ):
        return False
    side, symbols = scope
    query = session.query(StrategyLifecycle).filter(
        StrategyLifecycle.chat_id == raw_message.chat_id,
        StrategyLifecycle.lifecycle_status.in_(["entered", "holding"]),
        StrategyLifecycle.side == side,
        StrategyLifecycle.symbol.in_(sorted(symbols or {"BTC", "ETH"})),
    )
    if raw_message.posted_at is not None:
        query = query.filter(StrategyLifecycle.signal_at <= raw_message.posted_at)
    targets = [
        lifecycle
        for lifecycle in query.order_by(StrategyLifecycle.id.asc()).all()
        if _lifecycle_has_live_execution_binding(session, lifecycle)
    ]
    applied = False
    for lifecycle in targets:
        applied = _apply_lifecycle_event_decision(
            session,
            raw_message,
            {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle.id,
                "symbol": lifecycle.symbol,
                "side": lifecycle.side,
                "management_action": "partial_take_profit",
                "management_fraction": 0.5,
                "confidence": 0.85,
                "reason": "低信心可选离场：每条已验证仓位腿部分平仓 50%。",
                "_explicit_multi_target": len(targets) > 1,
            },
            parse_source=parse_source,
            authoritative_generation=LOW_CONFIDENCE_GROUP_EXIT_GENERATION,
            applied_candidate_ids=applied_candidate_ids,
        ) or applied
    return applied


def _apply_deterministic_management_scope_if_matched(
    session,
    raw_message: RawMessage,
    decision: dict[str, Any],
    *,
    parse_source: str,
    authoritative_generation: str | None,
    applied_candidate_ids: set[int] | None,
    current_message_text: str | None = None,
    multi_target_management_config: MultiTargetManagementConfig | None = None,
) -> bool:
    """Fan out only deterministic risk reductions to verified live positions."""

    instruction_text = (
        raw_message.text or ""
        if current_message_text is None
        else current_message_text
    )

    try:
        confidence = float(decision.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if (
        confidence < 0.7
        and not _is_authorized_exact_context_risk_reduction(decision, confidence)
    ) or _looks_like_trading_education_content(
        raw_message.text or ""
    ):
        return False

    expanded_targets = _expand_lifecycle_event_targets(decision)
    if expanded_targets is None:
        return False
    if len(expanded_targets) != 1 or expanded_targets[0] is not decision:
        if _multi_target_action_is_live(
            decision,
            multi_target_management_config,
        ):
            admissions = _admit_explicit_management_targets_in_session(
                session,
                raw_message=raw_message,
                target_decisions=expanded_targets,
                instruction_text=instruction_text,
            )
            expanded_targets = [
                admission.decision
                for admission in admissions
                if admission.accepted
            ]
        else:
            if _effective_multi_target_action(decision) != "partial_take_profit":
                return False
            if not _validate_explicit_management_targets_in_session(
                session,
                raw_message=raw_message,
                target_decisions=expanded_targets,
                instruction_text=instruction_text,
            ):
                return False
        applied = False
        for target_decision in expanded_targets:
            applied = _apply_deterministic_management_scope_if_matched(
                session,
                raw_message,
                target_decision,
                parse_source=parse_source,
                authoritative_generation=authoritative_generation,
                applied_candidate_ids=applied_candidate_ids,
                current_message_text=instruction_text,
                multi_target_management_config=multi_target_management_config,
            ) or applied
        return applied

    scoped_decision = dict(decision)
    if str(scoped_decision.get("event_type") or "") == "exit_position" and (
        _exit_decision_looks_like_management_update(
            instruction_text,
            scoped_decision,
        )
    ):
        scoped_decision["event_type"] = "position_update"
        if not str(scoped_decision.get("management_action") or "").strip():
            scoped_decision["management_action"] = (
                _management_action_for_exit_downgrade(
                    instruction_text,
                    scoped_decision,
                )
            )
        if (
            not scoped_decision.get("take_profit")
            and scoped_decision.get("exit_price")
        ):
            scoped_decision["take_profit"] = scoped_decision.get("exit_price")
        scoped_decision["exit_price"] = None
    if not scoped_decision.get("stop_loss"):
        explicit_stop_loss = _extract_explicit_stop_loss_from_management_text(
            instruction_text
        )
        if explicit_stop_loss is not None:
            scoped_decision["stop_loss"] = explicit_stop_loss
    raw_management_action = str(
        scoped_decision.get("management_action") or ""
    ).strip()
    if (
        not scoped_decision.get("stop_loss")
        and raw_management_action
        in {"adjust_stop_loss", "adjust_position_tpsl", "risk_update"}
        and _should_move_stop_to_protect(
            current_text=instruction_text,
            decision=scoped_decision,
            management_action=raw_management_action,
        )
    ):
        scoped_decision["management_action"] = "move_stop_to_break_even"

    try:
        directive = resolve_management_directive(
            text=instruction_text,
            lifecycle_event=scoped_decision,
        )
    except ValueError as exc:
        if str(exc) != "management_fraction_ambiguous":
            raise
        return False
    reply_target = _resolve_reply_lifecycle_target(session, raw_message)
    explicit_target_lifecycle_id = _int_or_none(
        scoped_decision.get("target_lifecycle_id")
    )
    if (
        _is_authorized_exact_context_risk_reduction(
            scoped_decision, confidence
        )
        and reply_target is not None
        and explicit_target_lifecycle_id is not None
        and int(reply_target.id) != explicit_target_lifecycle_id
    ):
        return False
    try:
        targets = resolve_management_scope_in_session(
            session,
            raw_message=raw_message,
            directive=directive,
            explicit_target_lifecycle_id=explicit_target_lifecycle_id,
            reply_target_lifecycle_id=(
                int(reply_target.id) if reply_target is not None else None
            ),
        )
    except ManagementScopeError:
        return False

    applied = False
    for target in targets:
        target_decision = dict(scoped_decision)
        if directive.intent in {
            "adjust_stop_loss",
            "move_stop_to_break_even",
            "partial_then_break_even",
        }:
            target_decision["stop_loss"] = directive.stop_loss
            target_decision["stop_price_source"] = directive.stop_price_source
        target_decision.update(
            {
                "target_lifecycle_id": target.lifecycle_id,
                "symbol": target.symbol,
                "side": target.side,
                "_explicit_multi_target": len(targets) > 1,
                "management_scope_source": target.scope_source,
            }
        )
        if directive.intent == "cancel_entry":
            target_decision["event_type"] = "cancel_entry"
        elif directive.intent == "full_exit":
            target_decision["event_type"] = "exit_position"
            target_decision["management_action"] = "full_exit"
        else:
            target_decision["event_type"] = "position_update"
            target_decision["management_action"] = directive.intent
            target_decision["management_fraction"] = directive.fraction
        applied = _apply_lifecycle_event_decision(
            session,
            raw_message,
            target_decision,
            parse_source=parse_source,
            authoritative_generation=authoritative_generation,
            applied_candidate_ids=applied_candidate_ids,
            current_message_text=instruction_text,
        ) or applied
    return applied


def _low_confidence_group_exit_is_enabled(session, raw_message: RawMessage) -> bool:
    row = session.query(TradingSetting).filter(
        TradingSetting.key == LOW_CONFIDENCE_GROUP_EXIT_CUTOFF_KEY
    ).one_or_none()
    if row is None:
        return False
    try:
        payload = json.loads(row.value_json)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    cutoff = _int_or_none(payload.get("min_raw_message_id"))
    return cutoff is not None and raw_message.id > cutoff


def _low_confidence_group_exit_scope(text: str) -> tuple[str, set[str] | None] | None:
    """Return explicit side and BTC/ETH scope for cautious optional exits."""

    normalized = " ".join(str(text or "").split())
    side = _extract_exit_side(normalized)
    if side is None:
        return None
    has_exit = any(
        term in normalized.lower()
        for term in ("平仓", "离场", "出局", "平加仓", "可走", "先走", "close", "exit")
    )
    has_caution = any(term in normalized for term in ("求稳", "可以先", "解套", "有把握", "小亏"))
    if not has_exit or not has_caution:
        return None
    symbols = {
        symbol
        for symbol in {"BTC", "ETH"}
        if _text_mentions_exit_symbol(normalized, symbol)
    }
    return side, symbols or None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    return float(match.group(0))


def _media_missing_reason(media_assets: list[MediaAsset]) -> str | None:
    """Return a human-readable reason when no usable media file is available.

    Distinguishes between "never downloaded" (local_path is NULL for every
    asset) and "file missing from disk" (path stored but file gone).
    """
    if not media_assets:
        return None
    any_has_path = any(a.local_path for a in media_assets)
    any_has_file = any(_media_asset_to_data_url(a) for a in media_assets)
    if any_has_file:
        return None  # at least one file is usable
    if not any_has_path:
        return (
            "图片从未下载到本地（local_path 为空）。"
            "请点击「立即刷新」按钮重新同步该群消息，"
            "等待图片下载完成后再点「立即识别」。"
        )
    return (
        "图片文件丢失（磁盘上找不到）。"
        "请点击「立即刷新」按钮重新同步该群消息后再试。"
    )


def _media_asset_to_data_url(media_asset: MediaAsset) -> str | None:
    if not media_asset.local_path:
        return None
    path = Path(media_asset.local_path)
    # Relative paths (from live-listener / sync downloads) need to be
    # resolved against data/media.  Some are bare like "-100…\13121.jpg",
    # others already include the prefix like "data/media/-100…\3099.jpg".
    if not path.is_absolute():
        path_str = str(path).replace("\\", "/")
        if not path_str.startswith("data/media/"):
            path = Path("data/media") / path
    if not path.exists() or not path.is_file():
        return None
    mime_type = media_asset.mime_type or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_ai_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _parse_ai_result_json(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise ValueError("AI response did not contain JSON")
        return json.loads(match.group(0))


def _repair_ai_strategy_entry_from_text(payload: dict[str, Any], source_text: str) -> None:
    """Preserve explicit entry prices when AI only returns market entry."""
    if not isinstance(payload, dict):
        return
    if str(payload.get("recognition_result") or "").strip() != "\u662f\u7b56\u7565":
        return
    strategy = payload.get("strategy")
    if not isinstance(strategy, dict):
        return

    entry = _normalize_strategy_text(strategy.get("entry"), separator="-")
    if entry and not _is_market_only_entry(entry):
        return

    explicit_entry = _extract_labeled_entry_text(source_text)
    if not explicit_entry:
        return
    if entry and explicit_entry in entry:
        return
    strategy["entry"] = f"{entry}/{explicit_entry}" if entry else explicit_entry


def _is_market_only_entry(entry: str) -> bool:
    if re.search(r"\d", entry):
        return False
    lowered = entry.lower()
    market_terms = [
        "market",
        "\u5e02\u4ef7",
        "\u73b0\u4ef7",
        "\u73b0\u4ef7\u8fdb\u573a",
        "\u5e02\u4ef7\u8fdb\u573a",
    ]
    return any(term in lowered or term in entry for term in market_terms)


def _extract_labeled_entry_text(text: str) -> str | None:
    if not text:
        return None
    label_pattern = (
        r"(?:entry|entries|"
        r"\u8fdb\u573a|\u5165\u573a|\u5efa\u4ed3|\u5f00\u4ed3)"
        r"[^\n\d]{0,12}"
        r"(?:\u70b9\u4f4d|\u4ef7\u683c|\u4ef7|price|area|range)?"
    )
    price_pattern = (
        r"(\d+(?:\.\d+)?"
        r"(?:\s*[-~/]\s*\d+(?:\.\d+)?)*"
        r"(?:\s*(?:\u9644\u8fd1|\u5de6\u53f3|\u4e00\u7ebf|nearby|around))?)"
    )
    for line in text.splitlines():
        match = re.search(
            rf"{label_pattern}\s*[:\uff1a\-]?\s*{price_pattern}",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            return _normalize_strategy_text(match.group(1), separator="/")
    return None


def _result_from_ai_payload(
    *,
    raw_message_id: int,
    payload: dict[str, Any],
    parse_source: str,
) -> MessageRecognitionResult:
    status = str(payload.get("recognition_result") or "识别失败").strip()
    if status not in {"是策略", "非策略", "识别失败"}:
        status = "识别失败"
    strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
    summary = _format_ai_strategy_summary(strategy) if status == "是策略" else None
    result = MessageRecognitionResult(
        raw_message_id=raw_message_id,
        status=status,
        summary=summary,
        reason=str(payload.get("reason") or "").strip() or None,
        ai_payload=payload,
        parse_source=parse_source,
    )
    return result


def infer_deepseek_auxiliary(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    config: AiRecognitionConfig,
    context_text: str = "",
) -> dict[str, Any] | None:
    """Run text-only DeepSeek assessment without persisting or mutating state."""

    if not config.text_provider.is_configured:
        return None
    seed_default_prompt_registry(session_factory, config)
    composition = compose_trading_prompt(
        session_factory,
        model_kind="deepseek",
        context=context_text,
    )
    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        if raw_message is None:
            raise LookupError("raw message not found")
        if not (raw_message.text or "").strip():
            return None
        user_content = "\n\n".join(
            part
            for part in (
                json.dumps(
                    {
                        "current_message": {
                            "chat_id": raw_message.chat_id,
                            "message_id": raw_message.message_id,
                            "posted_at": (
                                str(raw_message.posted_at)
                                if raw_message.posted_at is not None
                                else None
                            ),
                            "sender": raw_message.sender_name,
                            "text": raw_message.text or "",
                        }
                    },
                    ensure_ascii=False,
                ),
                composition.context.strip(),
            )
            if part
        )
        error_message: str | None = None
        try:
            request_payload = _build_ai_recognition_payload(
                raw_message=raw_message,
                media_assets=[],
                prompt=composition.system_prompt,
                model=config.text_provider.model,
            )
            request_payload["messages"][1]["content"] = user_content
            headers = {"Content-Type": "application/json"}
            if config.text_provider.api_key:
                headers["Authorization"] = f"Bearer {config.text_provider.api_key}"
            with httpx.Client(timeout=config.text_provider.timeout_seconds) as client:
                response = client.post(
                    _chat_completions_url(config.text_provider.base_url),
                    json=request_payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
            payload = _parse_ai_result_json(_extract_ai_content(data))
            if not isinstance(payload.get("strategy"), dict):
                payload["strategy"] = {}
            if not isinstance(payload.get("lifecycle_event"), dict):
                payload["lifecycle_event"] = {
                    "event_type": "none",
                    "confidence": 0.0,
                }
        except Exception as exc:
            error_message = str(exc)
            payload = {
                "recognition_result": "识别失败",
                "reason": f"DeepSeek auxiliary validation failed: {exc}",
                "strategy": {},
                "lifecycle_event": {"event_type": "none", "confidence": 0.0},
                "confidence": 0.0,
            }
        record_prompt_invocation(
            session_factory,
            PromptInvocationRecord(
                feature="message_recognition",
                correlation_key=f"recognition:{raw_message_id}:deepseek",
                raw_message_id=raw_message_id,
                chat_id=raw_message.chat_id,
                model=config.text_provider.model,
                prompt_versions=composition.version_map,
                status="failed" if error_message else "completed",
                error_message=error_message,
            ),
        )
        payload["_prompt_versions"] = composition.version_map
        return payload


def apply_authoritative_mimo_payload(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    payload: dict[str, Any],
    model: str,
    error_message: str | None = None,
    authoritative_generation: str | None = None,
    _exact_context_risk_reduction_authorized: bool = False,
    multi_target_management_config: MultiTargetManagementConfig | None = None,
) -> MessageRecognitionResult:
    """Persist only the authoritative MiMo interpretation."""

    active_multi_target_management_config = (
        multi_target_management_config
        if multi_target_management_config is not None
        else load_multi_target_management_config()
    )
    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        if raw_message is None:
            raise LookupError("raw message not found")
        # Supersede only unlinked rows. Any candidate referenced by a durable
        # instruction remains an immutable execution snapshot and is retired
        # through its item when the new authority no longer accepts it.
        item_linked_candidate_ids = (
            session.query(MessageInstructionItem.signal_candidate_id)
            .filter(MessageInstructionItem.raw_message_id == raw_message_id)
        )
        (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == raw_message_id)
            .filter(SignalCandidate.parse_source == "mimo_authoritative")
            .filter(~SignalCandidate.id.in_(item_linked_candidate_ids))
            .update({SignalCandidate.parse_source: "mimo_superseded"})
        )
        accepted_candidate_ids: set[int] = set()
        if error_message or not payload:
            result = MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="识别失败",
                reason=error_message or "MiMo authoritative recognition failed",
                ai_payload=payload,
                parse_source="mimo_authoritative",
            )
            _upsert_recognition(session, result, engine=model)
            _project_authoritative_instruction_items(
                session,
                raw_message_id=raw_message_id,
                accepted_candidate_ids=accepted_candidate_ids,
            )
            session.commit()
            return result

        lifecycle_event = (
            dict(payload["lifecycle_event"])
            if isinstance(payload.get("lifecycle_event"), dict)
            else {"event_type": "none", "confidence": 0.0}
        )
        lifecycle_event.pop("_exact_context_risk_reduction_authorized", None)
        if _exact_context_risk_reduction_authorized:
            lifecycle_event["_exact_context_risk_reduction_authorized"] = True
        payload = dict(payload)
        payload["lifecycle_event"] = lifecycle_event
        current_message_text = _authoritative_current_message_text(
            raw_message.text,
            payload,
        )
        _project_multi_target_management_shadow_in_session(
            session,
            raw_message_id=raw_message_id,
            lifecycle_event=lifecycle_event,
            authoritative_generation=authoritative_generation,
            config=active_multi_target_management_config,
        )
        event_type = str(lifecycle_event.get("event_type") or "none")
        lifecycle_applied = _apply_low_confidence_group_exit_if_matched(
            session,
            raw_message,
            parse_source="low_confidence_group_exit",
            authoritative_generation=authoritative_generation,
            applied_candidate_ids=accepted_candidate_ids,
        )
        if not lifecycle_applied and event_type != "none":
            management_event = event_type in {
                "cancel_entry",
                "exit_position",
                "exit_full",
                "full_exit",
                "close_position",
                "position_update",
            }
            if management_event:
                lifecycle_applied = (
                    _apply_deterministic_management_scope_if_matched(
                        session,
                        raw_message,
                        lifecycle_event,
                        parse_source="mimo_authoritative",
                        authoritative_generation=authoritative_generation,
                        applied_candidate_ids=accepted_candidate_ids,
                        current_message_text=current_message_text,
                        multi_target_management_config=(
                            active_multi_target_management_config
                        ),
                    )
                )
            if not lifecycle_applied and not management_event:
                lifecycle_applied = _apply_lifecycle_event_decision(
                    session,
                    raw_message,
                    lifecycle_event,
                    parse_source="mimo_authoritative",
                    authoritative_generation=authoritative_generation,
                    applied_candidate_ids=accepted_candidate_ids,
                    current_message_text=current_message_text,
                    multi_target_management_config=(
                        active_multi_target_management_config
                    ),
                )

        lifecycle_event.pop("_exact_context_risk_reduction_authorized", None)

        result = _result_from_ai_payload(
            raw_message_id=raw_message_id,
            payload=payload,
            parse_source="mimo_authoritative",
        )
        entry_applied = False
        if result.status == "是策略":
            conflict = _detect_strategy_symbol_price_scale_conflict(
                payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {},
                raw_message.text or "",
            )
            if conflict is not None:
                review_strategy = dict(payload.get("strategy") or {})
                review_strategy["symbol"] = conflict.suggested_symbol
                review_candidate = _upsert_ai_signal_candidate(
                    session,
                    raw_message,
                    strategy=review_strategy,
                    confidence=min(float(payload.get("confidence") or 0.0), 0.69),
                    parse_source="mimo_symbol_review",
                )
                review_candidate.review_status = "needs_review"
                review_candidate.review_note = conflict.reason
                result = MessageRecognitionResult(
                    raw_message_id=raw_message_id,
                    status="非策略" if lifecycle_applied else "识别失败",
                    reason=conflict.reason,
                    ai_payload=payload,
                    parse_source="mimo_authoritative",
                )
                _upsert_recognition(session, result, engine=model)
            else:
                entry_candidate = _persist_ai_result(
                    session,
                    raw_message,
                    result,
                    engine=model,
                )
                if entry_candidate is not None:
                    session.flush()
                    accepted_candidate_ids.add(entry_candidate.id)
                entry_applied = True
        elif lifecycle_applied:
            result = MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status=str(payload.get("recognition_result") or "非策略"),
                reason=str(
                    lifecycle_event.get("reason") or payload.get("reason") or ""
                ).strip()
                or None,
                ai_payload=payload,
                parse_source="mimo_authoritative",
            )
            _upsert_recognition(session, result, engine=model)
        elif event_type != "none":
            result = MessageRecognitionResult(
                raw_message_id=raw_message_id,
                status="识别失败",
                reason="MiMo lifecycle event could not be applied safely",
                ai_payload=payload,
                parse_source="mimo_authoritative",
            )
            _upsert_recognition(session, result, engine=model)
        else:
            _upsert_recognition(session, result, engine=model)
        _project_authoritative_instruction_items(
            session,
            raw_message_id=raw_message_id,
            accepted_candidate_ids=accepted_candidate_ids,
        )
        _project_multi_target_management_shadow_in_session(
            session,
            raw_message_id=raw_message_id,
            lifecycle_event=lifecycle_event,
            authoritative_generation=authoritative_generation,
            config=active_multi_target_management_config,
        )
        session.commit()
        return result


def _project_multi_target_management_shadow_in_session(
    session,
    *,
    raw_message_id: int,
    lifecycle_event: Mapping[str, Any],
    authoritative_generation: str | None,
    config: MultiTargetManagementConfig,
) -> None:
    """Record an additive projection without changing authoritative work."""

    targets = lifecycle_event.get("targets")
    if (
        not config.projection_enabled
        or not isinstance(targets, list)
        or len(targets) < 2
    ):
        return
    try:
        projected_decision = dict(lifecycle_event)
        projected_decision["management_action"] = (
            _effective_multi_target_action(lifecycle_event)
        )
        project_management_targets_in_session(
            session,
            raw_message_id=raw_message_id,
            decision=projected_decision,
            decision_fingerprint=management_decision_fingerprint(
                projected_decision,
                authoritative_generation=authoritative_generation,
            ),
            projection_mode="shadow" if config.shadow_only else "live",
        )
    except Exception as exc:
        logger.warning(
            "Multi-target shadow projection failed open: raw_message_id=%s "
            "error=%s",
            int(raw_message_id),
            type(exc).__name__,
        )


def _format_ai_strategy_summary(strategy: dict[str, Any]) -> str | None:
    strategy = _normalize_ai_strategy(strategy)
    parts: list[str] = []
    symbol_side = " ".join(
        str(value)
        for value in [strategy.get("symbol"), strategy.get("side")]
        if value not in (None, "")
    )
    if symbol_side:
        parts.append(symbol_side)
    for label, key in [
        ("Entry", "entry"),
        ("SL", "stop_loss"),
        ("TP", "take_profit"),
        ("Lev", "leverage"),
        ("Type", "order_type"),
    ]:
        value = strategy.get(key)
        if value not in (None, ""):
            parts.append(f"{label} {value}")
    return "；".join(parts) if parts else None


def _project_authoritative_instruction_items(
    session,
    *,
    raw_message_id: int,
    accepted_candidate_ids: set[int],
) -> None:
    """Project only candidates accepted by the latest authoritative pass."""

    session.flush()
    projected_items = create_message_instruction_items_in_session(
        session,
        raw_message_id=raw_message_id,
        candidate_ids=accepted_candidate_ids,
    )
    accepted_items = [
        item
        for item in projected_items
        if item.signal_candidate_id in accepted_candidate_ids
    ]
    for sequence, item in enumerate(accepted_items):
        item.sequence = sequence
    obsolete_pending_items = (
        session.query(MessageInstructionItem)
        .filter(MessageInstructionItem.raw_message_id == raw_message_id)
        .filter(MessageInstructionItem.status == "pending")
        .filter(MessageInstructionItem.retired_at.is_(None))
        .filter(
            ~MessageInstructionItem.signal_candidate_id.in_(accepted_candidate_ids)
            if accepted_candidate_ids
            else True
        )
        .all()
    )
    for item in obsolete_pending_items:
        item.retired_at = utc_now()
    session.flush()


def _persist_ai_result(
    session,
    raw_message: RawMessage,
    result: MessageRecognitionResult,
    *,
    engine: str,
) -> SignalCandidate | None:
    payload = result.ai_payload or {}
    parse_source = result.parse_source or "ai"
    if result.status == "是策略":
        strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
        candidate = _upsert_entry_signal_candidate(
            session,
            raw_message,
            strategy=strategy,
            confidence=float(payload.get("confidence") or 0.0),
            parse_source=parse_source,
        )
        _ensure_lifecycle_record(session, raw_message, candidate)
    else:
        _apply_lifecycle_transition_signal_if_matched(session, raw_message, raw_message.text or "")
    _upsert_recognition(session, result, engine=engine)
    return candidate if result.status == "是策略" else None


def _upsert_entry_signal_candidate(
    session,
    raw_message: RawMessage,
    *,
    strategy: dict[str, Any],
    confidence: float,
    parse_source: str,
) -> SignalCandidate:
    normalized_strategy = _normalize_ai_strategy(strategy)
    desired = {
        "symbol": _string_or_none(normalized_strategy.get("symbol")),
        "side": _string_or_none(normalized_strategy.get("side")),
        "event_type": "entry_signal",
        "target_lifecycle_id": None,
        "management_action": None,
        "management_fraction": None,
        "entry_text": _string_or_none(normalized_strategy.get("entry")),
        "stop_loss_text": _string_or_none(normalized_strategy.get("stop_loss")),
        "take_profit_text": _string_or_none(normalized_strategy.get("take_profit")),
        "leverage_text": _string_or_none(normalized_strategy.get("leverage")),
        "confidence": max(0.0, min(confidence, 1.0)),
        "parse_source": parse_source,
    }
    candidates = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message.id)
        .filter(SignalCandidate.event_type == "entry_signal")
        .order_by(SignalCandidate.id.asc())
        .all()
    )
    candidate, editable = _candidate_for_semantic_upsert(
        session,
        candidates=candidates,
        desired=desired,
    )
    if candidate is None:
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            source_id=None,
            parse_source=parse_source,
            review_status="pending",
        )
        session.add(candidate)
        editable = True
    if editable:
        for field, value in desired.items():
            setattr(candidate, field, value)
        candidate.recognition_generation = None
    return candidate


def _candidate_for_semantic_upsert(
    session,
    *,
    candidates: list[SignalCandidate],
    desired: dict[str, Any],
    immutable_version_fields: set[str] | None = None,
) -> tuple[SignalCandidate | None, bool]:
    """Reuse immutable durable candidates only when execution semantics match."""

    linked_items = {
        item.signal_candidate_id: item
        for item in (
            session.query(MessageInstructionItem)
            .filter(
                MessageInstructionItem.signal_candidate_id.in_(
                    [candidate.id for candidate in candidates]
                )
            )
            .all()
        )
    } if candidates else {}
    ordered_candidates = sorted(
        candidates,
        key=lambda candidate: (
            linked_items.get(candidate.id) is None
            or linked_items[candidate.id].retired_at is not None,
            candidate.id,
        ),
    )
    for candidate in ordered_candidates:
        if all(getattr(candidate, field) == value for field, value in desired.items()):
            return candidate, candidate.id not in linked_items
    version_fields = immutable_version_fields or set()
    if version_fields:
        for candidate in ordered_candidates:
            item = linked_items.get(candidate.id)
            if item is None or item.status == "pending":
                continue
            if all(
                field in version_fields or getattr(candidate, field) == value
                for field, value in desired.items()
            ):
                return candidate, False
    for candidate in ordered_candidates:
        if candidate.id not in linked_items:
            return candidate, True
    return None, True


def _upsert_ai_signal_candidate(
    session,
    raw_message: RawMessage,
    *,
    strategy: dict[str, Any],
    confidence: float,
    parse_source: str,
) -> SignalCandidate:
    """Compatibility wrapper for the entry-role candidate upsert."""

    return _upsert_entry_signal_candidate(
        session,
        raw_message,
        strategy=strategy,
        confidence=confidence,
        parse_source=parse_source,
    )


def _normalize_ai_strategy(strategy: dict[str, Any]) -> dict[str, str | None]:
    return {
        "symbol": _normalize_symbol(strategy.get("symbol")),
        "side": _normalize_side(strategy.get("side")),
        "entry": _normalize_strategy_text(strategy.get("entry"), separator="-"),
        "stop_loss": _normalize_strategy_text(strategy.get("stop_loss"), separator="/"),
        "take_profit": _normalize_strategy_text(strategy.get("take_profit"), separator="/"),
        "leverage": _normalize_strategy_text(strategy.get("leverage"), separator="/"),
        "order_type": _normalize_strategy_text(strategy.get("order_type"), separator="/"),
    }


def _detect_strategy_symbol_price_scale_conflict(
    strategy: dict[str, Any],
    raw_text: str,
) -> _SymbolPriceScaleConflict | None:
    normalized = _normalize_ai_strategy(strategy)
    symbol = normalized.get("symbol")
    if symbol not in {"BTC", "ETH"}:
        return None
    field_texts = [
        value
        for value in (
            normalized.get("entry"),
            normalized.get("stop_loss"),
            normalized.get("take_profit"),
        )
        if value
    ]
    prices = _extract_strategy_price_values(field_texts)
    if len(prices) < 2:
        return None
    if symbol == "BTC":
        joined = " ".join([raw_text, *field_texts]).lower()
        if "万" in joined or re.search(r"\d(?:\.\d+)?\s*w\b", joined):
            return None
        if min(prices) >= 500 and max(prices) < 10000:
            return _SymbolPriceScaleConflict(
                original_symbol="BTC",
                suggested_symbol="ETH",
                reason=(
                    "symbol_price_scale_conflict: MiMo 输出 BTC，但入场/止损/止盈价格"
                    "整体处于 ETH 千位区间，疑似原文 BTC/ETH 笔误；已转入人工复核，禁止自动执行。"
                ),
            )
    if symbol == "ETH" and min(prices) >= 10000:
        return _SymbolPriceScaleConflict(
            original_symbol="ETH",
            suggested_symbol="BTC",
            reason=(
                "symbol_price_scale_conflict: MiMo 输出 ETH，但入场/止损/止盈价格"
                "整体处于 BTC 万位区间，疑似原文 BTC/ETH 笔误；已转入人工复核，禁止自动执行。"
            ),
        )
    return None


def _extract_strategy_price_values(field_texts: list[str]) -> list[float]:
    values: list[float] = []
    for text in field_texts:
        for match in re.finditer(r"(?<![A-Za-z])(\d+(?:\.\d+)?)(\s*[万wW])?", text):
            number = float(match.group(1))
            suffix = (match.group(2) or "").strip().lower()
            if suffix in {"万", "w"}:
                number *= 10000
            values.append(number)
    return values


def _normalize_symbol(value: Any) -> str | None:
    text = _normalize_strategy_text(value, separator="/")
    if not text:
        return None
    return text.upper().replace(" ", "")


def _normalize_side(value: Any) -> str | None:
    text = _normalize_strategy_text(value, separator="/")
    if not text:
        return None
    lowered = text.lower()
    if any(token in lowered for token in ["long", "buy"]):
        return "long"
    if any(token in lowered for token in ["short", "sell"]):
        return "short"
    if any(token in text for token in ["多", "做多", "开多"]):
        return "long"
    if any(token in text for token in ["空", "做空", "开空"]):
        return "short"
    return lowered


def _normalize_strategy_text(value: Any, *, separator: str) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (list, tuple, set)):
        parts = [
            _normalize_strategy_text(item, separator=separator)
            for item in value
        ]
        return separator.join(part for part in parts if part) or None
    if isinstance(value, dict):
        parts = [
            _normalize_strategy_text(item, separator=separator)
            for item in value.values()
        ]
        return separator.join(part for part in parts if part) or None

    text = str(value).strip()
    if not text:
        return None
    text = re.sub(
        r"^(entry|entries|sl|stop\s*loss|tp|take\s*profit|止损|止盈|入场|进场|建仓|目标)\s*[:：]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("－", "-").replace("—", "-").replace("~", "-").replace("至", "-")
    text = re.sub(r"\s+", "", text)
    if separator == "/":
        text = re.sub(r"[，,、|]+", "/", text)
        text = re.sub(r"\s*/\s*", "/", text)
    else:
        text = re.sub(r"\s*-\s*", "-", text)
    return text or None


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _is_actionable_entry_signal(text: str, parsed) -> bool:
    if parsed.confidence < 0.4:
        return False
    if parsed.event_type != "entry_signal":
        return False
    if not parsed.symbol or parsed.symbol.upper() in BLOCKED_SYMBOLS:
        return False
    if not parsed.side:
        return False
    if not _has_entry_instruction(text, parsed):
        return False
    if parsed.stop_loss is None and not parsed.take_profits:
        return False
    if _looks_like_position_management(text) and not _has_entry_instruction(text, parsed):
        return False
    return True


def _apply_bitcoin_junzhang_profile_if_matched(
    session,
    raw_message: RawMessage,
) -> MessageRecognitionResult | None:
    if int(raw_message.chat_id) != BITCOIN_JUNZHANG_CHAT_ID:
        return None
    text = _clean_bitcoin_junzhang_text(raw_message.text or "")
    if not text:
        return None

    if _apply_bitcoin_junzhang_management_if_matched(session, raw_message, text):
        return MessageRecognitionResult(
            raw_message_id=raw_message.id,
            status="非策略",
            reason="比特币军长短句 profile 识别为已有仓位管理/离场消息，已匹配生命周期。",
            parse_source=BITCOIN_JUNZHANG_PARSE_SOURCE,
        )

    parsed_entry = _parse_bitcoin_junzhang_entry(text)
    if parsed_entry is None:
        return None
    if parsed_entry["stop_loss"] is None and not parsed_entry["take_profit"]:
        return MessageRecognitionResult(
            raw_message_id=raw_message.id,
            status="非策略",
            reason="比特币军长短句 profile 命中开仓意图，但缺少止损/止盈，记录为半策略且不自动交易。",
            parse_source=BITCOIN_JUNZHANG_PARSE_SOURCE,
        )

    candidate = _upsert_bitcoin_junzhang_entry_candidate(
        session,
        raw_message=raw_message,
        parsed_entry=parsed_entry,
    )
    _ensure_lifecycle_record(session, raw_message, candidate)
    return MessageRecognitionResult(
        raw_message_id=raw_message.id,
        status="是策略",
        summary=_format_candidate_summary(candidate),
        parse_source=BITCOIN_JUNZHANG_PARSE_SOURCE,
    )


def _clean_bitcoin_junzhang_text(text: str) -> str:
    lines = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("---------------"):
            break
        if line.startswith("@Tarderfengge") or line.startswith("QQ:"):
            continue
        lines.append(line.strip("💰 "))
    compact = " ".join(lines).strip()
    compact = re.sub(r"@\S+", "", compact)
    compact = re.sub(r"QQ[:：]?\s*\d+", "", compact, flags=re.IGNORECASE)
    return " ".join(compact.split())


def _parse_bitcoin_junzhang_entry(text: str) -> dict[str, Any] | None:
    if not _junzhang_has_entry_intent(text):
        return None
    symbol = _extract_junzhang_symbol(text)
    side = _extract_junzhang_side(text)
    if symbol is None or side is None:
        return None
    return {
        "symbol": symbol,
        "side": side,
        "entry": _extract_junzhang_entry_text(text),
        "stop_loss": _extract_junzhang_stop_loss(text),
        "take_profit": _extract_junzhang_take_profit(text),
        "leverage": _extract_junzhang_leverage(text),
    }


def _junzhang_has_entry_intent(text: str) -> bool:
    lowered = text.lower()
    return (
        any(term in text for term in ["现价开", "现价", "开一层", "开个", "杠干"])
        or any(term in lowered for term in ["market", "open"])
    )


def _extract_junzhang_symbol(text: str) -> str | None:
    aliases = [
        ("比特", "BTC"),
        ("大饼", "BTC"),
        ("BTC", "BTC"),
        ("以太", "ETH"),
        ("ETH", "ETH"),
    ]
    for alias, symbol in aliases:
        if alias in text:
            return symbol
    match = re.search(r"(?<![A-Za-z0-9])([A-Za-z]{2,10})(?![A-Za-z0-9])", text)
    if match is None:
        return None
    symbol = match.group(1).upper()
    return None if symbol in BLOCKED_SYMBOLS else symbol


def _extract_junzhang_side(text: str) -> str | None:
    if any(term in text for term in ["空单", "开空", "做空", "杠干空", "空"]):
        return "short"
    if any(term in text for term in ["多单", "开多", "做多", "杠干多", "多"]):
        return "long"
    return None


def _extract_junzhang_entry_text(text: str) -> str | None:
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)", text)
    if range_match:
        return f"{range_match.group(1)}-{range_match.group(2)}"
    if "现价" in text or "开一层" in text or "开个" in text:
        return "现价"
    return None


def _extract_junzhang_stop_loss(text: str) -> float | None:
    match = re.search(r"止损(?:放|上移到|移到)?[:：]?\s*(\d+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1))
    return None


def _extract_junzhang_take_profit(text: str) -> str | None:
    match = re.search(r"止盈[:：]?\s*([0-9./\s-]+)", text)
    if match is None:
        return None
    values = re.findall(r"\d+(?:\.\d+)?", match.group(1))
    return "/".join(values) if values else None


def _extract_junzhang_leverage(text: str) -> str | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*倍", text)
    if match:
        return f"{match.group(1)}x"
    return None


def _upsert_bitcoin_junzhang_entry_candidate(
    session,
    *,
    raw_message: RawMessage,
    parsed_entry: dict[str, Any],
) -> SignalCandidate:
    candidate = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message.id)
        .order_by(SignalCandidate.id.asc())
        .first()
    )
    if candidate is None:
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            source_id=None,
            parse_source=BITCOIN_JUNZHANG_PARSE_SOURCE,
            review_status="pending",
        )
        session.add(candidate)
    candidate.symbol = parsed_entry["symbol"]
    candidate.side = parsed_entry["side"]
    candidate.event_type = "entry_signal"
    candidate.entry_text = parsed_entry["entry"]
    candidate.stop_loss_text = _format_number(parsed_entry["stop_loss"])
    candidate.take_profit_text = parsed_entry["take_profit"]
    candidate.leverage_text = parsed_entry["leverage"]
    candidate.confidence = 0.88
    candidate.parse_source = BITCOIN_JUNZHANG_PARSE_SOURCE
    return candidate


def _apply_bitcoin_junzhang_management_if_matched(
    session,
    raw_message: RawMessage,
    text: str,
) -> bool:
    if "挂单取消" in text or "取消挂单" in text:
        return _apply_bitcoin_junzhang_cancel_if_matched(session, raw_message, text)
    if _junzhang_is_take_profit_close(text):
        return _apply_bitcoin_junzhang_close_if_matched(session, raw_message, text)
    if "止损上移到开仓价" in text or "止损移到开仓价" in text:
        return _apply_bitcoin_junzhang_stop_update_if_matched(
            session,
            raw_message,
            text,
            move_to_entry=True,
        )
    if re.search(r"止损(?:放|移到|上移到)?[:：]?\s*\d+(?:\.\d+)?", text):
        return _apply_bitcoin_junzhang_stop_update_if_matched(
            session,
            raw_message,
            text,
            move_to_entry=False,
        )
    return False


def _junzhang_is_take_profit_close(text: str) -> bool:
    return "止盈" in text and any(term in text for term in ["掉", "了", "止盈"])


def _apply_bitcoin_junzhang_cancel_if_matched(session, raw_message: RawMessage, text: str) -> bool:
    lifecycle = _select_bitcoin_junzhang_lifecycle(
        session,
        raw_message=raw_message,
        text=text,
        statuses={"pending_entry"},
    )
    if lifecycle is None:
        return False
    lifecycle.lifecycle_status = "exited"
    lifecycle.exit_reason = "cancelled"
    lifecycle.exited_at = raw_message.posted_at or utc_now()
    lifecycle.exit_signal_message_id = raw_message.message_id
    lifecycle.updated_at = utc_now()
    _upsert_close_signal_candidate(
        session,
        raw_message=raw_message,
        lifecycle=lifecycle,
        parse_source=BITCOIN_JUNZHANG_PARSE_SOURCE,
    )
    return True


def _apply_bitcoin_junzhang_close_if_matched(session, raw_message: RawMessage, text: str) -> bool:
    lifecycle = _select_bitcoin_junzhang_lifecycle(
        session,
        raw_message=raw_message,
        text=text,
        statuses={"entered"},
    )
    if lifecycle is None:
        return False
    if _lifecycle_has_live_execution_binding(session, lifecycle):
        record_lifecycle_exit_intent(
            session,
            lifecycle,
            exit_message_id=raw_message.message_id,
            reason=text,
        )
        _upsert_close_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=lifecycle,
            parse_source=BITCOIN_JUNZHANG_PARSE_SOURCE,
        )
        return True
    lifecycle.lifecycle_status = "exited"
    lifecycle.exit_reason = "kol_signal"
    lifecycle.exited_at = raw_message.posted_at or utc_now()
    lifecycle.exit_signal_message_id = raw_message.message_id
    lifecycle.updated_at = utc_now()
    _upsert_close_signal_candidate(
        session,
        raw_message=raw_message,
        lifecycle=lifecycle,
        parse_source=BITCOIN_JUNZHANG_PARSE_SOURCE,
    )
    return True


def _apply_bitcoin_junzhang_stop_update_if_matched(
    session,
    raw_message: RawMessage,
    text: str,
    *,
    move_to_entry: bool,
) -> bool:
    lifecycle = _select_bitcoin_junzhang_lifecycle(
        session,
        raw_message=raw_message,
        text=text,
        statuses={"entered"},
    )
    if lifecycle is None:
        return False
    if move_to_entry:
        if lifecycle.entry_price_actual is None:
            return False
        stop_loss = lifecycle.entry_price_actual
        action = "move_stop_to_entry"
    else:
        stop_loss = _extract_junzhang_stop_loss(text)
        action = "risk_update"
    if stop_loss is None:
        return False
    lifecycle.stop_loss = stop_loss
    lifecycle.management_signal_message_id = raw_message.message_id
    lifecycle.management_action = action
    lifecycle.management_note = "比特币军长短句 profile 识别到止损更新。"
    lifecycle.updated_at = utc_now()
    _upsert_management_signal_candidate(
        session,
        raw_message=raw_message,
        lifecycle=lifecycle,
        parse_source=BITCOIN_JUNZHANG_PARSE_SOURCE,
    )
    return True


def _select_bitcoin_junzhang_lifecycle(
    session,
    *,
    raw_message: RawMessage,
    text: str,
    statuses: set[str],
) -> StrategyLifecycle | None:
    symbol = _extract_junzhang_symbol(text)
    side = _extract_junzhang_side(text)
    query = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
        .filter(StrategyLifecycle.lifecycle_status.in_(statuses))
    )
    if symbol is not None:
        query = query.filter(StrategyLifecycle.symbol == symbol)
    if side is not None:
        query = query.filter(StrategyLifecycle.side == side)
    if raw_message.posted_at is not None:
        query = query.filter(StrategyLifecycle.signal_at <= raw_message.posted_at)
        query = query.filter(StrategyLifecycle.signal_at >= raw_message.posted_at - timedelta(days=7))
    matches = (
        query.order_by(
            StrategyLifecycle.entered_at.desc().nullslast(),
            StrategyLifecycle.signal_at.desc(),
            StrategyLifecycle.id.desc(),
        )
        .limit(2)
        .all()
    )
    return matches[0] if len(matches) == 1 else None


def _has_entry_instruction(text: str, parsed) -> bool:
    if parsed.entry_range is not None:
        return True
    lowered = text.lower()
    return any(term in text or term in lowered for term in ENTRY_TERMS)


def _looks_like_position_management(text: str) -> bool:
    return any(term in text for term in POSITION_MANAGEMENT_TERMS)


def _caption_looks_like_ocr_recap_reference(caption: str) -> bool:
    caption = caption.strip()
    if not caption:
        return False
    if not any(term in caption for term in OCR_RECAP_REFERENCE_TERMS):
        return False
    parsed = parse_signal_text(caption)
    caption_has_complete_plan = parsed.entry_range is not None and (
        parsed.stop_loss is not None or bool(parsed.take_profits)
    )
    return not caption_has_complete_plan


def _apply_lifecycle_transition_signal_if_matched(
    session,
    raw_message: RawMessage,
    text: str,
) -> bool:
    if _apply_exit_signal_if_matched(session, raw_message, text):
        return True
    if _apply_entry_confirmation_signal_if_matched(session, raw_message, text):
        return True
    if _apply_pending_entry_invalidation_if_matched(session, raw_message, text):
        return True
    return False


def _apply_entry_confirmation_signal_if_matched(
    session,
    raw_message: RawMessage,
    text: str,
) -> bool:
    entry_signal = _parse_explicit_entry_confirmation_signal(text)
    if entry_signal is None:
        return False

    symbol, side, entry_price = entry_signal
    query = session.query(StrategyLifecycle).filter(
        StrategyLifecycle.chat_id == raw_message.chat_id,
        StrategyLifecycle.lifecycle_status.in_(["pending_entry", "expired"]),
    )
    if symbol is not None:
        query = query.filter(StrategyLifecycle.symbol == symbol)
    if side is not None:
        query = query.filter(StrategyLifecycle.side == side)
    if raw_message.posted_at is not None:
        query = query.filter(StrategyLifecycle.signal_at <= raw_message.posted_at)
        query = query.filter(StrategyLifecycle.signal_at >= raw_message.posted_at - timedelta(hours=24))

    matches = [
        item
        for item in query.order_by(
            StrategyLifecycle.signal_at.desc(),
            StrategyLifecycle.id.desc(),
        ).all()
        if item.lifecycle_status == "pending_entry"
        or _lifecycle_has_live_execution_binding(session, item)
    ]
    if not matches:
        return False

    latest = matches[0]
    if symbol is None and len(matches) > 1:
        same_latest_time = [
            item for item in matches if item.signal_at == latest.signal_at
        ]
        if len(same_latest_time) > 1:
            return False

    entered_at = raw_message.posted_at or utc_now()
    latest.lifecycle_status = "entered"
    latest.entered_at = entered_at
    latest.entry_signal_message_id = raw_message.message_id
    latest.exit_reason = None
    latest.exited_at = None
    if entry_price is not None:
        latest.entry_price_actual = entry_price
    latest.updated_at = utc_now()

    if latest.trade_idea_id is not None:
        trade_idea = session.get(TradeIdea, latest.trade_idea_id)
        if trade_idea is not None:
            trade_idea.status = "open"
            if trade_idea.opened_at is None:
                trade_idea.opened_at = entered_at

    _upsert_entry_confirmation_candidate(
        session,
        raw_message=raw_message,
        lifecycle=latest,
        entry_price=entry_price,
    )
    return True


def _lifecycle_has_live_execution_binding(session, lifecycle: StrategyLifecycle) -> bool:
    return has_live_execution_binding(session, lifecycle)


def _apply_exit_signal_if_matched(
    session,
    raw_message: RawMessage,
    text: str,
) -> bool:
    if _apply_cancel_signal_if_matched(session, raw_message, text):
        return True

    exit_signal = _parse_explicit_exit_signal(text)
    if exit_signal is None:
        return False

    symbol, side = exit_signal
    query = session.query(StrategyLifecycle).filter(
        StrategyLifecycle.chat_id == raw_message.chat_id,
        StrategyLifecycle.lifecycle_status == "entered",
    )
    if symbol is not None:
        query = query.filter(StrategyLifecycle.symbol == symbol)
    if side is not None:
        query = query.filter(StrategyLifecycle.side == side)
    if raw_message.posted_at is not None:
        query = query.filter(StrategyLifecycle.signal_at <= raw_message.posted_at)

    matches = query.order_by(
        StrategyLifecycle.entered_at.desc().nullslast(),
        StrategyLifecycle.signal_at.desc(),
        StrategyLifecycle.id.desc(),
    ).all()
    if not matches:
        return False
    if symbol is None and len(matches) > 1:
        return False

    lifecycle = matches[0]
    exited_at = raw_message.posted_at or utc_now()
    if _lifecycle_has_live_execution_binding(session, lifecycle):
        record_lifecycle_exit_intent(
            session,
            lifecycle,
            exit_message_id=raw_message.message_id,
            reason=text,
        )
        _upsert_close_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=lifecycle,
            parse_source="exit_heuristic",
        )
        return True
    lifecycle.lifecycle_status = "exited"
    lifecycle.exit_reason = "kol_signal"
    lifecycle.exited_at = exited_at
    if lifecycle.entered_at is not None and _datetime_after(lifecycle.entered_at, exited_at):
        lifecycle.entered_at = lifecycle.signal_at
    lifecycle.exit_signal_message_id = raw_message.message_id
    lifecycle.updated_at = utc_now()

    if lifecycle.trade_idea_id is not None:
        trade_idea = session.get(TradeIdea, lifecycle.trade_idea_id)
        if trade_idea is not None and trade_idea.status == "open":
            trade_idea.status = "closed"
            trade_idea.closed_at = exited_at

    _upsert_close_signal_candidate(
        session,
        raw_message=raw_message,
        lifecycle=lifecycle,
        parse_source="exit_heuristic",
    )
    return True


def _apply_cancel_signal_if_matched(
    session,
    raw_message: RawMessage,
    text: str,
) -> bool:
    cancel_signal = _parse_explicit_cancel_signal(text)
    if cancel_signal is None:
        return False

    symbol, side = cancel_signal
    query = session.query(StrategyLifecycle).filter(
        StrategyLifecycle.chat_id == raw_message.chat_id,
        StrategyLifecycle.lifecycle_status.in_(["pending_entry", "expired", "entered"]),
    )
    if symbol is not None:
        query = query.filter(StrategyLifecycle.symbol == symbol)
    if side is not None:
        query = query.filter(StrategyLifecycle.side == side)
    if raw_message.posted_at is not None:
        query = query.filter(StrategyLifecycle.signal_at <= raw_message.posted_at)
        query = query.filter(StrategyLifecycle.signal_at >= raw_message.posted_at - timedelta(hours=24))

    matches = [
        item
        for item in query.order_by(
            StrategyLifecycle.signal_at.desc(),
            StrategyLifecycle.id.desc(),
        ).all()
        if item.lifecycle_status == "pending_entry"
        or _lifecycle_has_live_execution_binding(session, item)
        or _lifecycle_entered_after_message(item, raw_message)
    ]
    if not matches:
        return False

    latest = matches[0]
    cancel_all_matches = _cancel_signal_applies_to_all_matches(text)
    if symbol is None and len(matches) > 1 and not cancel_all_matches:
        same_latest_time = [
            item for item in matches if item.signal_at == latest.signal_at
        ]
        if len(same_latest_time) > 1:
            return False

    exited_at = raw_message.posted_at or utc_now()
    cancelled_lifecycles = matches if cancel_all_matches else [latest]
    for lifecycle in cancelled_lifecycles:
        if _lifecycle_has_live_execution_binding(session, lifecycle):
            record_lifecycle_exit_intent(
                session,
                lifecycle,
                exit_message_id=raw_message.message_id,
                reason=text,
            )
            continue
        entered_after_cancel_message = _lifecycle_entered_after_message(lifecycle, raw_message)
        lifecycle.lifecycle_status = "exited"
        lifecycle.exit_reason = "cancelled"
        lifecycle.exited_at = exited_at
        lifecycle.exit_signal_message_id = raw_message.message_id
        if entered_after_cancel_message:
            lifecycle.entered_at = None
            lifecycle.entry_price_actual = None
        lifecycle.updated_at = utc_now()

    _upsert_close_signal_candidate(
        session,
        raw_message=raw_message,
        lifecycle=latest,
        parse_source="cancel_heuristic",
    )
    return True


def _lifecycle_entered_after_message(
    lifecycle: StrategyLifecycle,
    raw_message: RawMessage,
) -> bool:
    if lifecycle.lifecycle_status != "entered":
        return False
    event_at = raw_message.posted_at
    if event_at is None:
        return False
    return _datetime_after(lifecycle.entered_at, event_at)


def _apply_pending_entry_invalidation_if_matched(
    session,
    raw_message: RawMessage,
    text: str,
) -> bool:
    normalized = (text or "").strip()
    if not normalized or _looks_like_trading_education_content(normalized):
        return False
    if _parse_explicit_cancel_signal(normalized) is not None:
        return False

    symbol = _extract_exit_symbol(normalized)
    if symbol is None:
        return False

    lowered = normalized.lower()
    invalidation_terms = [
        "没站稳",
        "未站稳",
        "没有站稳",
        "跌破",
        "破位",
        "继续下探",
        "继续下跌",
        "继续回落",
        "压力位继续下移",
        "前方压力位继续下移",
        "入场条件失效",
        "策略失效",
        "计划失效",
        "作废",
        "放弃",
        "不做了",
        "别进",
        "不要进",
        "等后续",
    ]
    english_terms = [
        "invalidated",
        "setup invalid",
        "plan invalid",
        "no entry",
        "do not enter",
        "skip entry",
        "wait for next",
        "broke down",
    ]
    if not any(term in normalized for term in invalidation_terms) and not any(
        term in lowered for term in english_terms
    ):
        return False

    query = session.query(StrategyLifecycle).filter(
        StrategyLifecycle.chat_id == raw_message.chat_id,
        StrategyLifecycle.symbol == symbol,
        StrategyLifecycle.lifecycle_status == "pending_entry",
    )
    side = _extract_exit_side(normalized)
    if side is not None:
        query = query.filter(StrategyLifecycle.side == side)
    if raw_message.posted_at is not None:
        query = query.filter(StrategyLifecycle.signal_at <= raw_message.posted_at)
        query = query.filter(StrategyLifecycle.signal_at >= raw_message.posted_at - timedelta(hours=24))

    matches = query.order_by(
        StrategyLifecycle.signal_at.desc(),
        StrategyLifecycle.id.desc(),
    ).all()
    if not matches:
        return False

    latest = matches[0]
    invalidated_at = raw_message.posted_at or utc_now()
    latest.lifecycle_status = "invalidated"
    latest.exit_reason = "context_invalidated"
    latest.exited_at = invalidated_at
    latest.exit_signal_message_id = raw_message.message_id
    latest.updated_at = utc_now()

    if latest.trade_idea_id is not None:
        trade_idea = session.get(TradeIdea, latest.trade_idea_id)
        if trade_idea is not None and trade_idea.status == "open":
            trade_idea.status = "closed"
            trade_idea.closed_at = invalidated_at

    candidate = _upsert_close_signal_candidate(
        session,
        raw_message=raw_message,
        lifecycle=latest,
        parse_source="context_invalidation_heuristic",
    )
    candidate.event_type = "context_invalidation"
    candidate.review_note = "Pending entry invalidated by later same-symbol market context."
    return True


def _upsert_close_signal_candidate(
    session,
    *,
    raw_message: RawMessage,
    lifecycle: StrategyLifecycle,
    parse_source: str,
    management_action: str | None = None,
    management_fraction: float | None = None,
    recognition_generation: str | None = None,
    confidence: float | None = None,
) -> SignalCandidate:
    desired = {
        "symbol": lifecycle.symbol,
        "side": lifecycle.side,
        "event_type": "close_signal",
        "target_lifecycle_id": lifecycle.id,
        "management_action": management_action,
        "management_fraction": management_fraction,
        "entry_text": None,
        "stop_loss_text": None,
        "take_profit_text": None,
        "leverage_text": None,
        "parse_source": parse_source,
        "recognition_generation": recognition_generation,
    }
    candidates = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message.id)
        .filter(SignalCandidate.event_type.in_(["close_signal", "position_update"]))
        .filter(SignalCandidate.target_lifecycle_id == lifecycle.id)
        .order_by(SignalCandidate.id.asc())
        .all()
    )
    candidate, editable = _candidate_for_semantic_upsert(
        session,
        candidates=candidates,
        desired=desired,
        immutable_version_fields={"recognition_generation"},
    )
    if candidate is None:
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            source_id=None,
            parse_source=parse_source,
            review_status="pending",
        )
        session.add(candidate)
        editable = True
    if editable:
        for field, value in desired.items():
            setattr(candidate, field, value)
        candidate.confidence = (
            float(confidence)
            if confidence is not None
            else max(candidate.confidence or 0.0, 0.85)
        )
    return candidate


def _upsert_entry_confirmation_candidate(
    session,
    *,
    raw_message: RawMessage,
    lifecycle: StrategyLifecycle,
    entry_price: float | None,
    parse_source: str = "entry_confirm_heuristic",
) -> SignalCandidate:
    desired = {
        "symbol": lifecycle.symbol,
        "side": lifecycle.side,
        "event_type": "entry_signal",
        "target_lifecycle_id": None,
        "management_action": None,
        "management_fraction": None,
        "entry_text": _format_number(entry_price) if entry_price is not None else None,
        "stop_loss_text": _format_number(lifecycle.stop_loss),
        "take_profit_text": lifecycle.take_profit,
        "leverage_text": None,
        "parse_source": parse_source,
    }
    candidates = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message.id)
        .filter(SignalCandidate.event_type == "entry_signal")
        .order_by(SignalCandidate.id.asc())
        .all()
    )
    candidate, editable = _candidate_for_semantic_upsert(
        session,
        candidates=candidates,
        desired=desired,
    )
    if candidate is None:
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            source_id=None,
            parse_source=parse_source,
            review_status="pending",
        )
        session.add(candidate)
        editable = True
    if editable:
        for field, value in desired.items():
            setattr(candidate, field, value)
        candidate.recognition_generation = None
        candidate.confidence = max(candidate.confidence or 0.0, 0.85)
    return candidate


def _upsert_management_signal_candidate(
    session,
    *,
    raw_message: RawMessage,
    lifecycle: StrategyLifecycle,
    parse_source: str,
    management_action: str | None = None,
    management_fraction: float | None = None,
    recognition_generation: str | None = None,
    requested_stop_loss: float | None = None,
    stop_price_source: str | None = None,
    requested_take_profit: str | None = None,
    management_contract_json: str | None = None,
    management_contract_fingerprint: str | None = None,
) -> SignalCandidate:
    break_even_intent = str(management_action or "").lower() in {
        "move_stop_to_break_even",
        "partial_then_break_even",
    }
    desired = {
        "symbol": lifecycle.symbol,
        "side": lifecycle.side,
        "event_type": "position_update",
        "target_lifecycle_id": lifecycle.id,
        "management_action": management_action,
        "management_fraction": management_fraction,
        "entry_text": None,
        "stop_loss_text": (
            _format_number(requested_stop_loss)
            if requested_stop_loss is not None
            else None
            if break_even_intent
            else _format_number(lifecycle.stop_loss)
        ),
        "stop_price_source": (
            stop_price_source
            if requested_stop_loss is not None
            else None
        ),
        "take_profit_text": requested_take_profit or lifecycle.take_profit,
        "leverage_text": None,
        "parse_source": parse_source,
        "recognition_generation": recognition_generation,
        "management_contract_json": management_contract_json,
        "management_contract_fingerprint": management_contract_fingerprint,
    }
    candidates = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message.id)
        .filter(SignalCandidate.event_type.in_(["close_signal", "position_update"]))
        .filter(SignalCandidate.target_lifecycle_id == lifecycle.id)
        .order_by(SignalCandidate.id.asc())
        .all()
    )
    candidate, editable = _candidate_for_semantic_upsert(
        session,
        candidates=candidates,
        desired=desired,
        immutable_version_fields={"recognition_generation"},
    )
    if candidate is None:
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            source_id=None,
            parse_source=parse_source,
            review_status="pending",
        )
        session.add(candidate)
        editable = True
    if editable:
        for field, value in desired.items():
            setattr(candidate, field, value)
        candidate.confidence = max(candidate.confidence or 0.0, 0.85)
    return candidate


def _parse_explicit_exit_signal(text: str) -> tuple[str | None, str | None] | None:
    normalized = (text or "").strip()
    if not normalized:
        return None
    if _looks_like_trading_education_content(normalized):
        return None

    lowered = normalized.lower()
    has_exit_term = (
        any(
            term in normalized
            for term in [
                "平仓",
                "全平",
                "全部平",
                "全出",
                "全部出",
                "出局",
                "离场",
                "止盈了",
                "止损了",
            ]
        )
        or any(term in lowered for term in ["close", "closed", "exit", "stop out", "stopped out"])
    )
    if not has_exit_term:
        return None

    symbol = _extract_exit_symbol(normalized)
    side = _extract_exit_side(normalized)
    if symbol is None and side is None:
        return None
    return symbol, side


def _parse_explicit_cancel_signal(text: str) -> tuple[str | None, str | None] | None:
    normalized = (text or "").strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if any(
        term in normalized
        for term in [
            "\u53d6\u6d88\u9650\u4ef7",
            "\u53d6\u6d88\u6302\u5355",
            "\u53d6\u6d88\u9650\u4ef7\u6302\u5355",
            "\u64a4\u5355",
            "\u53d6\u6d88\u8ba2\u5355",
            "\u6302\u5355\u53d6\u6d88",
        ]
    ):
        return _extract_exit_symbol(normalized), _extract_exit_side(normalized)
    if any(
        term in normalized
        for term in ["取消限价", "取消挂单", "取消限价挂单", "撤单", "取消订单", "挂单取消"]
    ):
        return _extract_exit_symbol(normalized), _extract_exit_side(normalized)
    has_cancel_term = (
        any(term in normalized for term in ["取消限价", "取消挂单", "撤单", "取消订单", "挂单取消"])
        or any(term in lowered for term in ["cancel limit", "cancel order", "cancel entry"])
    )
    has_unentered_cancel_term = (
        "取消" in normalized
        and any(term in normalized for term in ["没有入场", "没入场", "未入场", "没有进场", "没进场", "未进场"])
        and any(term in normalized for term in ["策略", "挂单", "单"])
    )
    has_english_unentered_cancel_term = (
        "cancel" in lowered
        and any(term in lowered for term in ["not entered", "no entry", "unfilled", "not filled"])
    )
    if has_unentered_cancel_term or has_english_unentered_cancel_term:
        return _extract_exit_symbol(normalized), _extract_exit_side(normalized)
    if not has_cancel_term:
        return None
    return _extract_exit_symbol(normalized), _extract_exit_side(normalized)


def _cancel_signal_applies_to_all_matches(text: str) -> bool:
    normalized = (text or "").strip()
    lowered = normalized.lower()
    return (
        any(term in normalized for term in ["全部", "所有", "都", "均", "两次", "两单", "两个"])
        or any(term in lowered for term in ["all", "both"])
    )


def _parse_explicit_entry_confirmation_signal(
    text: str,
) -> tuple[str | None, str | None, float | None] | None:
    normalized = (text or "").strip()
    if not normalized:
        return None
    if _parse_explicit_cancel_signal(normalized) is not None:
        return None
    if _parse_explicit_exit_signal(normalized) is not None:
        return None

    lowered = normalized.lower()
    has_entry_confirm_term = (
        any(
            term in normalized
            for term in [
                "现价入场",
                "现价进",
                "现价开",
                "现价上车",
                "市价入场",
                "市价进",
                "直接进",
                "直接入场",
                "现在进",
                "现在入场",
                "已进",
                "进了",
                "按现价",
            ]
        )
        or (
            any(term in normalized for term in ["现价", "市价"])
            and any(term in normalized for term in ["入场", "进场", "开仓", "建仓", "上车"])
        )
        or any(
            term in lowered
            for term in [
                "market entry",
                "enter now",
                "entry now",
                "enter at market",
                "market in",
                "entered",
            ]
        )
    )
    if not has_entry_confirm_term:
        return None

    symbol = _extract_exit_symbol(normalized)
    side = _extract_exit_side(normalized)
    price = _extract_entry_confirmation_price(normalized, symbol)
    return symbol, side, price


def _extract_entry_confirmation_price(text: str, symbol: str | None) -> float | None:
    values = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", text)]
    if not values:
        return None
    for value in values:
        if symbol == "BTC" and value >= 1000:
            return value
        if symbol == "ETH" and value >= 100:
            return value
        if symbol not in {"BTC", "ETH"} and value > 100:
            return value
    return None


def _datetime_after(left, right) -> bool:
    if left is None or right is None:
        return False
    if getattr(left, "tzinfo", None) is not None:
        left = left.replace(tzinfo=None)
    if getattr(right, "tzinfo", None) is not None:
        right = right.replace(tzinfo=None)
    return left > right


def _extract_exit_symbol(text: str) -> str | None:
    for alias, symbol in EXIT_SYMBOL_ALIASES.items():
        if alias.isascii():
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE):
                return symbol
        elif alias in text:
            return symbol
    return None


def _text_mentions_exit_symbol(text: str, expected_symbol: str) -> bool:
    for alias, symbol in EXIT_SYMBOL_ALIASES.items():
        if symbol != expected_symbol:
            continue
        if alias.isascii():
            if re.search(
                rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                text,
                flags=re.IGNORECASE,
            ):
                return True
        elif alias in text:
            return True
    return False


def _extract_exit_side(text: str) -> str | None:
    lowered = text.lower()
    if any(term in text for term in ["空单", "做空", "开空"]) or "short" in lowered:
        return "short"
    if any(term in text for term in ["多单", "做多", "开多"]) or "long" in lowered:
        return "long"
    return None


def _looks_like_trading_education_content(text: str) -> bool:
    normalized = " ".join((text or "").split())
    if len(normalized) < 120:
        return False

    operational_terms = [
        "全部止盈",
        "全平",
        "平仓",
        "临时离场",
        "先离场",
        "先出来",
        "止盈出局",
        "保本出局",
        "成本走",
        "止损修改",
        "调整止损",
        "推保护",
        "带保护",
        "撤单",
        "取消挂单",
    ]
    if any(term in normalized for term in operational_terms):
        return False

    lowered = normalized.lower()
    if any(term in lowered for term in ["close now", "exit now", "take profit now"]):
        return False

    education_terms = [
        "知识点",
        "举个例子",
        "很多人",
        "新手",
        "一直强调",
        "交易不是",
        "长期盈利",
        "方向一样",
        "真正决定",
        "经验",
        "教学",
        "盈亏比",
        "位置决定风险",
    ]
    return sum(1 for term in education_terms if term in normalized) >= 2


def _upsert_recognition(
    session,
    result: MessageRecognitionResult,
    *,
    engine: str = "local_rule_parser",
) -> MessageRecognition:
    recognition = (
        session.query(MessageRecognition)
        .filter(MessageRecognition.raw_message_id == result.raw_message_id)
        .one_or_none()
    )
    if recognition is None:
        recognition = MessageRecognition(raw_message_id=result.raw_message_id, status=result.status)
        session.add(recognition)
    recognition.status = result.status
    recognition.reason = result.reason
    recognition.summary = result.summary
    recognition.engine = engine
    recognition.updated_at = utc_now()
    return recognition


def _format_candidate_summary(candidate: SignalCandidate) -> str:
    parts: list[str] = []
    symbol_side = " ".join(value for value in [candidate.symbol, candidate.side] if value)
    if symbol_side:
        parts.append(symbol_side)
    if candidate.entry_text:
        parts.append(f"Entry {candidate.entry_text}")
    if candidate.stop_loss_text:
        parts.append(f"SL {candidate.stop_loss_text}")
    if candidate.take_profit_text:
        parts.append(f"TP {candidate.take_profit_text}")
    if candidate.leverage_text:
        parts.append(candidate.leverage_text)
    return "；".join(parts) or "已命中策略候选"


def _format_entry_range(entry_range: tuple[float, float] | None) -> str | None:
    if entry_range is None:
        return None
    return f"{_format_number(entry_range[0])}-{_format_number(entry_range[1])}"


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def _has_video_like_media(media_assets: list[MediaAsset]) -> bool:
    for media_asset in media_assets:
        media_kind = (media_asset.kind or "").lower()
        mime_type = (media_asset.mime_type or "").lower()
        if "video" in media_kind or "document" in media_kind or mime_type.startswith("video/"):
            return True
    return False


def _has_image_like_media(media_assets: list[MediaAsset]) -> bool:
    for media_asset in media_assets:
        media_kind = (media_asset.kind or "").lower()
        mime_type = (media_asset.mime_type or "").lower()
        if "photo" in media_kind or "image" in media_kind or mime_type.startswith("image/"):
            return True
    return False


def _ensure_lifecycle_record(
    session,
    raw_message: RawMessage,
    candidate: SignalCandidate,
) -> StrategyLifecycle | None:
    """Create a StrategyLifecycle record for a newly recognised entry signal.

    Idempotent — if a lifecycle record already exists for this
    (chat_id, message_id) it will be left unchanged.
    """
    from sqlalchemy.exc import IntegrityError

    if not candidate.symbol or not candidate.side:
        return None

    existing = (
        session.query(StrategyLifecycle)
        .filter(
            StrategyLifecycle.chat_id == raw_message.chat_id,
            StrategyLifecycle.message_id == raw_message.message_id,
        )
        .one_or_none()
    )
    if existing is not None:
        _backfill_lifecycle_strategy_fields(existing, candidate)
        return existing

    entry_low, entry_high = _parse_entry_range_values(candidate.entry_text)
    stop_loss = _parse_single_float(candidate.stop_loss_text)
    duplicate = _find_duplicate_active_lifecycle(
        session,
        raw_message=raw_message,
        candidate=candidate,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
    )
    if duplicate is not None:
        candidate.review_note = (
            f"Duplicate active strategy lifecycle #{duplicate.id}; "
            f"original message #{duplicate.message_id}."
        )
        candidate.event_type = "duplicate_entry_signal"
        candidate.confidence = max(candidate.confidence or 0.0, 0.9)
        return duplicate

    correction = _find_active_lifecycle_entry_correction(
        session,
        raw_message=raw_message,
        candidate=candidate,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
    )
    if correction is not None:
        _apply_entry_correction_to_lifecycle(
            correction,
            raw_message=raw_message,
            candidate=candidate,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
        )
        return correction

    lc = StrategyLifecycle(
        signal_candidate_id=candidate.id,
        chat_id=raw_message.chat_id,
        message_id=raw_message.message_id,
        symbol=candidate.symbol.upper(),
        side=candidate.side.lower(),
        lifecycle_status="pending_entry",
        signal_at=raw_message.posted_at or utc_now(),
        entry_range_low=entry_low,
        entry_range_high=entry_high,
        stop_loss=stop_loss,
        take_profit=candidate.take_profit_text,
    )
    session.add(lc)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return lc


def _find_active_lifecycle_entry_correction(
    session,
    *,
    raw_message: RawMessage,
    candidate: SignalCandidate,
    entry_low: float | None,
    entry_high: float | None,
    stop_loss: float | None,
    window_hours: int = DUPLICATE_ACTIVE_STRATEGY_WINDOW_HOURS,
) -> StrategyLifecycle | None:
    signal_at = raw_message.posted_at or utc_now()
    symbol = (candidate.symbol or "").upper()
    side = (candidate.side or "").lower()
    if not symbol or not side:
        return None

    candidate_entry = (entry_low, entry_high)
    if not any(value is not None for value in candidate_entry):
        return None

    candidate_tp_values = _strategy_price_values(
        candidate.take_profit_text,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
    )
    if stop_loss is None and not candidate_tp_values:
        return None

    query = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
        .filter(StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered"]))
        .filter(StrategyLifecycle.symbol == symbol)
        .filter(StrategyLifecycle.side == side)
        .filter(StrategyLifecycle.signal_at <= signal_at)
        .filter(StrategyLifecycle.signal_at >= signal_at - timedelta(hours=window_hours))
        .order_by(StrategyLifecycle.signal_at.desc(), StrategyLifecycle.id.desc())
    )
    for lifecycle in query.all():
        lifecycle_entry = (lifecycle.entry_range_low, lifecycle.entry_range_high)
        if _same_optional_float_pair(lifecycle_entry, candidate_entry):
            continue
        if not _looks_like_entry_correction(
            old_entry=lifecycle_entry,
            new_entry=candidate_entry,
            side=side,
            stop_loss=stop_loss,
        ):
            continue
        if not _same_optional_float(lifecycle.stop_loss, stop_loss):
            continue
        lifecycle_tp_values = _strategy_price_values(
            lifecycle.take_profit,
            entry_low=lifecycle.entry_range_low,
            entry_high=lifecycle.entry_range_high,
            stop_loss=lifecycle.stop_loss,
        )
        if lifecycle_tp_values and candidate_tp_values and lifecycle_tp_values != candidate_tp_values:
            continue
        return lifecycle
    return None


def _looks_like_entry_correction(
    *,
    old_entry: tuple[float | None, float | None],
    new_entry: tuple[float | None, float | None],
    side: str,
    stop_loss: float | None,
) -> bool:
    old_values = sorted(value for value in old_entry if value is not None)
    new_values = sorted(value for value in new_entry if value is not None)
    if not old_values or not new_values:
        return False
    old_low, old_high = old_values[0], old_values[-1]
    new_low, new_high = new_values[0], new_values[-1]
    overlaps = old_low <= new_high and new_low <= old_high
    if not overlaps:
        return False
    old_contains_new = old_low <= new_low and new_high <= old_high
    new_contains_old = new_low <= old_low and old_high <= new_high
    if old_contains_new or new_contains_old:
        return True
    old_plausible = _entry_range_plausible_for_side(
        entry_low=old_low,
        entry_high=old_high,
        side=side,
        stop_loss=stop_loss,
    )
    new_plausible = _entry_range_plausible_for_side(
        entry_low=new_low,
        entry_high=new_high,
        side=side,
        stop_loss=stop_loss,
    )
    return not old_plausible and new_plausible


def _entry_range_plausible_for_side(
    *,
    entry_low: float,
    entry_high: float,
    side: str,
    stop_loss: float | None,
) -> bool:
    if stop_loss is None:
        return True
    if side == "short":
        return entry_high < stop_loss
    if side == "long":
        return entry_low > stop_loss
    return True


def _apply_entry_correction_to_lifecycle(
    lifecycle: StrategyLifecycle,
    *,
    raw_message: RawMessage,
    candidate: SignalCandidate,
    entry_low: float | None,
    entry_high: float | None,
    stop_loss: float | None,
) -> None:
    old_entry = _format_lifecycle_entry_pair(
        lifecycle.entry_range_low,
        lifecycle.entry_range_high,
    )
    new_entry = _format_lifecycle_entry_pair(entry_low, entry_high)
    lifecycle.entry_range_low = entry_low
    lifecycle.entry_range_high = entry_high
    if stop_loss is not None:
        lifecycle.stop_loss = stop_loss
    if candidate.take_profit_text:
        lifecycle.take_profit = candidate.take_profit_text
    lifecycle.management_signal_message_id = raw_message.message_id
    lifecycle.management_action = "strategy_correction"
    lifecycle.management_note = (
        f"后续策略修正：入场区间由 {old_entry or '-'} 调整为 {new_entry or '-'}；"
        f"修正消息 #{raw_message.message_id}。"
    )
    lifecycle.updated_at = utc_now()
    candidate.review_note = (
        f"Strategy correction for active lifecycle #{lifecycle.id}; "
        f"entry {old_entry or '-'} -> {new_entry or '-'}."
    )
    candidate.event_type = "strategy_correction"
    candidate.confidence = max(candidate.confidence or 0.0, 0.9)


def _find_duplicate_active_lifecycle(
    session,
    *,
    raw_message: RawMessage,
    candidate: SignalCandidate,
    entry_low: float | None,
    entry_high: float | None,
    stop_loss: float | None,
    window_hours: int = DUPLICATE_ACTIVE_STRATEGY_WINDOW_HOURS,
) -> StrategyLifecycle | None:
    signal_at = raw_message.posted_at or utc_now()
    symbol = (candidate.symbol or "").upper()
    side = (candidate.side or "").lower()
    if not symbol or not side:
        return None

    query = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
        .filter(StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered"]))
        .filter(StrategyLifecycle.symbol == symbol)
        .filter(StrategyLifecycle.side == side)
        .filter(StrategyLifecycle.signal_at <= signal_at)
        .filter(StrategyLifecycle.signal_at >= signal_at - timedelta(hours=window_hours))
        .order_by(StrategyLifecycle.signal_at.desc(), StrategyLifecycle.id.desc())
    )
    candidate_tp_values = _strategy_price_values(
        candidate.take_profit_text,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
    )
    for lifecycle in query.all():
        if not _same_optional_float_pair(
            (lifecycle.entry_range_low, lifecycle.entry_range_high),
            (entry_low, entry_high),
        ):
            continue
        if not _same_optional_float(lifecycle.stop_loss, stop_loss):
            continue
        lifecycle_tp_values = _strategy_price_values(
            lifecycle.take_profit,
            entry_low=lifecycle.entry_range_low,
            entry_high=lifecycle.entry_range_high,
            stop_loss=lifecycle.stop_loss,
        )
        if lifecycle_tp_values and candidate_tp_values and lifecycle_tp_values != candidate_tp_values:
            continue
        return lifecycle
    return None


def _same_optional_float(left: float | None, right: float | None, *, tolerance: float = 1e-6) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) <= tolerance


def _same_optional_float_pair(
    left: tuple[float | None, float | None],
    right: tuple[float | None, float | None],
) -> bool:
    left_values = sorted(value for value in left if value is not None)
    right_values = sorted(value for value in right if value is not None)
    if len(left_values) != len(right_values):
        return False
    return all(_same_optional_float(a, b) for a, b in zip(left_values, right_values))


def _format_lifecycle_entry_pair(
    entry_low: float | None,
    entry_high: float | None,
) -> str | None:
    if entry_low is None and entry_high is None:
        return None
    if _same_optional_float(entry_low, entry_high):
        return _format_number(entry_low)
    values = [value for value in (entry_low, entry_high) if value is not None]
    if not values:
        return None
    if len(values) == 1:
        return _format_number(values[0])
    return f"{_format_number(min(values))}-{_format_number(max(values))}"


def _strategy_price_values(
    text: str | None,
    *,
    entry_low: float | None,
    entry_high: float | None,
    stop_loss: float | None,
) -> tuple[float, ...]:
    if not text:
        return ()
    values = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", text)]
    reference_prices = [
        value for value in (entry_low, entry_high, stop_loss) if value is not None and value > 0
    ]
    min_strategy_price = min(reference_prices) * 0.5 if reference_prices else 0
    filtered = [value for value in values if value >= min_strategy_price]
    return tuple(round(value, 6) for value in filtered)


def _backfill_lifecycle_strategy_fields(
    lifecycle: StrategyLifecycle,
    candidate: SignalCandidate,
) -> None:
    entry_low, entry_high = _parse_entry_range_values(candidate.entry_text)
    if lifecycle.signal_candidate_id is None:
        lifecycle.signal_candidate_id = candidate.id
    if lifecycle.entry_range_low is None and entry_low is not None:
        lifecycle.entry_range_low = entry_low
    if lifecycle.entry_range_high is None and entry_high is not None:
        lifecycle.entry_range_high = entry_high
    if lifecycle.stop_loss is None:
        stop_loss = _parse_single_float(candidate.stop_loss_text)
        if stop_loss is not None:
            lifecycle.stop_loss = stop_loss
    if not lifecycle.take_profit and candidate.take_profit_text:
        lifecycle.take_profit = candidate.take_profit_text
    lifecycle.updated_at = utc_now()


def _parse_entry_range_values(entry_text: str | None) -> tuple[float | None, float | None]:
    """Parse '62000-62200' → (62000.0, 62200.0).  Returns (None, None) on failure."""
    if not entry_text:
        return None, None
    values = re.findall(r"\d+(?:\.\d+)?", entry_text)
    if len(values) < 2:
        raw = re.findall(r"\d+(?:\.\d+)?", entry_text)
        if raw:
            single = float(raw[0])
            return single, single
        return None, None
    low = float(values[0])
    high = float(values[1])
    if low > high:
        low, high = high, low
    return low, high


def _parse_single_float(text: str | None) -> float | None:
    """Parse '61000' → 61000.0.  Returns None on failure."""
    if not text:
        return None
    values = re.findall(r"\d+(?:\.\d+)?", text)
    return float(values[0]) if values else None

"""Immediate message-level strategy recognition."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from datetime import timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import (
    AiProviderConfig,
    AiRecognitionConfig,
    load_ai_recognition_config,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    MediaAsset,
    MessageRecognition,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    TradeIdea,
    utc_now,
)
from telegram_kol_research.raw_ingest import NormalizedMessageRecord
from telegram_kol_research.recognition_profiles import BITCOIN_JUNZHANG_PROFILE
from telegram_kol_research.parsing.text_parser import parse_signal_text


BLOCKED_SYMBOLS = {
    "QQ",
    "VX",
    "WX",
    "VIP",
    "HTTP",
    "HTTPS",
}

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


def recognize_message_now(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig | None = None,
    ai_recognition_config_path: str | Path = "config/ai_recognition.yaml",
) -> MessageRecognitionResult:
    """Run V1 immediate recognition for one raw message and persist the result."""

    config = ai_recognition_config or load_ai_recognition_config(ai_recognition_config_path)
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
                )
            else:
                result = _recognize_with_ai_provider(
                    raw_message=raw_message,
                    media_assets=media_assets,
                    config=config,
                    provider=config.image_provider,
                    parse_source="image_ai",
                )
                _persist_ai_result(session, raw_message, result, engine=config.image_provider.model)
            session.commit()
            return result

        if (raw_message.text or "").strip() and config.text_provider.is_configured:
            result = _recognize_with_ai_provider(
                raw_message=raw_message,
                media_assets=[],
                config=config,
                provider=config.text_provider,
                parse_source="text_ai",
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
        result = _recognize_text_with_ai_provider(
            raw_message=raw_message,
            merged_text=merged_text,
            config=config,
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
                text_only_result = _recognize_text_with_ai_provider(
                    raw_message=raw_message,
                    merged_text=caption,
                    config=config,
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
) -> MessageRecognitionResult:
    """Send plain text (already merged with OCR output) through the text AI
    provider for strategy recognition."""
    payload = _build_ai_recognition_payload(
        raw_message=raw_message,
        media_assets=[],
        prompt=config.recognition_prompt,
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
        prompt=(
            _build_multimodal_recognition_prompt(config)
            if media_assets
            else config.recognition_prompt
        ),
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


def _build_multimodal_recognition_prompt(config: AiRecognitionConfig) -> str:
    text_prompt = config.recognition_prompt.strip()
    image_prompt = config.mimo_direct_prompt.strip()
    if not text_prompt:
        return image_prompt
    if not image_prompt or image_prompt in text_prompt:
        return text_prompt
    return "\n\n".join(
        [
            text_prompt,
            (
                "多模态补充要求：当前消息可能同时包含文字/caption 和图片。"
                "必须结合文字语境与图片内容整体判断，不要只识别图片中的文字；"
                "如果文字说明这是盈利、复盘、参考或历史截图，而图片里是旧策略，"
                "应按非策略处理。"
            ),
            image_prompt,
        ]
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


LIFECYCLE_EVENT_PROMPT = """
你是 Telegram 加密货币 KOL 策略生命周期事件判定器。
你会收到：当前消息、同群最近的活跃策略列表、以及最近聊天上下文。

你的任务不是识别新策略，而是判断“当前消息”是否在改变某一条已有策略的状态。

只允许输出 JSON，不要输出解释文本：
{
  "event_type": "none | entry_confirm | cancel_entry | exit_position | position_update",
  "target_lifecycle_id": null,
  "symbol": null,
  "side": null,
  "entry_price": null,
  "exit_price": null,
  "stop_loss": null,
  "take_profit": null,
  "management_action": null,
  "confidence": 0.0,
  "reason": "一句话说明判断依据"
}

判定规则：
- entry_confirm：当前消息是在通知之前 pending_entry 策略现在/现价/市价/直接入场，或明确说已经进场。
- cancel_entry：当前消息是在取消之前 pending_entry 限价挂单或等待入场策略，例如取消限价、撤单、取消挂单、等后续信号。
- exit_position：当前消息是在关闭已 entered 策略，例如平仓、全平、离场、临时离场、止盈了、止损了、先出来、保本出局、成本附近保本出局、保本走、成本走、breakeven exit。
- position_update：当前消息是在管理已 entered 策略但没有完全离场，例如提前止盈一半、止盈一半、分批止盈30%、按比例止盈、减仓一半、减仓30%、持仓收益达到100%后分批止盈、带保护、保护止损、上移止损、推保护、继续持有。“回成本了，注意保护成本，平加仓”表示减仓一半并将止损移至成本价，management_action 应输出 partial_take_profit, move_stop_to_protect。management_action 可输出 partial_take_profit、move_stop_to_protect、hold_update、risk_update。
- none：普通聊天、行情观点、广告、复盘、联系方式、无法确定目标策略、或只是识别新策略但不改变已有策略。
- 必须优先依据当前消息，不要把上下文里的旧消息当成当前动作。
- 如果能明确对应活跃策略，请输出 target_lifecycle_id。
- 如果不能唯一对应，event_type 必须为 none 或 confidence 低于 0.7。
- confidence 低于 0.7 时，系统不会执行状态变更。
""".strip()


def _apply_ai_lifecycle_event_if_matched(
    session,
    *,
    raw_message: RawMessage,
    config: AiRecognitionConfig,
) -> MessageRecognitionResult | None:
    context = _load_lifecycle_event_context(session, raw_message)
    if not context["active_strategies"]:
        return None

    try:
        decision = _call_lifecycle_event_ai(
            raw_message=raw_message,
            context=context,
            config=config,
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
    return {
        "active_strategies": active_strategies,
        "recent_messages": recent_context,
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
) -> dict[str, Any]:
    provider = config.text_provider
    payload = {
        "model": provider.model,
        "messages": [
            {
                "role": "system",
                "content": config.lifecycle_event_prompt.strip() or LIFECYCLE_EVENT_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "current_message": {
                            "chat_id": raw_message.chat_id,
                            "message_id": raw_message.message_id,
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
    return _parse_ai_result_json(_extract_ai_content(data))


def _apply_lifecycle_event_decision(
    session,
    raw_message: RawMessage,
    decision: dict[str, Any],
) -> bool:
    event_type = str(decision.get("event_type") or "none").strip()
    try:
        confidence = float(decision.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if event_type == "none" or confidence < 0.7:
        return False
    if _looks_like_trading_education_content(raw_message.text or ""):
        return False
    if event_type == "exit_position" and _exit_decision_looks_like_management_update(
        raw_message.text,
        decision,
    ):
        decision = dict(decision)
        event_type = "position_update"
        decision["event_type"] = event_type
        if not str(decision.get("management_action") or "").strip():
            decision["management_action"] = _management_action_for_exit_downgrade(
                raw_message.text,
                decision,
            )
        if not decision.get("take_profit") and decision.get("exit_price"):
            decision["take_profit"] = decision.get("exit_price")
        decision["exit_price"] = None

    target = _resolve_lifecycle_event_target(session, raw_message, decision)
    if target is None:
        return False
    explicit_symbol = _extract_exit_symbol(raw_message.text or "")
    if explicit_symbol is not None and target.symbol != explicit_symbol:
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
        _upsert_entry_confirmation_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            entry_price=entry_price,
            parse_source="lifecycle_ai",
        )
        return True

    if event_type == "cancel_entry" and target.lifecycle_status == "pending_entry":
        target.lifecycle_status = "exited"
        target.exit_reason = "cancelled"
        target.exited_at = event_at
        target.exit_signal_message_id = raw_message.message_id
        target.updated_at = utc_now()
        _upsert_close_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            parse_source="lifecycle_ai",
        )
        return True

    if event_type == "exit_position" and (
        target.lifecycle_status == "entered"
        or (
            target.lifecycle_status == "expired"
            and _lifecycle_has_live_execution_binding(session, target)
        )
    ):
        target.lifecycle_status = "exited"
        target.exit_reason = "kol_signal"
        target.exited_at = event_at
        exit_price = _number_or_none(decision.get("exit_price"))
        if exit_price is not None:
            target.exit_price_actual = exit_price
        target.exit_signal_message_id = raw_message.message_id
        target.updated_at = utc_now()
        _upsert_close_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            parse_source="lifecycle_ai",
        )
        return True

    if event_type == "position_update" and target.lifecycle_status == "entered":
        management_action = str(decision.get("management_action") or "").strip() or "position_update"
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
                management_action=management_action,
            )
            else None
        )
        if explicit_stop_loss is not None:
            target.stop_loss = explicit_stop_loss
            stop_note = f"止损已按 KOL 明确指令调整为 {explicit_stop_loss:g}。"
            management_note = f"{management_note} {stop_note}" if management_note else stop_note
        elif protective_stop is not None:
            target.stop_loss = protective_stop
            protect_note = f"收到推保护价指令，止损已调整到成本保护价 {protective_stop:g}。"
            management_note = f"{management_note} {protect_note}" if management_note else protect_note
        if explicit_take_profit:
            target.take_profit = explicit_take_profit
        target.management_signal_message_id = raw_message.message_id
        target.management_action = management_action
        target.management_note = management_note
        target.updated_at = utc_now()
        _upsert_management_signal_candidate(
            session,
            raw_message=raw_message,
            lifecycle=target,
            parse_source="lifecycle_ai",
        )
        return True

    return False


def _exit_decision_looks_like_management_update(
    text: str | None,
    decision: dict[str, Any],
) -> bool:
    combined = _combined_lifecycle_text(text, decision)
    if _has_full_exit_instruction(str(text or "").lower()):
        return False
    return _has_partial_take_profit_terms(combined) or _has_protective_stop_terms(combined)


def _management_action_for_exit_downgrade(
    text: str | None,
    decision: dict[str, Any],
) -> str:
    combined = _combined_lifecycle_text(text, decision)
    has_protective_stop = _has_protective_stop_terms(combined)
    if "平加仓" in combined and has_protective_stop:
        return "partial_take_profit, move_stop_to_protect"
    if has_protective_stop:
        return "move_stop_to_protect"
    if _has_partial_take_profit_terms(combined):
        return "partial_take_profit"
    return "position_update"


def _combined_lifecycle_text(text: str | None, decision: dict[str, Any]) -> str:
    return " ".join(
        str(part or "")
        for part in (
            text,
            decision.get("reason"),
            decision.get("management_action"),
        )
    ).lower()


def _has_full_exit_instruction(text: str) -> bool:
    full_exit_terms = [
        "平仓",
        "全平",
        "全部平",
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


def _persist_ai_result(
    session,
    raw_message: RawMessage,
    result: MessageRecognitionResult,
    *,
    engine: str,
) -> None:
    payload = result.ai_payload or {}
    parse_source = result.parse_source or "ai"
    if result.status == "是策略":
        strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
        candidate = _upsert_ai_signal_candidate(
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


def _upsert_ai_signal_candidate(
    session,
    raw_message: RawMessage,
    *,
    strategy: dict[str, Any],
    confidence: float,
    parse_source: str,
) -> SignalCandidate:
    normalized_strategy = _normalize_ai_strategy(strategy)
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
            parse_source=parse_source,
            review_status="pending",
        )
        session.add(candidate)
    candidate.symbol = _string_or_none(normalized_strategy.get("symbol"))
    candidate.side = _string_or_none(normalized_strategy.get("side"))
    candidate.event_type = "entry_signal"
    candidate.entry_text = _string_or_none(normalized_strategy.get("entry"))
    candidate.stop_loss_text = _string_or_none(normalized_strategy.get("stop_loss"))
    candidate.take_profit_text = _string_or_none(normalized_strategy.get("take_profit"))
    candidate.leverage_text = _string_or_none(normalized_strategy.get("leverage"))
    candidate.confidence = max(0.0, min(confidence, 1.0))
    candidate.parse_source = parse_source
    return candidate


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
    if lifecycle.execution_binding_id is not None:
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        if binding is not None and binding.status in {"open", "active"}:
            return True
    return (
        session.query(ExecutionBinding.id)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(ExecutionBinding.chat_id == lifecycle.chat_id)
        .filter(ExecutionBinding.message_id == lifecycle.message_id)
        .filter(ExecutionBinding.symbol == lifecycle.symbol.upper())
        .filter(ExecutionBinding.side == lifecycle.side.lower())
        .filter(ExecutionBinding.status.in_(["open", "active"]))
        .first()
        is not None
    )


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
            parse_source=parse_source,
            review_status="pending",
        )
        session.add(candidate)
    candidate.symbol = lifecycle.symbol
    candidate.side = lifecycle.side
    candidate.event_type = "close_signal"
    candidate.confidence = max(candidate.confidence or 0.0, 0.85)
    candidate.parse_source = parse_source
    return candidate


def _upsert_entry_confirmation_candidate(
    session,
    *,
    raw_message: RawMessage,
    lifecycle: StrategyLifecycle,
    entry_price: float | None,
    parse_source: str = "entry_confirm_heuristic",
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
            parse_source=parse_source,
            review_status="pending",
        )
        session.add(candidate)
    candidate.symbol = lifecycle.symbol
    candidate.side = lifecycle.side
    candidate.event_type = "entry_signal"
    candidate.entry_text = _format_number(entry_price) if entry_price is not None else None
    candidate.stop_loss_text = _format_number(lifecycle.stop_loss)
    candidate.take_profit_text = lifecycle.take_profit
    candidate.confidence = max(candidate.confidence or 0.0, 0.85)
    candidate.parse_source = parse_source
    return candidate


def _upsert_management_signal_candidate(
    session,
    *,
    raw_message: RawMessage,
    lifecycle: StrategyLifecycle,
    parse_source: str,
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
            parse_source=parse_source,
            review_status="pending",
        )
        session.add(candidate)
    candidate.symbol = lifecycle.symbol
    candidate.side = lifecycle.side
    candidate.event_type = "position_update"
    candidate.entry_text = None
    candidate.stop_loss_text = _format_number(lifecycle.stop_loss)
    candidate.take_profit_text = lifecycle.take_profit
    candidate.confidence = max(candidate.confidence or 0.0, 0.85)
    candidate.parse_source = parse_source
    return candidate


def _parse_explicit_exit_signal(text: str) -> tuple[str | None, str | None] | None:
    normalized = (text or "").strip()
    if not normalized:
        return None
    if _looks_like_trading_education_content(normalized):
        return None

    lowered = normalized.lower()
    has_exit_term = (
        any(term in normalized for term in ["平仓", "全平", "全部平", "出局", "离场", "止盈了", "止损了"])
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

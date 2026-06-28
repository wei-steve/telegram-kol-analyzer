"""Side-channel AI recognition experiments."""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiRecognitionConfig,
    load_ai_recognition_config,
)
from telegram_kol_research.media_retention import resolve_media_path
from telegram_kol_research.models import MediaAsset, RawMessage, RecognitionExperiment, utc_now


MIMO_DIRECT_EXPERIMENT_NAME = "mimo_direct_v1"
MIMO_DIRECT_PROMPT_VERSION = "mimo_direct_v1"

MIMO_DIRECT_PROMPT = """
你是 Telegram 加密货币 KOL 消息的多模态交易策略识别器。

你会收到一条消息的文字/图片。请只判断当前这条消息是否包含“新的、可执行的开仓策略”。

必须判定为“是策略”的条件：
1. 有明确交易标的，例如 BTC、ETH、SOL、DOGE、BNB 等。
2. 有明确方向：long/short，做多/做空，开多/开空。
3. 有明确入场方式：具体价格、区间、市价、到价进入、挂单区间之一。
4. 至少有止损、止盈、无效价、保护价、分批止盈计划之一。
5. 表达的是新开仓或新挂单，不是已有仓位管理、复盘、教学或广告。

图片要求：
- 直接阅读图片中的文字、表格、标注和截图内容。
- 不要依赖外部 OCR 文本。
- 不要补全图片或文字里没有出现的价格、币种、方向。
- 如果图片模糊、裁切、遮挡或关键数字不确定，请判定为“识别失败”或低置信度。

只输出 JSON，不要输出解释性文字：
{
  "recognition_result": "是策略 | 非策略 | 识别失败",
  "input_reading": {
    "observed_text": "你从当前文字或图片中实际读到的关键内容；如果没有可读内容则为空字符串",
    "image_quality": "clear | blurry | cropped | unreadable | none"
  },
  "reason": "一句话说明判断依据",
  "strategy": {
    "symbol": null,
    "side": null,
    "entry": null,
    "stop_loss": null,
    "take_profit": null,
    "leverage": null,
    "order_type": null
  },
  "confidence": 0.0
}
""".strip()


@dataclass(frozen=True)
class ExperimentRunStats:
    considered: int = 0
    skipped_existing: int = 0
    skipped_no_input: int = 0
    succeeded: int = 0
    failed: int = 0


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
            try:
                payload = _call_mimo_direct_model(
                    raw_message=raw_message,
                    media_assets=media_assets,
                    model_config=model_config,
                    prompt=_build_mimo_experiment_prompt(config),
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
                _upsert_experiment_result(
                    session,
                    raw_message=raw_message,
                    model_config=model_config,
                    input_kind=actual_input_kind,
                    payload={},
                    error_message=str(exc),
                )
                stats = _replace(stats, failed=stats.failed + 1)
            session.commit()
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
        try:
            payload = _call_mimo_direct_model(
                raw_message=raw_message,
                media_assets=media_assets,
                model_config=model_config,
                prompt=_build_mimo_experiment_prompt(config),
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
            result = _upsert_experiment_result(
                session,
                raw_message=raw_message,
                model_config=model_config,
                input_kind=input_kind,
                payload={},
                error_message=str(exc),
            )
        session.commit()
        return result


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
    prompt: str,
    media_root: str | Path,
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
    return _parse_json_object(content)


def _build_mimo_payload(
    *,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    model: str,
    prompt: str = MIMO_DIRECT_PROMPT,
    media_root: str | Path = "data/media",
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
        data_url = _media_asset_to_data_url(media_asset, media_root=media_root)
        if data_url:
            user_parts.append({"type": "image_url", "image_url": {"url": data_url}})
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_parts if len(user_parts) > 1 else user_text},
        ],
        "temperature": 0,
    }


def _upsert_experiment_result(
    session,
    *,
    raw_message: RawMessage,
    model_config: AiModelConfig,
    input_kind: str,
    payload: dict[str, Any],
    error_message: str | None,
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
            prompt_version=MIMO_DIRECT_PROMPT_VERSION,
            input_kind=input_kind,
            status="识别失败",
            created_at=now,
        )
        session.add(existing)
    input_reading = payload.get("input_reading") if isinstance(payload.get("input_reading"), dict) else {}
    strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
    status = str(payload.get("recognition_result") or ("识别失败" if error_message else "识别失败")).strip()
    if status not in {"是策略", "非策略", "识别失败"}:
        status = "识别失败"
    existing.model = model_config.model
    existing.prompt_version = MIMO_DIRECT_PROMPT_VERSION
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
    has_image = any(_media_asset_to_data_url(asset, media_root=media_root) for asset in media_assets)
    if has_text and has_image:
        return "text+image"
    if has_image:
        return "image"
    if has_text:
        return "text"
    return "empty"


def _build_mimo_experiment_prompt(config: AiRecognitionConfig) -> str:
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
                "MiMo 对照实验要求：请按上面的文字策略识别规则判断。"
                "如果有图片，必须结合文字/caption 语境与图片内容整体判断；"
                "如果没有图片，就只根据当前文字判断。"
            ),
            image_prompt,
        ]
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

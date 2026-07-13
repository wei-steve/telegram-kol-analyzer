"""Side-channel AI recognition experiments."""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass, field
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
from telegram_kol_research.models import MediaAsset, RawMessage, RecognitionExperiment, StrategyLifecycle, utc_now
from telegram_kol_research.prompt_composition import compose_trading_prompt
from telegram_kol_research.prompt_defaults import seed_default_prompt_registry
from telegram_kol_research.prompt_registry import (
    PromptInvocationRecord,
    record_prompt_invocation,
)


MIMO_DIRECT_EXPERIMENT_NAME = "mimo_direct_v1"
MIMO_DIRECT_PROMPT_VERSION = "mimo_direct_v1"
MIMO_AUTHORITATIVE_PROMPT_VERSION = "mimo_authoritative_v1"
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


@dataclass(frozen=True)
class MimoAuthoritativeResult:
    raw_message_id: int
    payload: dict[str, Any]
    input_kind: str
    model: str
    status: str
    error_message: str | None = None
    prompt_versions: dict[str, int] = field(default_factory=dict)

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
        session.refresh(result)
        session.expunge(result)
        return result


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
        try:
            payload = _call_mimo_direct_model(
                raw_message=raw_message,
                media_assets=media_assets,
                model_config=model_config,
                prompt=composition.system_prompt,
                media_root=media_root,
                context_text=composition.context,
            )
            _validate_authoritative_payload(payload)
        except Exception as exc:
            payload = {}
            error_message = str(exc)
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
    prompt: str,
    media_root: str | Path,
    context_text: str = "",
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
    context_text: str = "",
) -> dict[str, Any]:
    user_text = (
        f"Message metadata:\n"
        f"chat_id={raw_message.chat_id}\n"
        f"message_id={raw_message.message_id}\n"
        f"sender={raw_message.sender_name or 'Unknown'}\n\n"
        f"Text/caption:\n{(raw_message.text or '').strip() or '(empty)'}"
    )
    if context_text.strip():
        user_text = f"{user_text}\n\n{context_text.strip()}"
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


def _build_authoritative_context(session, raw_message: RawMessage) -> str:
    recent = (
        session.query(RawMessage)
        .filter(RawMessage.chat_id == raw_message.chat_id)
        .filter(RawMessage.id != raw_message.id)
        .order_by(RawMessage.posted_at.desc(), RawMessage.message_id.desc())
        .limit(20)
        .all()
    )
    recent_rows = [
        {
            "message_id": row.message_id,
            "text": (row.text or "").strip(),
        }
        for row in reversed(recent)
        if (row.text or "").strip()
    ]
    active = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
        .filter(StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered", "expired"]))
        .order_by(StrategyLifecycle.signal_at.desc())
        .limit(20)
        .all()
    )
    active_rows = [
        {
            "lifecycle_id": row.id,
            "source_message_id": row.message_id,
            "symbol": row.symbol,
            "side": row.side,
            "status": row.lifecycle_status,
            "entry_range_low": row.entry_range_low,
            "entry_range_high": row.entry_range_high,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
        }
        for row in active
    ]
    return "\n".join(
        [
            "Recent context:",
            json.dumps(recent_rows, ensure_ascii=False, sort_keys=True),
            "Active strategies:",
            json.dumps(active_rows, ensure_ascii=False, sort_keys=True),
        ]
    )


def build_authoritative_context_for_message(
    session_factory: sessionmaker,
    raw_message_id: int,
) -> str:
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
                "除了识别新的开仓策略，也要识别已有策略的生命周期事件。"
                "当消息出现“第一止盈点来了”“止盈点来了”“已到第一目标”“减仓”“部分止盈”"
                "并且文字或图片显示已有持仓、收益率、开仓均价、最新价、交易对或多空方向时，"
                "应判定为“仓位管理”，不要判定为“非策略”。"
                "当消息表示全部止盈、平仓、离场、止损时，判定为“离场信号”；"
                "当消息表示现在进场、已进场、入场了，判定为“入场确认”；"
                "当消息表示取消挂单、取消策略、不进了，判定为“取消入场”。"
                "recognition_result 只能输出：是策略、非策略、识别失败、入场确认、取消入场、离场信号、仓位管理、策略调整。"
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

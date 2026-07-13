"""Side-effect-free active-versus-draft prompt comparisons."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.authoritative_recognition import compare_assessments
from telegram_kol_research.message_recognition import (
    _build_ai_recognition_payload,
    _chat_completions_url,
    _extract_ai_content,
    _parse_ai_result_json,
)
from telegram_kol_research.models import AiPromptTestRun, MediaAsset, RawMessage
from telegram_kol_research.prompt_defaults import (
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
)
from telegram_kol_research.prompt_registry import (
    get_prompt_detail,
    resolve_active_prompt,
)
from telegram_kol_research.recognition_experiments import (
    _build_authoritative_context,
    _call_mimo_direct_model,
    _find_mimo_model,
    _is_image_asset,
    _media_asset_to_data_url,
)


PromptModelCaller = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class PromptDraftTestResult:
    test_run_id: int
    active_payload: dict[str, Any]
    draft_payload: dict[str, Any]
    differences: list[str]
    duration_ms: int
    error_message: str | None


def run_prompt_draft_test(
    session_factory: sessionmaker,
    *,
    prompt_key: str,
    draft_version_id: int,
    raw_message_id: int,
    model_kind: str,
    ai_recognition_config: AiRecognitionConfig,
    media_root: str | Path,
    model_caller: PromptModelCaller | None = None,
) -> PromptDraftTestResult:
    """Compare published and draft prompts without applying either result."""

    if prompt_key not in {SHARED_TRADING_PROMPT, MIMO_VISION_PROMPT}:
        raise ValueError("historical recognition tests support trading prompts only")
    if model_kind not in {"mimo", "deepseek"}:
        raise ValueError(f"unsupported model kind: {model_kind}")
    if prompt_key == MIMO_VISION_PROMPT and model_kind != "mimo":
        raise ValueError("MiMo vision prompt can only be tested with MiMo")

    detail = get_prompt_detail(session_factory, prompt_key)
    if detail.draft_version is None or detail.draft_version.id != draft_version_id:
        raise ValueError("draft version changed")

    shared = resolve_active_prompt(session_factory, SHARED_TRADING_PROMPT)
    vision = resolve_active_prompt(session_factory, MIMO_VISION_PROMPT)
    active_parts = [shared.content]
    draft_parts = [shared.content]
    if model_kind == "mimo":
        active_parts.append(vision.content)
        draft_parts.append(vision.content)
    if prompt_key == SHARED_TRADING_PROMPT:
        draft_parts[0] = detail.draft_version.content
    else:
        draft_parts[1] = detail.draft_version.content

    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        if raw_message is None:
            raise LookupError("raw message not found")
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == raw_message_id)
            .order_by(MediaAsset.id.asc())
            .all()
        )
        if prompt_key == MIMO_VISION_PROMPT and not any(
            _is_image_asset(asset) for asset in media_assets
        ):
            raise ValueError("MiMo vision prompt tests require image input")
        unreadable = [
            asset
            for asset in media_assets
            if _is_image_asset(asset)
            and _media_asset_to_data_url(asset, media_root=media_root) is None
        ]
        if unreadable:
            raise ValueError("image media is unavailable or unreadable")
        context_text = _build_authoritative_context(session, raw_message)
        caller = model_caller or _call_configured_model
        model = _model_name(ai_recognition_config, model_kind)
        started = time.perf_counter()
        active_payload: dict[str, Any] = {}
        draft_payload: dict[str, Any] = {}
        differences: list[str] = []
        error_message = None
        try:
            active_payload = caller(
                model_kind=model_kind,
                system_prompt="\n\n".join(active_parts),
                context_text=context_text,
                raw_message=raw_message,
                media_assets=media_assets,
                config=ai_recognition_config,
                media_root=media_root,
            )
            draft_payload = caller(
                model_kind=model_kind,
                system_prompt="\n\n".join(draft_parts),
                context_text=context_text,
                raw_message=raw_message,
                media_assets=media_assets,
                config=ai_recognition_config,
                media_root=media_root,
            )
            _, differences = compare_assessments(active_payload, draft_payload)
        except Exception as exc:
            error_message = str(exc)
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        row = AiPromptTestRun(
            prompt_definition_id=detail.definition_id,
            draft_version_id=draft_version_id,
            raw_message_id=raw_message_id,
            model=model,
            status="failed" if error_message else "completed",
            active_result_json=json.dumps(active_payload, ensure_ascii=False, sort_keys=True),
            draft_result_json=json.dumps(draft_payload, ensure_ascii=False, sort_keys=True),
            differences_json=json.dumps(differences, ensure_ascii=False),
            error_message=error_message,
            duration_ms=duration_ms,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return PromptDraftTestResult(
            test_run_id=row.id,
            active_payload=active_payload,
            draft_payload=draft_payload,
            differences=differences,
            duration_ms=duration_ms,
            error_message=error_message,
        )


def _model_name(config: AiRecognitionConfig, model_kind: str) -> str:
    if model_kind == "deepseek":
        return config.text_provider.model or "deepseek"
    model_config = _find_mimo_model(config)
    return model_config.model if model_config is not None else "mimo-v2.5"


def _call_configured_model(
    *,
    model_kind: str,
    system_prompt: str,
    context_text: str,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    config: AiRecognitionConfig,
    media_root: str | Path,
) -> dict[str, Any]:
    if model_kind == "mimo":
        model_config = _find_mimo_model(config)
        if model_config is None or not model_config.provider.is_configured:
            raise RuntimeError("MiMo model is not configured")
        return _call_mimo_direct_model(
            raw_message=raw_message,
            media_assets=media_assets,
            model_config=model_config,
            prompt=system_prompt,
            media_root=media_root,
            context_text=context_text,
        )

    provider = config.text_provider
    if not provider.is_configured:
        raise RuntimeError("DeepSeek model is not configured")
    request_payload = _build_ai_recognition_payload(
        raw_message=raw_message,
        media_assets=[],
        prompt=system_prompt,
        model=provider.model,
    )
    request_payload["messages"][1]["content"] = "\n\n".join(
        part
        for part in (
            str(request_payload["messages"][1]["content"]),
            context_text,
        )
        if part
    )
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    with httpx.Client(timeout=provider.timeout_seconds) as client:
        response = client.post(
            _chat_completions_url(provider.base_url),
            json=request_payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
    return _parse_ai_result_json(_extract_ai_content(data))

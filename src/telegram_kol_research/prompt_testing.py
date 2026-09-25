"""Side-effect-free active-versus-draft prompt comparisons.

The test run calls **the model the relevant stage is bound to right now**, not
a hard-coded vendor. Until 2026-09-25 this module took a ``model_kind`` of
``"mimo"`` or ``"deepseek"``; both names had come loose from what they did.
``"mimo"`` meant "the head of the ``authoritative_recognition`` chain", which
by then was ``gpt-5.6-luna``, and ``"deepseek"`` meant "the head of
``batch_text_recognition``", a stage that is not on the production path at all
(``docs/ARCHITECTURE.md`` §5.5) and whose account started answering 402 the
same day. A name that has to be explained before it can be read is naming
debt, so the selector is now the model id and the candidates come from the
stage binding -- rebind the stage and the test follows it.

Both prompts this module compares are consumed by one stage,
``authoritative_recognition``: ``recognition_experiments`` composes
``trading.analysis.shared`` + ``trading.analysis.mimo_vision`` into the single
system prompt it sends down that chain, so the vision prompt is a supplement
to the shared one rather than a stage of its own.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_model_router import resolve_stage_chain
from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.ai_stage_catalog import AUTHORITATIVE_STAGE
from telegram_kol_research.authoritative_recognition import compare_assessments
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
    _is_image_asset,
    _media_asset_to_data_url,
    _validate_authoritative_payload,
)


PromptModelCaller = Callable[..., dict[str, Any]]


#: Which stage's binding each testable prompt follows. Both entries are
#: ``authoritative_recognition`` on purpose: that is the one stage which sends
#: either of these prompts to a model in production. ``trading.analysis.shared``
#: is also composed for ``batch_text_recognition``, but that stage is CLI-only
#: (§5.5), and testing a trading prompt against a model production never asks
#: is what produced the misleading "DeepSeek 复核" reading in the first place.
PROMPT_TEST_STAGE_BY_PROMPT_KEY: dict[str, str] = {
    SHARED_TRADING_PROMPT: AUTHORITATIVE_STAGE,
    MIMO_VISION_PROMPT: AUTHORITATIVE_STAGE,
}

#: Prompts whose subject matter is the image, so a model that cannot read one
#: has nothing to say about the draft. This is the capability the configuration
#: already records per model, which is what replaced "it has to be MiMo".
IMAGE_READING_PROMPT_KEYS: frozenset[str] = frozenset({MIMO_VISION_PROMPT})


class PromptTestModelError(ValueError):
    """No usable model, or the named one cannot run this prompt.

    A ``ValueError`` so that the Web layer's existing handler keeps turning it
    into ``422`` rather than a 500.
    """


@dataclass(frozen=True)
class PromptDraftTestResult:
    test_run_id: int
    active_payload: dict[str, Any]
    draft_payload: dict[str, Any]
    differences: list[str]
    duration_ms: int
    error_message: str | None
    model_id: str = ""
    model: str = ""
    stage_key: str = ""


def prompt_test_stage_key(prompt_key: str) -> str:
    """The stage whose binding this prompt's historical test follows."""

    try:
        return PROMPT_TEST_STAGE_BY_PROMPT_KEY[str(prompt_key)]
    except KeyError:
        raise ValueError(
            "historical recognition tests support trading prompts only"
        ) from None


def prompt_requires_image_model(prompt_key: str) -> bool:
    return str(prompt_key) in IMAGE_READING_PROMPT_KEYS


def prompt_test_models(
    config: AiRecognitionConfig,
    prompt_key: str,
) -> list[AiModelConfig]:
    """The models this prompt's historical test may be run on, in chain order.

    The first entry is the default: it is the model the stage would call for a
    real message, so the default test is the one that says what production
    would do. An empty list means the stage has no usable model.
    """

    stage_key = prompt_test_stage_key(prompt_key)
    chain = resolve_stage_chain(config, stage_key)
    if prompt_requires_image_model(prompt_key):
        chain = [model for model in chain if model.supports_image]
    return chain


def resolve_prompt_test_model(
    config: AiRecognitionConfig,
    *,
    prompt_key: str,
    model_id: str | None = None,
) -> AiModelConfig:
    """Pick the model one test run calls, or say why none can be picked.

    ``model_id`` of ``None`` means "whatever the stage starts with", which is
    the only default that keeps following a rebind.
    """

    stage_key = prompt_test_stage_key(prompt_key)
    requested = str(model_id or "").strip()
    if requested:
        # Capability is judged on the model itself and before routability: "it
        # cannot read images" is the answer the person needs, and it does not
        # become a different answer because a provider is disabled today.
        named = _model_by_id(config, requested)
        if named is not None:
            _require_prompt_capability(prompt_key, named)
    chain = prompt_test_models(config, prompt_key)
    if not chain:
        raise PromptTestModelError(
            f"stage {stage_key} has no usable model for this prompt: "
            "bind one on the AI 模型选择 page"
        )
    if not requested:
        return chain[0]
    for model in chain:
        if model.id == requested:
            _require_prompt_capability(prompt_key, model)
            return model
    raise PromptTestModelError(
        f"model {requested} is not a usable member of stage {stage_key}"
    )


def _model_by_id(config: AiRecognitionConfig, model_id: str) -> Any:
    for model in getattr(config, "models", ()) or ():
        if str(getattr(model, "id", "")) == model_id:
            return model
    return None


def _require_prompt_capability(prompt_key: str, model: Any) -> None:
    if not prompt_requires_image_model(prompt_key):
        return
    if bool(getattr(model, "supports_image", False)):
        return
    raise PromptTestModelError(
        f"model {getattr(model, 'id', '')} cannot read images, and "
        f"{prompt_key} is an image-reading prompt"
    )


def run_prompt_draft_test(
    session_factory: sessionmaker,
    *,
    prompt_key: str,
    draft_version_id: int,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig,
    media_root: str | Path,
    model_id: str | None = None,
    model_caller: PromptModelCaller | None = None,
) -> PromptDraftTestResult:
    """Compare published and draft prompts without applying either result."""

    stage_key = prompt_test_stage_key(prompt_key)
    model = resolve_prompt_test_model(
        ai_recognition_config, prompt_key=prompt_key, model_id=model_id
    )

    detail = get_prompt_detail(session_factory, prompt_key)
    if detail.draft_version is None or detail.draft_version.id != draft_version_id:
        raise ValueError("draft version changed")

    shared = resolve_active_prompt(session_factory, SHARED_TRADING_PROMPT)
    vision = resolve_active_prompt(session_factory, MIMO_VISION_PROMPT)
    # One composition, the one production uses: the authoritative stage always
    # sends shared + vision as a single system prompt, so a comparison built
    # any other way would be measuring a prompt nothing runs.
    active_prompt_versions = {
        SHARED_TRADING_PROMPT: shared.version_id,
        MIMO_VISION_PROMPT: vision.version_id,
    }
    active_parts = [shared.content, vision.content]
    draft_parts = [shared.content, vision.content]
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
        if prompt_requires_image_model(prompt_key) and not any(
            _is_image_asset(asset) for asset in media_assets
        ):
            raise ValueError("image-reading prompt tests require image input")
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
        started = time.perf_counter()
        active_payload: dict[str, Any] = {}
        draft_payload: dict[str, Any] = {}
        differences: list[str] = []
        error_message = None
        try:
            active_payload = caller(
                model=model,
                system_prompt="\n\n".join(active_parts),
                context_text=context_text,
                raw_message=raw_message,
                media_assets=media_assets,
                media_root=media_root,
            )
            _validate_prompt_test_payload(prompt_key, active_payload)
            draft_payload = caller(
                model=model,
                system_prompt="\n\n".join(draft_parts),
                context_text=context_text,
                raw_message=raw_message,
                media_assets=media_assets,
                media_root=media_root,
            )
            _validate_prompt_test_payload(prompt_key, draft_payload)
            _, differences = compare_assessments(active_payload, draft_payload)
        except Exception as exc:
            error_message = str(exc)
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        row = AiPromptTestRun(
            prompt_definition_id=detail.definition_id,
            draft_version_id=draft_version_id,
            raw_message_id=raw_message_id,
            model=model.model,
            # The stage binding this run followed. Old rows hold ``mimo`` /
            # ``deepseek``, which already meant "the head of one stage's
            # chain" -- this is that same fact said precisely, and it stays
            # readable when the bound model changes. The model that actually
            # answered is the ``model`` column next to it.
            model_kind=stage_key,
            active_prompt_versions_json=json.dumps(
                active_prompt_versions, ensure_ascii=False, sort_keys=True
            ),
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
            model_id=model.id,
            model=model.model,
            stage_key=stage_key,
        )


def _validate_prompt_test_payload(prompt_key: str, payload: dict[str, Any]) -> None:
    _validate_authoritative_payload(payload)


def _json_difference_paths(
    active: Any,
    draft: Any,
    *,
    prefix: str = "",
) -> list[str]:
    if type(active) is not type(draft):
        return [prefix or "payload"]
    if isinstance(active, dict):
        differences: list[str] = []
        for key in sorted(set(active) | set(draft)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in active or key not in draft:
                differences.append(path)
            else:
                differences.extend(
                    _json_difference_paths(active[key], draft[key], prefix=path)
                )
        return differences
    if isinstance(active, list):
        differences = []
        for index in range(max(len(active), len(draft))):
            path = f"{prefix}[{index}]"
            if index >= len(active) or index >= len(draft):
                differences.append(path)
            else:
                differences.extend(
                    _json_difference_paths(
                        active[index],
                        draft[index],
                        prefix=path,
                    )
                )
        return differences
    return [] if active == draft else [prefix or "payload"]


def _call_configured_model(
    *,
    model: AiModelConfig,
    system_prompt: str,
    context_text: str,
    raw_message: RawMessage,
    media_assets: list[MediaAsset],
    media_root: str | Path,
) -> dict[str, Any]:
    """Send the comparison through the same caller the stage uses live."""

    if not model.provider.is_configured:
        raise RuntimeError(f"model {model.id} is not configured")
    return _call_mimo_direct_model(
        raw_message=raw_message,
        media_assets=media_assets,
        model_config=model,
        prompt=system_prompt,
        media_root=media_root,
        context_text=context_text,
    )

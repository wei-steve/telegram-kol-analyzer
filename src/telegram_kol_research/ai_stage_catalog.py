"""Which parts of this system call an AI model, and what each one needs.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §2.1/§3.

The storage shape is three layers, after OpenMinis' ProviderInstance →
ModelEntry → usage binding:

* :class:`AiProvider` -- one OpenAI-compatible endpoint plus one key.
* :class:`AiModel` -- one model name served by a provider.
* ``AiRecognitionConfig.stages`` -- for each stage key below, an **ordered**
  list of model ids: the first is the one used, the rest are fallbacks.

This module deliberately holds no I/O and imports nothing from the rest of the
package, so the catalogue can be read by the Web layer, the router and the
config loader without an import cycle.

``runtime_incident_agent`` is intentionally absent: it keeps its own
fail-closed environment configuration (``llm_chat.load_runtime_agent_llm_config``)
and must not become steerable from a Web page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


#: Provider and model ids are stable keys that appear in stage bindings and in
#: stored audit rows, so they stay conservative -- but ``mimo-v2.5`` is an
#: existing id, so a dot has to be legal, and the v1 model list has always let
#: a person type the id, so upper case has to be too: rejecting ``Qwen-Max``
#: would turn somebody's existing configuration into a save that fails.
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Well-known hosts get a readable provider id; anything else is slugified.
PROVIDER_ID_BY_HOST: dict[str, str] = {
    "api.deepseek.com": "deepseek",
    "open.bigmodel.cn": "zhipu",
    "api.xiaomimimo.com": "mimo",
}

#: Display names for the three providers this project already talks to.
PROVIDER_LABEL_BY_ID: dict[str, str] = {
    "deepseek": "DeepSeek",
    "zhipu": "智谱",
    "mimo": "MiMo",
}


def default_provider_label(provider_id: str) -> str:
    key = str(provider_id or "")
    return PROVIDER_LABEL_BY_ID.get(key.split("-")[0], key)


def is_valid_slug(value: str) -> bool:
    return bool(_SLUG.fullmatch(str(value or "")))


def slugify(value: str, *, fallback: str = "custom") -> str:
    """A lowercase id made of ``[a-z0-9._-]``, never empty."""

    lowered = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-._")
    normalized = re.sub(r"-{2,}", "-", normalized)
    if not normalized or not normalized[0].isalnum():
        normalized = f"{fallback}-{normalized}".strip("-")
    return (normalized or fallback)[:64]


@dataclass(frozen=True, slots=True)
class AiProvider:
    """One OpenAI-compatible endpoint and the key used against it."""

    id: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 60.0
    enabled: bool = True
    #: Does this base URL still need a ``/v1`` appended -- "I only filled in
    #: the host" versus "this is already the API root". ``None`` means nobody
    #: has said, and :func:`ai_endpoints.infer_append_v1` decides, which is
    #: what every configuration written before this switch existed relies on.
    append_v1: bool | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url.strip())

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def api_key_last4(self) -> str:
        key = self.api_key.strip()
        return key[-4:] if len(key) >= 4 else ""


@dataclass(frozen=True, slots=True)
class AiModel:
    """One model name under a provider, with the capabilities it can serve.

    ``provider`` is the derived view the design asks for: the config layer
    binds the resolved :class:`AiProvider` onto every model it loads, so a
    caller holding a model never has to carry the provider table around. It is
    never serialised -- ``provider_id`` is what the YAML stores.
    """

    id: str
    provider_id: str
    model: str = ""
    label: str = ""
    supports_text: bool = True
    supports_image: bool = False
    enabled: bool = True
    provider: AiProvider | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class AiStageDefinition:
    """One place in this system that calls an AI model."""

    stage_key: str
    label: str
    description: str
    requires_text: bool = True
    requires_image: bool = False
    #: Does this stage run on the production message pipeline (worker/ingest).
    production_path: bool = False
    #: Verbatim "生产路径" note from the design's §2.1 table.
    production_note: str = ""
    #: Environment variables used when the stage has no chain bound.
    env_fallback: str = ""

    def supports(self, *, supports_text: bool, supports_image: bool) -> bool:
        """Can a model with these capabilities serve this stage."""

        if self.requires_text and not supports_text:
            return False
        if self.requires_image and not supports_image:
            return False
        return True

    @property
    def capability_label(self) -> str:
        if self.requires_text and self.requires_image:
            return "文本 + 图片"
        if self.requires_image:
            return "图片"
        return "文本"


AI_STAGE_DEFINITIONS: tuple[AiStageDefinition, ...] = (
    AiStageDefinition(
        stage_key="authoritative_recognition",
        label="单条消息权威识别（MiMo 多模态，v1 / v2 合同共用）",
        description="worker 主路径：每条新消息的权威识别，v1 与 v2 合同共用同一条链。",
        requires_text=True,
        requires_image=True,
        production_path=True,
        production_note="是，主路径",
    ),
    AiStageDefinition(
        stage_key="context_resolution",
        label="上下文结合分析（第二层）",
        description="权威识别判定需要更多上下文时调用；另有重分析队列使用同一条链。",
        requires_text=True,
        production_path=True,
        production_note="是（权威识别判定需要时调用；另有重分析队列）",
    ),
    AiStageDefinition(
        stage_key="semantic_review",
        label="语义分歧复核（只读顾问）",
        description="只读顾问：复核语义分歧，不改变任何交易判定。",
        requires_text=True,
        production_path=True,
        production_note="是（worker semantic_review 单例循环）",
    ),
    AiStageDefinition(
        stage_key="strategy_alert",
        label="策略提醒分类（Telegram 提醒 bot）",
        description="Telegram 提醒 bot 的策略提醒分类；未绑定时沿用环境变量。",
        requires_text=True,
        production_path=True,
        production_note="是，当 bot token 配置时",
        env_fallback="TELEGRAM_KOL_ALERT_LLM_MODEL / TELEGRAM_KOL_LLM_*",
    ),
    AiStageDefinition(
        stage_key="research_chat",
        label="Web 群消息问答",
        description="Web 页面里针对群消息的问答；未绑定时沿用环境变量。",
        requires_text=True,
        production_path=False,
        production_note="web",
        env_fallback="TELEGRAM_KOL_LLM_*",
    ),
    AiStageDefinition(
        stage_key="batch_text_recognition",
        label="离线/批量文本识别（V1 recognize_message_now，含生命周期事件 AI）",
        description="离线与批量工具的文本识别，不在生产消息管线上。",
        requires_text=True,
        production_path=False,
        production_note="否，只有 CLI / 批量工具",
    ),
    AiStageDefinition(
        stage_key="batch_image_recognition",
        label="离线/批量图片识别（V1；GLM-OCR 走 layout_parsing，其他走多模态 chat）",
        description="离线与批量工具的图片识别，不在生产消息管线上。",
        requires_text=False,
        requires_image=True,
        production_path=False,
        production_note="否，只有 CLI / 批量工具",
    ),
)

AI_STAGE_DEFINITIONS_BY_KEY: dict[str, AiStageDefinition] = {
    definition.stage_key: definition for definition in AI_STAGE_DEFINITIONS
}

AI_STAGE_KEYS: tuple[str, ...] = tuple(
    definition.stage_key for definition in AI_STAGE_DEFINITIONS
)

#: The stage whose chain head every other MiMo-facing check follows: the daily
#: probe, the provider-health derivation and the prompt centre's "mimo" test.
AUTHORITATIVE_STAGE = "authoritative_recognition"
CONTEXT_RESOLUTION_STAGE = "context_resolution"
SEMANTIC_REVIEW_STAGE = "semantic_review"
STRATEGY_ALERT_STAGE = "strategy_alert"
RESEARCH_CHAT_STAGE = "research_chat"
BATCH_TEXT_STAGE = "batch_text_recognition"
BATCH_IMAGE_STAGE = "batch_image_recognition"


def stage_definition(stage_key: str) -> AiStageDefinition | None:
    return AI_STAGE_DEFINITIONS_BY_KEY.get(str(stage_key or ""))


def provider_id_for_base_url(
    base_url: str,
    *,
    taken: Iterable[str] = (),
) -> str:
    """A stable, readable provider id for one endpoint.

    Known hosts keep their name; the same host appearing twice (a second key,
    a different timeout) gets ``-2``, ``-3`` ... as the design specifies.
    """

    text = str(base_url or "").strip()
    host = ""
    if text:
        without_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
        host = without_scheme.split("/", 1)[0].split("@")[-1].split(":")[0].lower()
    base = PROVIDER_ID_BY_HOST.get(host) or (slugify(host) if host else "custom")
    used = set(taken)
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"

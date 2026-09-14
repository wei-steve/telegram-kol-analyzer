"""Configuration for message-level AI strategy recognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
import logging
import warnings

import yaml

from telegram_kol_research.ai_endpoints import (
    chat_completions_url,
    infer_append_v1,
)
from telegram_kol_research.ai_stage_catalog import (
    AI_STAGE_DEFINITIONS,
    AI_STAGE_DEFINITIONS_BY_KEY,
    AI_STAGE_KEYS,
    AUTHORITATIVE_STAGE,
    AiModel,
    AiProvider,
    AiStageDefinition,
    default_provider_label,
    is_valid_slug,
    provider_id_for_base_url,
    slugify,
    stage_definition,
)


logger = logging.getLogger(__name__)

#: ``config/ai_recognition.yaml`` layout this module writes. A file without the
#: key is v1 and is migrated in memory on every load; nothing is written back
#: until someone saves, so a production file is not rewritten by a deploy.
AI_CONFIG_SCHEMA_VERSION = 2


class AiRecognitionConfigValidationError(ValueError):
    """A save was refused because the v2 structure is not self-consistent.

    Carries every problem found, so the Web layer can return one 422 listing
    all of them rather than making the user fix one per round trip.
    """

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(str(error) for error in errors)
        super().__init__("; ".join(self.errors) or "invalid AI configuration")




DEFAULT_RECOGNITION_PROMPT = """你是 Telegram 加密货币 KOL 消息的交易策略识别器。你的任务不是做行情分析，而是判断“这一条单独消息”是否包含可以进入自动化交易流程的明确策略。

请严格遵守：宁可判定为非策略，也不要把模糊内容误判为策略。

【必须判定为“是策略”的条件】
一条消息只有同时满足以下条件，才可以判定为“是策略”：
1. 明确出现交易标的，例如 BTC、ETH、SOL、DOGE、BNB，或中文别名如大饼、以太等。
2. 明确出现交易方向：多、做多、开多、long，或空、做空、开空、short。
3. 明确出现入场方式之一：具体入场价或入场区间；市价进场；到达某价格后进场；明确挂单区间。
4. 至少出现一个风险或退出要素：止损；止盈；无效价；保护价；分批止盈计划。
5. 这条消息表达的是“新开仓/新挂单/可执行入场”，而不是对已有仓位的复盘或管理。

【必须判定为“非策略”的情况】
以下情况即使出现多、空、BTC、ETH，也必须判定为“非策略”：
1. 只是行情观点、复盘、教学、情绪判断。
2. 只是提醒持有、继续拿、减仓、止盈、移动止损、保护价、补仓、别追、观望。
3. 只是说某个单子已经盈利、已经止盈、已经止损、已经错过。
4. 只是宣传、广告、联系方式、QQ、微信、群公告。
5. 只有方向但没有明确入场计划。
6. 只有价格但没有方向。
7. 只有止盈/止损更新，没有新的入场指令。
8. 视频消息默认不是策略。
9. 图片消息如果 OCR 内容不足以满足“是策略”的全部条件，则判定为“识别失败”或“非策略”，不要猜。

【特别注意】
- “多单继续持有”“空单继续持有”不是新策略。
- “设置好止损”“上推保护价”“分批止盈”通常是持仓管理，不是新策略。
- “不要逆势加仓”“趋势对的时候可以考虑盈利”是教学或建议，不是策略。
- 消息里出现 QQ、微信、联系方式，不代表 QQ 是交易标的。
- 不要把普通英文单词误认为币种；只有常见币种或明确带 USDT/币种上下文时才识别为标的。
- 如果消息像策略，但缺少关键字段，请判定为“非策略”或“识别失败”，不要补全、不要猜测。

【输出格式】
请只输出 JSON，不要输出解释文字：

{
  "recognition_result": "是策略 | 非策略 | 识别失败",
  "reason": "一句话说明原因",
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

【字段要求】
- 如果不是策略，strategy 内所有字段尽量为 null。
- confidence 范围 0 到 1。
- 只有满足全部“是策略”条件时，confidence 才能高于 0.7。
- 如果只是持仓管理或复盘，recognition_result 必须是“非策略”。
"""


NORMALIZED_STRATEGY_OUTPUT_INSTRUCTIONS = """
【策略字段统一格式】
- strategy.symbol 输出大写币种简称，例如 "BTC"、"ETH"。
- strategy.side 只能输出 "long" 或 "short"；中文做多/开多统一为 "long"，做空/开空统一为 "short"。
- strategy.entry 必须是字符串。单价保留原意，例如 "62400附近"；区间入场统一为 "62000-62500"；分批/多档入场用 "/" 分隔。
- strategy.order_type 只能输出 "market"、"limit" 或 "market+limit"。市价/现价/直接进场输出 "market"；限价/挂单/到价进场输出 "limit"；一部分市价先进、一部分挂单补仓输出 "market+limit"。
- 如果原文同时出现市价/现价入场和具体入场点位，例如 "Eth(市价进场)" 与 "进场点位：1730附近"，strategy.entry 必须输出 "市价进场/1730附近"，不能只输出 "市价进场"；strategy.order_type 必须输出 "market" 或 "market+limit"。
- strategy.stop_loss 必须是字符串。只输出止损价或无效价本身，不要输出解释性长句。
- strategy.take_profit 必须是字符串。单个止盈输出单价；分批止盈统一用 "/" 分隔，例如 "63600/64800/66000"。
- 不要把 entry、stop_loss、take_profit 输出成数组或对象；不要补全原文没有给出的价格。
"""


DEFAULT_LIFECYCLE_EVENT_PROMPT = """
你是 Telegram 加密货币 KOL 策略生命周期事件判定器。
你会收到：当前消息、同群最近的活跃策略列表、最近聊天上下文，以及可选的 reply_context。

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
- reply_context 是精确 Telegram 回复目标，不是普通的附近聊天上下文。当前消息明确表达取消且 reply_context.lifecycle_status 为 pending_entry 时，必须输出 cancel_entry，并使用 reply_context.lifecycle_id 作为唯一 target_lifecycle_id。
- 当前消息回复的 reply_context 若已是 entered，“取消/撤单”只表示原入场计划取消；不得自动转为 exit_position。输出 none 或低置信度并在 reason 说明需要人工处理。
- exit_position：当前消息是在关闭已 entered 策略，例如平仓、全平、离场、临时离场、止盈了、止损了、先出来、保本出局、成本附近保本出局、保本走、成本走、求稳可走、稳健者可走、breakeven exit。仅当当前消息能唯一对应一条已 entered 策略时，求稳可走/稳健者可走才是全平指令。
- position_update：当前消息是在管理已 entered 策略但没有完全离场，例如提前止盈一半、止盈一半、分批止盈30%、第一止盈位/第一个止盈位、按比例止盈、减仓一半、减仓30%、持仓收益达到100%后分批止盈、移动止损至成本价、止损移动到成本价、带保护、保护止损、上移止损、推保护、继续持有。“回成本了，注意保护成本，平加仓”表示减仓一半并将止损移至成本价，management_action 应输出 partial_take_profit, move_stop_to_protect。management_action 可输出 partial_take_profit、move_stop_to_protect、hold_update、risk_update。
- “第一止盈位 60950 移动止损至成本价”这类表达只是部分止盈并把止损推到成本保护，不是全量平仓/离场；必须判定为 position_update，不能判定为 exit_position。
- 如果当前消息明确调整止损价，请输出 stop_loss；明确调整止盈价或止盈计划，请输出 take_profit；只是“推保护/带保护/保本”但没有新价格时，management_action 输出 move_stop_to_protect。
- 临时入场、临时离场、部分止盈、调整止盈价、调整止损价都属于生命周期事件，不要当成新的 strategy。
- none：普通聊天、行情观点、广告、复盘、联系方式、无法确定目标策略、或只是识别新策略但不改变已有策略。
- 必须优先依据当前消息，不要把上下文里的旧消息当成当前动作。
- 如果只对应一个活跃策略，只输出 target_lifecycle_id，不要输出 targets。若当前消息明确列出多个独立标的且每个都能唯一对应活跃策略，请在非空 targets 中逐项输出 target_lifecycle_id、symbol、side；不要猜测或重复目标。
- 如果不能唯一对应，event_type 必须为 none 或 confidence 低于 0.7。
- confidence 低于 0.7 时，系统不会执行状态变更。
""".strip()


DEFAULT_MIMO_DIRECT_PROMPT = """
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


MARKET_ENTRY_WITH_PRICE_INSTRUCTION = (
    '- 如果原文同时出现市价/现价入场和具体入场点位，例如 "Eth(市价进场)" 与 '
    '"进场点位：1730附近"，strategy.entry 必须输出 "市价进场/1730附近"，不能只输出 '
    '"市价进场"；strategy.order_type 可单独输出 "market" 或 "market+limit"。'
)

REFERENCE_STRATEGY_INSTRUCTION = (
    '- 如果同一条消息已经给出完整的新开仓参数（标的、方向、入场区间或价格、止损、止盈），'
    '不要仅因为出现“可以考虑”“参考”“正常我不做单”等弱提示就判为非策略；'
    '只有明确要求用户不要进场、取消该单、已经错过入场，或只是在复盘既有仓位时，才判为非策略。'
    '如果图文消息的正文是“会员单盈利/已盈利/做个参考/复盘”等语境，而完整开仓参数主要来自图片或转发截图，'
    '应按历史策略截图或复盘参考处理为非策略，不要创建新的开仓策略。'
)

PRICE_SHORTHAND_NORMALIZATION_INSTRUCTION = """
- 价格简写必须按币种语境归一化为交易所绝对价格后再输出。尤其是 BTC/比特币：
  - 原文 "5.89-5.93附近"、"5.89万-5.93万"、"5.89-5.93w" 表示 "58900-59300"，不要输出 "5.89-5.93"。
  - 原文 "5.78" 表示 "57800"，例如止损 5.78 应输出 "57800"。
  - 原文 "6万/6.07/6.23" 表示 "60000/60700/62300"。
  - 对 BTC，如果价格数字明显低于当前 BTC 价格量级，且上下文是点位/入场/止盈/止损，应按“万位简写”理解。
  - ETH 等其他币种不要套用 BTC 万位规则，除非原文明确带 "万"。
- entry、stop_loss、take_profit、entry_price、exit_price 字段都必须遵守这个规则。
""".strip()


MIMO_AUTHORITATIVE_OUTPUT_INSTRUCTIONS = """
【MiMo 权威识别输出】
你必须同时完成“新开仓识别”和“已有策略生命周期事件识别”。这两个维度相互独立：
- 一条“出局/平仓/止盈/止损离场”消息不是新开仓，recognition_result 可以是“非策略”，同时 lifecycle_event.event_type 必须是 exit_position。
- 持仓管理消息不得因为“不是新开仓”而遗漏生命周期事件。
- 取消挂单/仓位管理和新开仓可以同时存在。必须把每个独立动作分别放入 instructions；不得因为识别到取消或管理动作而清空同消息中的完整新策略。
- instructions 内管理动作在前、新开仓在后，每个独立动作分别输出 confidence、reason、strategy 或 target。

图片与图文要求：
- 直接读取图片中可见的文字、表格、交易所截图、标注、箭头、标签和图表点位。
- 必须结合当前正文/caption 与图片整体判断，不要只看其中一个。
- 不要补全图片或文字中没有的币种、方向、价格、止损、止盈或关联策略。
- 图片模糊、裁切、遮挡、无法读取或内部矛盾时，输出识别失败或低置信度，禁止猜测。

只输出一个 JSON 对象：
{
  "instructions": [
    {
      "kind": "entry | cancel_pending_entry | replace_entry | full_exit | partial_exit | partial_take_profit | move_stop_to_protect | hold_update | risk_update",
      "confidence": 0.0,
      "reason": "当前消息中该独立动作的判断依据",
      "strategy": null,
      "target": {"lifecycle_id": null, "thread_id": null},
      "parameters": {}
    }
  ],
  "recognition_result": "是策略 | 非策略 | 识别失败",
  "reason": "当前消息的核心判断依据",
  "strategy": {
    "symbol": null,
    "side": null,
    "entry": null,
    "stop_loss": null,
    "take_profit": null,
    "leverage": null,
    "order_type": null
  },
  "lifecycle_event": {
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
    "reason": "生命周期判断依据"
  },
  "input_reading": {
    "observed_text": "从当前文字和图片中实际读到的关键内容",
    "image_quality": "clear | blurry | cropped | unreadable | none"
  },
  "confidence": 0.0
}
""".strip()


@dataclass(frozen=True)
class AiProviderConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = 60.0
    #: Carried from :class:`ai_stage_catalog.AiProvider` so the call sites that
    #: only ever see this flat shape still address the endpoint their provider
    #: asked for. ``None`` keeps the inferred rule
    #: (``ai_endpoints.infer_append_v1``), which is what a hand-built config and
    #: every file written before the switch existed rely on.
    append_v1: bool | None = field(default=None, compare=False)

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())


@dataclass(frozen=True)
class AiModelConfig:
    id: str
    label: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = 60.0
    supports_text: bool = True
    supports_image: bool = False
    #: The provider's ``/v1`` switch, carried down so a chain member knows how
    #: to address its own endpoint. Not part of equality: it describes how to
    #: reach the model, not which model it is.
    append_v1: bool | None = field(default=None, compare=False)

    @property
    def provider(self) -> AiProviderConfig:
        return AiProviderConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            append_v1=self.append_v1,
        )


@dataclass(frozen=True)
class AiRecognitionConfig:
    """AI settings in both shapes at once.

    ``providers`` / ``models`` / ``stages`` are the schema-v2 structure. The
    six fields below them are the v1 view every un-migrated call site still
    reads; :func:`load_ai_recognition_config` and
    :func:`save_ai_recognition_config` keep the two consistent, deriving the v1
    view from ``stages`` exactly as the design specifies (``text_provider`` and
    ``active_text_model_id`` from ``batch_text_recognition[0]``,
    ``image_provider`` / ``active_image_model_id`` from
    ``batch_image_recognition[0]``, ``context_resolution_model_id`` from
    ``context_resolution[0]``).

    They are left as plain fields rather than read-only properties on purpose:
    every existing construction site and every ``dataclasses.replace`` keeps
    working unchanged, which is what phase 1 promised. A hand-built config is
    therefore exactly what its caller passed, and derivation happens only where
    the file is read or written.
    """

    recognition_prompt: str = DEFAULT_RECOGNITION_PROMPT
    lifecycle_event_prompt: str = DEFAULT_LIFECYCLE_EVENT_PROMPT
    mimo_direct_prompt: str = DEFAULT_MIMO_DIRECT_PROMPT
    mode: str = "local_rule_parser"
    text_provider: AiProviderConfig = field(default_factory=AiProviderConfig)
    image_provider: AiProviderConfig = field(default_factory=AiProviderConfig)
    ai_models: list[AiModelConfig] = field(default_factory=list)
    active_text_model_id: str = ""
    active_image_model_id: str = ""
    context_resolution_model_id: str = ""
    providers: list[AiProvider] = field(default_factory=list)
    models: list[AiModel] = field(default_factory=list)
    stages: dict[str, list[str]] = field(default_factory=dict)
    #: What loading skipped and why. Never part of equality: two configs that
    #: describe the same models are the same config.
    config_warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def providers_by_id(self) -> dict[str, AiProvider]:
        return {provider.id: provider for provider in self.providers}

    @property
    def models_by_id(self) -> dict[str, AiModel]:
        return {model.id: model for model in self.models}

    def stage_chain(self, stage_key: str) -> list[AiModelConfig]:
        """The usable, ordered models bound to one stage (may be empty)."""

        return resolve_stage_models(self, stage_key)


def build_authoritative_mimo_prompt(config: AiRecognitionConfig) -> str:
    """Compose all text experience plus MiMo-only multimodal instructions."""

    sections = [
        config.recognition_prompt,
        NORMALIZED_STRATEGY_OUTPUT_INSTRUCTIONS,
        config.lifecycle_event_prompt,
        PRICE_SHORTHAND_NORMALIZATION_INSTRUCTION,
        config.mimo_direct_prompt,
        MIMO_AUTHORITATIVE_OUTPUT_INSTRUCTIONS,
    ]
    return "\n\n".join(section.strip() for section in sections if section.strip())


@dataclass(frozen=True)
class AiPromptDefinition:
    id: str
    field_name: str
    tag: str
    title: str
    description: str
    status_label: str = "在线生效"
    editable: bool = True


AI_PROMPT_DEFINITIONS = [
    AiPromptDefinition(
        id="recognition_prompt",
        field_name="recognition_prompt",
        tag="DeepSeek / 文本策略识别",
        title="单条消息是否为新开仓策略",
        description="文字消息直接使用；GLM-OCR 图片转文字后也会把 OCR 文本交给这个提示词判断。",
    ),
    AiPromptDefinition(
        id="lifecycle_event_prompt",
        field_name="lifecycle_event_prompt",
        tag="DeepSeek / 生命周期识别",
        title="入场、撤单、平仓、仓位管理事件",
        description="同群存在活跃策略时使用，用来判断当前消息是否改变已有策略状态。",
    ),
    AiPromptDefinition(
        id="mimo_direct_prompt",
        field_name="mimo_direct_prompt",
        tag="MiMo / 多模态直识别",
        title="文字和图片一起直接判断",
        description="用于 MiMo 多模态链路；直接发送文字和原图，不复用 GLM-OCR 结果。",
        status_label="在线生效",
    ),
]


def build_ai_prompt_views(config: AiRecognitionConfig) -> list[dict[str, Any]]:
    """Return prompt metadata and effective values for the Web prompt editor."""

    return [
        {
            "id": definition.id,
            "field_name": definition.field_name,
            "tag": definition.tag,
            "title": definition.title,
            "description": definition.description,
            "status_label": definition.status_label,
            "editable": definition.editable,
            "value": str(getattr(config, definition.field_name)),
        }
        for definition in AI_PROMPT_DEFINITIONS
    ]


DEFAULT_AI_MODELS = [
    AiModelConfig(
        id="deepseek-v4-flash",
        label="DeepSeek V4 Flash",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        supports_text=True,
        supports_image=False,
    ),
    AiModelConfig(
        id="glm-ocr",
        label="GLM-OCR",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-ocr",
        supports_text=False,
        supports_image=True,
    ),
    AiModelConfig(
        id="mimo-v2.5",
        label="MiMo V2.5",
        base_url="https://api.xiaomimimo.com/v1",
        model="mimo-v2.5",
        supports_text=True,
        supports_image=True,
    ),
]


#: The v1 ``_find_mimo_model`` rule, kept here so migration reproduces exactly
#: what production does today rather than what the old selection page said.
MIMO_AUTHORITATIVE_MODEL_NAME = "mimo-v2.5"


def _bind_model(model: AiModel, provider: AiProvider) -> AiModelConfig:
    """Join one provider and one model into the flat runtime shape.

    :class:`AiModelConfig` stays the type every caller and the provider client
    already speak, so nothing downstream has to learn the two-layer storage.
    """

    return AiModelConfig(
        id=model.id,
        label=model.label or model.id,
        base_url=provider.base_url,
        api_key=provider.api_key,
        model=model.model,
        timeout_seconds=provider.timeout_seconds,
        supports_text=model.supports_text,
        supports_image=model.supports_image,
        append_v1=provider.append_v1,
    )


def resolve_stage_models(
    config: AiRecognitionConfig,
    stage_key: str,
) -> list[AiModelConfig]:
    """The ordered models one stage can actually call right now.

    Membership is stored; routability is decided here. A member whose model or
    provider is disabled, or whose provider has no endpoint, stays bound (so a
    temporary disable does not lose the binding) but is skipped -- the same
    meaning as OpenMinis' ``availableEntryIds``. An empty result means "this
    stage has no usable model", which every call site must already handle
    because that is what an unconfigured provider has always meant.
    """

    definition = stage_definition(stage_key)
    # ``getattr`` rather than the properties: several call sites hand this a
    # stand-in config object that only carries the v1 fields, and a stage
    # lookup against one of those has to answer "nothing bound", not raise.
    providers = {
        provider.id: provider for provider in getattr(config, "providers", ()) or ()
    }
    models = {model.id: model for model in getattr(config, "models", ()) or ()}
    stages = getattr(config, "stages", None) or {}
    resolved: list[AiModelConfig] = []
    seen: set[str] = set()
    for model_id in stages.get(str(stage_key or ""), ()):
        model = models.get(str(model_id))
        if model is None or not model.enabled or model.id in seen:
            continue
        provider = providers.get(model.provider_id)
        if provider is None or not provider.enabled:
            continue
        if definition is not None and not definition.supports(
            supports_text=model.supports_text,
            supports_image=model.supports_image,
        ):
            continue
        bound = _bind_model(model, provider)
        if not bound.provider.is_configured:
            continue
        seen.add(model.id)
        resolved.append(bound)
    return resolved


def _normalize_provider(provider: AiProvider) -> AiProvider:
    base_url = str(provider.base_url or "").strip().rstrip("/")
    return AiProvider(
        id=str(provider.id or "").strip(),
        label=str(provider.label or "").strip(),
        base_url=base_url,
        api_key=str(provider.api_key or "").strip(),
        timeout_seconds=float(provider.timeout_seconds or 60),
        enabled=bool(provider.enabled),
        # Write the guess down. A provider loaded from a file that predates the
        # switch keeps producing the URL it always produced, and stops being a
        # guess the moment somebody saves.
        append_v1=(
            bool(provider.append_v1)
            if provider.append_v1 is not None
            else infer_append_v1(base_url)
        ),
    )


def _normalize_model(model: AiModel) -> AiModel:
    model_id = str(model.id or "").strip()
    return AiModel(
        id=model_id,
        provider_id=str(model.provider_id or "").strip(),
        model=str(model.model or "").strip(),
        label=str(model.label or "").strip() or model_id,
        supports_text=bool(model.supports_text),
        supports_image=bool(model.supports_image),
        enabled=bool(model.enabled),
    )


def normalize_ai_config_v2(
    providers: Iterable[AiProvider],
    models: Iterable[AiModel],
    stages: dict[str, Iterable[str]] | None,
) -> tuple[
    list[AiProvider],
    list[AiModel],
    dict[str, list[str]],
    list[str],
    list[str],
]:
    """Normalize one v2 structure; report what was dropped and what was wrong.

    Returns ``(providers, models, stages, warnings, errors)``. Loading applies
    the warnings and keeps going (a half-broken file must not stop recognition
    from starting); saving turns ``errors`` into a 422 so the page says what to
    fix. Every dropped member appears in ``warnings`` either way.
    """

    warning_list: list[str] = []
    error_list: list[str] = []

    normalized_providers: list[AiProvider] = []
    provider_ids: set[str] = set()
    for provider in providers:
        item = _normalize_provider(provider)
        if not is_valid_slug(item.id):
            error_list.append(f"provider id is not a valid slug: {item.id!r}")
            warning_list.append(f"skipped provider with invalid id {item.id!r}")
            continue
        if item.id in provider_ids:
            error_list.append(f"duplicate provider id: {item.id!r}")
            warning_list.append(f"skipped duplicate provider {item.id!r}")
            continue
        provider_ids.add(item.id)
        normalized_providers.append(item)
    providers_by_id = {item.id: item for item in normalized_providers}

    normalized_models: list[AiModel] = []
    model_ids: set[str] = set()
    for model in models:
        item = _normalize_model(model)
        if not is_valid_slug(item.id):
            error_list.append(f"model id is not a valid slug: {item.id!r}")
            warning_list.append(f"skipped model with invalid id {item.id!r}")
            continue
        if item.id in model_ids:
            error_list.append(f"duplicate model id: {item.id!r}")
            warning_list.append(f"skipped duplicate model {item.id!r}")
            continue
        provider = providers_by_id.get(item.provider_id)
        if provider is None:
            error_list.append(
                f"model {item.id!r} references unknown provider "
                f"{item.provider_id!r}"
            )
            warning_list.append(
                f"skipped model {item.id!r}: provider {item.provider_id!r} is unknown"
            )
            continue
        model_ids.add(item.id)
        normalized_models.append(
            AiModel(
                id=item.id,
                provider_id=item.provider_id,
                model=item.model,
                label=item.label,
                supports_text=item.supports_text,
                supports_image=item.supports_image,
                enabled=item.enabled,
                provider=provider,
            )
        )
    models_by_id = {item.id: item for item in normalized_models}

    raw_stages = dict(stages or {})
    for stage_key in raw_stages:
        if stage_key not in AI_STAGE_DEFINITIONS_BY_KEY:
            warning_list.append(f"dropped unknown stage {stage_key!r}")
    normalized_stages: dict[str, list[str]] = {}
    for definition in AI_STAGE_DEFINITIONS:
        chain: list[str] = []
        for raw_id in raw_stages.get(definition.stage_key, ()) or ():
            model_id = str(raw_id or "").strip()
            if not model_id or model_id in chain:
                continue
            model = models_by_id.get(model_id)
            if model is None:
                warning_list.append(
                    f"stage {definition.stage_key}: dropped unknown model "
                    f"{model_id!r}"
                )
                continue
            if not definition.supports(
                supports_text=model.supports_text,
                supports_image=model.supports_image,
            ):
                error_list.append(
                    f"stage {definition.stage_key} requires "
                    f"{definition.capability_label}; model {model_id!r} cannot serve it"
                )
                warning_list.append(
                    f"stage {definition.stage_key}: dropped {model_id!r}, it does not "
                    f"support {definition.capability_label}"
                )
                continue
            if not model.enabled:
                warning_list.append(
                    f"stage {definition.stage_key}: {model_id!r} is bound but disabled"
                )
            elif model.provider is not None and not model.provider.enabled:
                warning_list.append(
                    f"stage {definition.stage_key}: {model_id!r} is bound but its "
                    f"provider {model.provider_id!r} is disabled"
                )
            elif model.provider is not None and not model.provider.is_configured:
                warning_list.append(
                    f"stage {definition.stage_key}: {model_id!r} is bound but its "
                    f"provider {model.provider_id!r} has no base_url"
                )
            chain.append(model_id)
        normalized_stages[definition.stage_key] = chain
    return (
        normalized_providers,
        normalized_models,
        normalized_stages,
        warning_list,
        error_list,
    )


def migrate_v1_ai_config(
    ai_models: Sequence[AiModelConfig],
    *,
    active_text_model_id: str = "",
    active_image_model_id: str = "",
    context_resolution_model_id: str = "",
) -> tuple[list[AiProvider], list[AiModel], dict[str, list[str]]]:
    """Turn the flat v1 model list into providers, models and stage chains.

    Pure and idempotent: nothing is read or written, and re-running it on the
    v1 view derived from its own output gives the same answer.

    The ids passed in must already be the **resolved** ones (what
    ``_select_active_model`` picked), not the raw YAML strings, so the stages
    describe what production actually does today rather than what the old page
    displayed. ``authoritative_recognition`` follows
    ``recognition_experiments._find_mimo_model`` -- the entry whose id or model
    name is ``mimo-v2.5`` -- because that, not ``active_image_model_id``, is
    the model the production path has been calling.

    Providers are deduplicated by ``(base_url, api_key, timeout_seconds)``. The
    design says ``(base_url, api_key)``; the timeout is included because two
    v1 entries on one endpoint with different timeouts would otherwise come
    back from a round trip with a timeout they never had, and a silent change
    to a request deadline is exactly the kind of thing this project does not
    do quietly.
    """

    providers: list[AiProvider] = []
    provider_id_by_key: dict[tuple[str, str, float], str] = {}
    models: list[AiModel] = []
    model_id_by_source: dict[str, str] = {}
    for entry in ai_models:
        normalized = _normalize_model_config(entry)
        if not normalized.id:
            continue
        model_id = _migrated_model_id(
            normalized.id, taken=model_id_by_source.values()
        )
        model_id_by_source[normalized.id] = model_id
        key = (normalized.base_url, normalized.api_key, normalized.timeout_seconds)
        provider_id = provider_id_by_key.get(key)
        if provider_id is None:
            provider_id = provider_id_for_base_url(
                normalized.base_url,
                taken=provider_id_by_key.values(),
            )
            provider_id_by_key[key] = provider_id
            providers.append(
                AiProvider(
                    id=provider_id,
                    label=default_provider_label(provider_id),
                    base_url=normalized.base_url,
                    api_key=normalized.api_key,
                    timeout_seconds=normalized.timeout_seconds,
                    enabled=True,
                )
            )
        models.append(
            AiModel(
                id=model_id,
                provider_id=provider_id,
                model=normalized.model,
                label=normalized.label or normalized.id,
                supports_text=normalized.supports_text,
                supports_image=normalized.supports_image,
                enabled=True,
            )
        )

    known = {model.id for model in models}

    def _chain(source_id: str) -> list[str]:
        model_id = model_id_by_source.get(str(source_id or "").strip(), "")
        return [model_id] if model_id and model_id in known else []

    mimo_model_id = ""
    for entry in ai_models:
        if (
            entry.id == MIMO_AUTHORITATIVE_MODEL_NAME
            or entry.model == MIMO_AUTHORITATIVE_MODEL_NAME
        ):
            mimo_model_id = entry.id.strip()
            break
    text_id = str(active_text_model_id or "").strip()
    stages = {
        AUTHORITATIVE_STAGE: _chain(mimo_model_id),
        "context_resolution": _chain(
            str(context_resolution_model_id or "").strip() or text_id
        ),
        "semantic_review": _chain(text_id),
        "strategy_alert": [],
        "research_chat": [],
        "batch_text_recognition": _chain(text_id),
        "batch_image_recognition": _chain(
            str(active_image_model_id or "").strip()
        ),
    }
    return providers, models, stages


def stage_head_provider(
    config: AiRecognitionConfig,
    stage_key: str,
    *,
    legacy: AiProviderConfig | None = None,
) -> AiProviderConfig:
    """The provider one stage starts with, with the v1 field as the fallback.

    Used by the stages that only ever make a single attempt (the batch tools,
    the prompt centre's test runs): they need the head, not the chain. A
    configuration with no v2 model table keeps whatever the v1 field said, so
    a hand-built config behaves exactly as it did.
    """

    chain = resolve_stage_models(config, stage_key)
    if chain:
        return chain[0].provider
    return legacy if legacy is not None else AiProviderConfig()


def _migrated_model_id(source_id: str, *, taken: Iterable[str]) -> str:
    """Keep an id that is already a usable key; repair one that is not.

    The v1 model list never validated its ids -- the Web form writes whatever
    was typed -- and a stage binding is only a stable key if the id is one.
    Every id in use today passes unchanged; an exotic one is slugified rather
    than dropped, because dropping it would delete the model.
    """

    if is_valid_slug(source_id):
        return source_id
    base = slugify(source_id, fallback="model")
    used = set(taken)
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def _derive_v1_view(
    providers: list[AiProvider],
    models: list[AiModel],
    stages: dict[str, list[str]],
    *,
    fallback_text_provider: AiProviderConfig,
    fallback_image_provider: AiProviderConfig,
) -> dict[str, Any]:
    """The v1 fields, read off the v2 structure (design §3).

    A stage with no usable model leaves its v1 field alone: that is exactly
    what "no provider configured" has always looked like to the call sites.
    """

    probe = AiRecognitionConfig(providers=providers, models=models, stages=stages)
    text_chain = resolve_stage_models(probe, "batch_text_recognition")
    image_chain = resolve_stage_models(probe, "batch_image_recognition")
    context_chain = resolve_stage_models(probe, "context_resolution")
    ai_models = [
        _bind_model(model, model.provider)
        for model in models
        if model.provider is not None
    ]
    return {
        "ai_models": ai_models,
        "text_provider": (
            text_chain[0].provider if text_chain else fallback_text_provider
        ),
        "image_provider": (
            image_chain[0].provider if image_chain else fallback_image_provider
        ),
        "active_text_model_id": text_chain[0].id if text_chain else "",
        "active_image_model_id": image_chain[0].id if image_chain else "",
        "context_resolution_model_id": context_chain[0].id if context_chain else "",
    }


def load_ai_recognition_config(config_path: str | Path) -> AiRecognitionConfig:
    """Load AI recognition settings, falling back to conservative defaults."""

    path = Path(config_path)
    if not path.exists():
        ai_models = _normalize_ai_models(
            [],
            text_provider=AiProviderConfig(),
            image_provider=AiProviderConfig(),
        )
        text_model = next(
            model for model in ai_models if model.id == "deepseek-v4-flash"
        )
        image_model = next(model for model in ai_models if model.id == "mimo-v2.5")
        providers, models, stages = migrate_v1_ai_config(
            ai_models,
            active_text_model_id=text_model.id,
            active_image_model_id=image_model.id,
            context_resolution_model_id=text_model.id,
        )
        return AiRecognitionConfig(
            recognition_prompt=_with_price_shorthand_instruction(DEFAULT_RECOGNITION_PROMPT),
            lifecycle_event_prompt=_with_lifecycle_event_instructions(
                DEFAULT_LIFECYCLE_EVENT_PROMPT
            ),
            mimo_direct_prompt=_with_mimo_direct_instructions(DEFAULT_MIMO_DIRECT_PROMPT),
            ai_models=ai_models,
            active_text_model_id=text_model.id,
            active_image_model_id=image_model.id,
            context_resolution_model_id=text_model.id,
            providers=providers,
            models=models,
            stages=stages,
        )

    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw_data, dict):
        return AiRecognitionConfig()
    if any(
        key in raw_data
        for key in (
            "recognition_prompt",
            "lifecycle_event_prompt",
            "mimo_direct_prompt",
        )
    ):
        warnings.warn(
            "YAML AI prompt fields are deprecated seed inputs; runtime prompts come from the database registry.",
            DeprecationWarning,
            stacklevel=2,
        )

    recognition_prompt = _with_price_shorthand_instruction(
        _with_normalized_strategy_output_instructions(
            str(raw_data.get("recognition_prompt") or DEFAULT_RECOGNITION_PROMPT)
        )
    )
    lifecycle_event_prompt = _with_lifecycle_event_instructions(
        str(raw_data.get("lifecycle_event_prompt") or DEFAULT_LIFECYCLE_EVENT_PROMPT)
    )
    mimo_direct_prompt = _with_price_shorthand_instruction(
        _with_mimo_direct_instructions(
            str(raw_data.get("mimo_direct_prompt") or DEFAULT_MIMO_DIRECT_PROMPT)
        )
    )
    mode = str(raw_data.get("mode") or "local_rule_parser")
    raw_text_provider = _load_provider_config(raw_data.get("text_provider"))
    raw_image_provider = _load_provider_config(raw_data.get("image_provider"))
    if _schema_version(raw_data) >= 2:
        return _load_v2_config(
            raw_data,
            recognition_prompt=recognition_prompt,
            lifecycle_event_prompt=lifecycle_event_prompt,
            mimo_direct_prompt=mimo_direct_prompt,
            mode=mode,
            fallback_text_provider=raw_text_provider,
            fallback_image_provider=raw_image_provider,
        )
    ai_models = _load_ai_models(
        raw_data.get("ai_models"),
        text_provider=raw_text_provider,
        image_provider=raw_image_provider,
    )
    active_text_model_id = str(raw_data.get("active_text_model_id") or "")
    active_image_model_id = str(raw_data.get("active_image_model_id") or "")
    text_model = _select_active_model(
        ai_models,
        active_text_model_id,
        supports="text",
        fallback_provider=raw_text_provider,
    )
    image_model = _select_active_model(
        ai_models,
        active_image_model_id,
        supports="image",
        fallback_provider=raw_image_provider,
    )
    context_resolution_model = _select_active_model(
        ai_models,
        str(raw_data.get("context_resolution_model_id") or ""),
        supports="text",
        fallback_provider=(text_model.provider if text_model else raw_text_provider),
    )
    providers, models, stages = migrate_v1_ai_config(
        ai_models,
        active_text_model_id=text_model.id if text_model else "",
        active_image_model_id=image_model.id if image_model else "",
        context_resolution_model_id=(
            context_resolution_model.id if context_resolution_model else ""
        ),
    )
    return AiRecognitionConfig(
        recognition_prompt=recognition_prompt,
        lifecycle_event_prompt=lifecycle_event_prompt,
        mimo_direct_prompt=mimo_direct_prompt,
        mode=mode,
        text_provider=text_model.provider if text_model else raw_text_provider,
        image_provider=image_model.provider if image_model else raw_image_provider,
        ai_models=ai_models,
        active_text_model_id=text_model.id if text_model else "",
        active_image_model_id=image_model.id if image_model else "",
        context_resolution_model_id=(
            context_resolution_model.id if context_resolution_model else ""
        ),
        providers=providers,
        models=models,
        stages=stages,
    )


def _schema_version(raw_data: dict[str, Any]) -> int:
    try:
        return int(raw_data.get("schema_version") or 1)
    except (TypeError, ValueError):
        return 1


def _provider_from_payload(value: Any) -> AiProvider:
    data = value if isinstance(value, dict) else {}
    append_v1 = data.get("append_v1")
    return AiProvider(
        id=str(data.get("id") or ""),
        label=str(data.get("label") or ""),
        base_url=str(data.get("base_url") or ""),
        api_key=str(data.get("api_key") or ""),
        timeout_seconds=float(data.get("timeout_seconds") or 60),
        enabled=bool(data.get("enabled", True)),
        append_v1=None if append_v1 is None else bool(append_v1),
    )


def _model_from_payload(value: Any) -> AiModel:
    data = value if isinstance(value, dict) else {}
    return AiModel(
        id=str(data.get("id") or data.get("model") or ""),
        provider_id=str(data.get("provider_id") or ""),
        model=str(data.get("model") or ""),
        label=str(data.get("label") or ""),
        supports_text=bool(data.get("supports_text", True)),
        supports_image=bool(data.get("supports_image", False)),
        enabled=bool(data.get("enabled", True)),
    )


def _stages_from_payload(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    stages: dict[str, list[str]] = {}
    for key, members in value.items():
        if isinstance(members, str):
            members = [members]
        if not isinstance(members, (list, tuple)):
            continue
        stages[str(key)] = [str(item) for item in members]
    return stages


def _load_v2_config(
    raw_data: dict[str, Any],
    *,
    recognition_prompt: str,
    lifecycle_event_prompt: str,
    mimo_direct_prompt: str,
    mode: str,
    fallback_text_provider: AiProviderConfig,
    fallback_image_provider: AiProviderConfig,
) -> AiRecognitionConfig:
    """Read a schema-v2 file. Broken members are skipped, never fatal."""

    raw_providers = raw_data.get("providers")
    raw_models = raw_data.get("models")
    providers, models, stages, warning_list, _errors = normalize_ai_config_v2(
        [
            _provider_from_payload(item)
            for item in (raw_providers if isinstance(raw_providers, list) else [])
        ],
        [
            _model_from_payload(item)
            for item in (raw_models if isinstance(raw_models, list) else [])
        ],
        _stages_from_payload(raw_data.get("stages")),
    )
    for message in warning_list:
        logger.warning("ai_recognition config: %s", message)
    derived = _derive_v1_view(
        providers,
        models,
        stages,
        fallback_text_provider=fallback_text_provider,
        fallback_image_provider=fallback_image_provider,
    )
    return AiRecognitionConfig(
        recognition_prompt=recognition_prompt,
        lifecycle_event_prompt=lifecycle_event_prompt,
        mimo_direct_prompt=mimo_direct_prompt,
        mode=mode,
        providers=providers,
        models=models,
        stages=stages,
        config_warnings=tuple(warning_list),
        **derived,
    )


#: The three stages a v1-shaped caller can still name, and the v1 field that
#: names each one.
_LEGACY_STAGE_FIELDS = (
    ("batch_text_recognition", "active_text_model_id"),
    ("batch_image_recognition", "active_image_model_id"),
    ("context_resolution", "context_resolution_model_id"),
)


def _promote_legacy_heads(
    stages: dict[str, list[str]],
    *,
    models: list[AiModel],
    heads: dict[str, str],
) -> dict[str, list[str]]:
    """Let an explicit v1 field decide its stage's head, keeping the tail.

    ``dataclasses.replace(config, context_resolution_model_id=...)`` is how
    ``context_authority_cutover`` changes a model, and the old
    ``POST /api/ai-recognition-config`` form works the same way. Neither can
    express a chain, so the head they name is moved to the front and the
    fallbacks already configured stay behind it instead of being erased.
    """

    models_by_id = {model.id: model for model in models}
    merged = {key: list(value) for key, value in stages.items()}
    for stage_key, head in heads.items():
        head = str(head or "").strip()
        if not head:
            continue
        model = models_by_id.get(head)
        definition = stage_definition(stage_key)
        if model is None or definition is None:
            continue
        if not definition.supports(
            supports_text=model.supports_text,
            supports_image=model.supports_image,
        ):
            continue
        chain = merged.get(stage_key, [])
        if chain[:1] == [head]:
            continue
        merged[stage_key] = [head] + [item for item in chain if item != head]
    return merged


def _preserved_stage_chains(
    path: Path,
    stages: dict[str, list[str]],
    *,
    models: list[AiModel],
) -> dict[str, list[str]]:
    """Keep chains the caller had no way to express.

    A v1-shaped save carries no ``stages``, so every chain is rebuilt from the
    v1 fields -- which name at most one model each. Without this, saving the
    old "AI配置" form once would silently delete every fallback a user had
    configured on the new page. So for each stage, the chain already on disk
    is kept when it starts with the same model the rebuild chose, and it is
    kept whole when the rebuild produced nothing at all.
    """

    if not path.exists():
        return stages
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return stages
    if not isinstance(raw, dict) or _schema_version(raw) < 2:
        return stages
    known = {model.id for model in models}
    existing = _stages_from_payload(raw.get("stages"))
    merged = {key: list(value) for key, value in stages.items()}
    for stage_key, chain in merged.items():
        previous = [
            item
            for item in existing.get(stage_key, [])
            if item in known
        ]
        if not previous:
            continue
        if not chain:
            merged[stage_key] = previous
        elif previous[:1] == chain[:1] and len(previous) > len(chain):
            merged[stage_key] = previous
    return merged


def save_ai_recognition_config(
    config_path: str | Path,
    config: AiRecognitionConfig,
) -> AiRecognitionConfig:
    """Persist AI recognition settings and return the normalized config.

    Always writes schema v2. The v1 keys are written too, as a derived mirror:
    they cost nothing, they keep ``config/ai_recognition.example.yaml`` and
    every reader of the raw file working, and they mean a rollback to code
    that predates v2 finds a configuration it can still read instead of an
    empty one.
    """

    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if config.models:
        normalized = _save_from_v2(config)
    else:
        normalized = _save_from_v1(path, config)
    payload: dict[str, Any] = {
        "schema_version": AI_CONFIG_SCHEMA_VERSION,
        "mode": normalized.mode,
        "recognition_prompt": normalized.recognition_prompt,
        "lifecycle_event_prompt": normalized.lifecycle_event_prompt,
        "mimo_direct_prompt": normalized.mimo_direct_prompt,
        "providers": [
            _provider_v2_to_payload(provider) for provider in normalized.providers
        ],
        "models": [_model_v2_to_payload(model) for model in normalized.models],
        "stages": {
            stage_key: list(normalized.stages.get(stage_key, []))
            for stage_key in AI_STAGE_KEYS
        },
        "active_text_model_id": normalized.active_text_model_id,
        "active_image_model_id": normalized.active_image_model_id,
        "context_resolution_model_id": normalized.context_resolution_model_id,
        "ai_models": [_model_to_payload(model) for model in normalized.ai_models],
        "text_provider": _provider_to_payload(normalized.text_provider),
        "image_provider": _provider_to_payload(normalized.image_provider),
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return normalized


def _normalized_prompts(config: AiRecognitionConfig) -> dict[str, str]:
    return {
        "recognition_prompt": _with_price_shorthand_instruction(
            _with_normalized_strategy_output_instructions(
                config.recognition_prompt.strip() or DEFAULT_RECOGNITION_PROMPT
            )
        ),
        "lifecycle_event_prompt": _with_lifecycle_event_instructions(
            config.lifecycle_event_prompt.strip() or DEFAULT_LIFECYCLE_EVENT_PROMPT
        ),
        "mimo_direct_prompt": _with_price_shorthand_instruction(
            _with_mimo_direct_instructions(
                config.mimo_direct_prompt.strip() or DEFAULT_MIMO_DIRECT_PROMPT
            )
        ),
    }


def _save_from_v1(
    path: Path,
    config: AiRecognitionConfig,
) -> AiRecognitionConfig:
    """Normalize a config that only carries the v1 fields (unchanged rules)."""

    ai_models = _normalize_ai_models(
        config.ai_models,
        text_provider=config.text_provider,
        image_provider=config.image_provider,
    )
    text_model = _select_active_model(
        ai_models,
        config.active_text_model_id,
        supports="text",
        fallback_provider=config.text_provider,
    )
    image_model = _select_active_model(
        ai_models,
        config.active_image_model_id,
        supports="image",
        fallback_provider=config.image_provider,
    )
    context_resolution_model = _select_active_model(
        ai_models,
        config.context_resolution_model_id,
        supports="text",
        fallback_provider=(text_model.provider if text_model else config.text_provider),
    )
    raw_providers, raw_models, raw_stages = migrate_v1_ai_config(
        ai_models,
        active_text_model_id=text_model.id if text_model else "",
        active_image_model_id=image_model.id if image_model else "",
        context_resolution_model_id=(
            context_resolution_model.id if context_resolution_model else ""
        ),
    )
    raw_stages = _preserved_stage_chains(path, raw_stages, models=raw_models)
    providers, models, stages, warning_list, _errors = normalize_ai_config_v2(
        raw_providers, raw_models, raw_stages
    )
    # Deliberately lenient, unlike the v2 path: this caller has only the v1
    # fields, so it cannot describe -- or fix -- a stage binding. Refusing its
    # save would turn "the model list has an entry that cannot serve the
    # authoritative stage" into a 500 on a form that has worked for months.
    # The offending binding is dropped and logged; the save goes through.
    for message in warning_list:
        logger.warning("ai_recognition save: %s", message)
    return AiRecognitionConfig(
        **_normalized_prompts(config),
        mode=_resolve_mode(config),
        text_provider=(
            text_model.provider
            if text_model
            else _normalize_provider_config(config.text_provider)
        ),
        image_provider=(
            image_model.provider
            if image_model
            else _normalize_provider_config(config.image_provider)
        ),
        ai_models=ai_models,
        active_text_model_id=text_model.id if text_model else "",
        active_image_model_id=image_model.id if image_model else "",
        context_resolution_model_id=(
            context_resolution_model.id if context_resolution_model else ""
        ),
        providers=providers,
        models=models,
        stages=stages,
        config_warnings=tuple(warning_list),
    )


def _save_from_v2(config: AiRecognitionConfig) -> AiRecognitionConfig:
    """Normalize a config whose v2 structure is what the caller edited."""

    providers, models, stages, warning_list, errors = normalize_ai_config_v2(
        config.providers, config.models, config.stages
    )
    if errors:
        raise AiRecognitionConfigValidationError(errors)
    stages = _promote_legacy_heads(
        stages,
        models=models,
        heads={
            stage_key: getattr(config, field_name)
            for stage_key, field_name in _LEGACY_STAGE_FIELDS
        },
    )
    derived = _derive_v1_view(
        providers,
        models,
        stages,
        fallback_text_provider=_normalize_provider_config(config.text_provider),
        fallback_image_provider=_normalize_provider_config(config.image_provider),
    )
    return AiRecognitionConfig(
        **_normalized_prompts(config),
        mode=_resolve_mode(config),
        providers=providers,
        models=models,
        stages=stages,
        config_warnings=tuple(warning_list),
        **derived,
    )


def _provider_v2_to_payload(provider: AiProvider) -> dict[str, Any]:
    return {
        "id": provider.id,
        "label": provider.label,
        "base_url": provider.base_url,
        "api_key": provider.api_key,
        "timeout_seconds": provider.timeout_seconds,
        "enabled": provider.enabled,
        "append_v1": (
            provider.append_v1
            if provider.append_v1 is not None
            else infer_append_v1(provider.base_url)
        ),
    }


def _model_v2_to_payload(model: AiModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "provider_id": model.provider_id,
        "model": model.model,
        "label": model.label,
        "supports_text": model.supports_text,
        "supports_image": model.supports_image,
        "enabled": model.enabled,
    }


def build_ai_config_view(config: AiRecognitionConfig) -> dict[str, Any]:
    """The whole v2 structure with keys masked (CLI ``ai-config-show``, Web).

    An API key is write-only everywhere in this project: a reader is told
    whether one is set and its last four characters, never the key.
    """

    providers = [
        {
            "id": provider.id,
            "label": provider.label or provider.id,
            "base_url": provider.base_url,
            "timeout_seconds": provider.timeout_seconds,
            "enabled": provider.enabled,
            "api_key_configured": provider.api_key_configured,
            "api_key_last4": provider.api_key_last4,
            "append_v1": (
                provider.append_v1
                if provider.append_v1 is not None
                else infer_append_v1(provider.base_url)
            ),
            # What this provider will actually be asked, so the page's own
            # preview can be checked against the server rather than trusted.
            "chat_completions_url": chat_completions_url(
                provider.base_url, provider.append_v1
            ),
        }
        for provider in config.providers
    ]
    models = [
        {
            "id": model.id,
            "provider_id": model.provider_id,
            "model": model.model,
            "label": model.label or model.id,
            "supports_text": model.supports_text,
            "supports_image": model.supports_image,
            "enabled": model.enabled,
        }
        for model in config.models
    ]
    # "Could this model be called at all", independent of any stage: enabled,
    # on an enabled provider, with an endpoint. The page needs it to tell a
    # member that will not route from one that simply is not saved yet -- the
    # per-stage ``effective`` list only names members already bound.
    routable_model_ids = [
        model.id
        for model in config.models
        if model.enabled
        and model.provider is not None
        and model.provider.enabled
        and model.provider.is_configured
    ]
    definitions = []
    effective: dict[str, list[dict[str, Any]]] = {}
    for definition in AI_STAGE_DEFINITIONS:
        definitions.append(
            {
                "stage_key": definition.stage_key,
                "label": definition.label,
                "description": definition.description,
                "requires_text": definition.requires_text,
                "requires_image": definition.requires_image,
                "capability_label": definition.capability_label,
                "production_path": definition.production_path,
                "production_note": definition.production_note,
                "env_fallback": definition.env_fallback,
            }
        )
        effective[definition.stage_key] = [
            {
                "id": model.id,
                "label": model.label or model.id,
                "model": model.model,
                "base_url": model.base_url,
                "role": "主用" if index == 0 else f"备用 {index}",
            }
            for index, model in enumerate(
                resolve_stage_models(config, definition.stage_key)
            )
        ]
    return {
        "schema_version": AI_CONFIG_SCHEMA_VERSION,
        "mode": config.mode,
        "providers": providers,
        "models": models,
        "definitions": definitions,
        "stages": {
            stage_key: list(config.stages.get(stage_key, []))
            for stage_key in AI_STAGE_KEYS
        },
        "effective": effective,
        "routable_model_ids": routable_model_ids,
        "warnings": list(config.config_warnings),
    }


def _load_provider_config(value: Any) -> AiProviderConfig:
    if not isinstance(value, dict):
        return AiProviderConfig()
    return AiProviderConfig(
        base_url=str(value.get("base_url") or ""),
        api_key=str(value.get("api_key") or ""),
        model=str(value.get("model") or ""),
        timeout_seconds=float(value.get("timeout_seconds") or 60),
    )


def _load_ai_models(
    value: Any,
    *,
    text_provider: AiProviderConfig,
    image_provider: AiProviderConfig,
) -> list[AiModelConfig]:
    loaded: list[AiModelConfig] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                loaded.append(_model_config_from_payload(item))
    return _normalize_ai_models(
        loaded,
        text_provider=text_provider,
        image_provider=image_provider,
    )


def _normalize_ai_models(
    models: list[AiModelConfig],
    *,
    text_provider: AiProviderConfig,
    image_provider: AiProviderConfig,
) -> list[AiModelConfig]:
    normalized_by_id = {
        model.id: _normalize_model_config(model)
        for model in models
        if model.id.strip()
    }
    for default_model in DEFAULT_AI_MODELS:
        existing = normalized_by_id.get(default_model.id)
        normalized_by_id[default_model.id] = _merge_default_model(
            default_model,
            existing=existing,
            text_provider=text_provider,
            image_provider=image_provider,
        )
    return list(normalized_by_id.values())


def _merge_default_model(
    default_model: AiModelConfig,
    *,
    existing: AiModelConfig | None,
    text_provider: AiProviderConfig,
    image_provider: AiProviderConfig,
) -> AiModelConfig:
    model = existing or default_model
    provider = AiProviderConfig()
    if text_provider.model.strip() == default_model.model:
        provider = text_provider
    elif image_provider.model.strip() == default_model.model:
        provider = image_provider
    if provider.model.strip() == default_model.model:
        model = AiModelConfig(
            id=default_model.id,
            label=model.label or default_model.label,
            base_url=provider.base_url or model.base_url or default_model.base_url,
            api_key=provider.api_key or model.api_key,
            model=provider.model or model.model or default_model.model,
            timeout_seconds=provider.timeout_seconds or model.timeout_seconds,
            supports_text=default_model.supports_text,
            supports_image=default_model.supports_image,
        )
    return _normalize_model_config(model)


def _model_config_from_payload(value: dict[str, Any]) -> AiModelConfig:
    return AiModelConfig(
        id=str(value.get("id") or value.get("model") or ""),
        label=str(value.get("label") or value.get("model") or value.get("id") or ""),
        base_url=str(value.get("base_url") or ""),
        api_key=str(value.get("api_key") or ""),
        model=str(value.get("model") or ""),
        timeout_seconds=float(value.get("timeout_seconds") or 60),
        supports_text=bool(value.get("supports_text", True)),
        supports_image=bool(value.get("supports_image", False)),
    )


def _normalize_model_config(config: AiModelConfig) -> AiModelConfig:
    return AiModelConfig(
        id=config.id.strip(),
        label=config.label.strip() or config.id.strip(),
        base_url=config.base_url.strip().rstrip("/"),
        api_key=config.api_key.strip(),
        model=config.model.strip(),
        timeout_seconds=float(config.timeout_seconds or 60),
        supports_text=bool(config.supports_text),
        supports_image=bool(config.supports_image),
    )


def _select_active_model(
    models: list[AiModelConfig],
    active_model_id: str,
    *,
    supports: str,
    fallback_provider: AiProviderConfig,
) -> AiModelConfig | None:
    capability = "supports_text" if supports == "text" else "supports_image"
    capable_models = [model for model in models if getattr(model, capability) and model.provider.is_configured]
    for model in capable_models:
        if model.id == active_model_id:
            return model
    if fallback_provider.is_configured:
        for model in capable_models:
            if (
                model.model == fallback_provider.model.strip()
                and model.base_url == fallback_provider.base_url.strip().rstrip("/")
            ):
                return model
    return capable_models[0] if capable_models else None


def _normalize_provider_config(config: AiProviderConfig) -> AiProviderConfig:
    return AiProviderConfig(
        base_url=config.base_url.strip().rstrip("/"),
        api_key=config.api_key.strip(),
        model=config.model.strip(),
        timeout_seconds=float(config.timeout_seconds or 60),
    )


def _resolve_mode(config: AiRecognitionConfig) -> str:
    requested = config.mode.strip() or "local_rule_parser"
    if requested != "local_rule_parser":
        return requested
    if (
        config.text_provider.is_configured
        or config.image_provider.is_configured
        or any(model.provider.is_configured for model in config.ai_models)
        or any(
            provider.enabled and provider.is_configured
            for provider in config.providers
        )
    ):
        return "ai_provider"
    return requested


def _with_normalized_strategy_output_instructions(prompt: str) -> str:
    prompt = prompt.strip()
    if "【策略字段统一格式】" in prompt:
        if MARKET_ENTRY_WITH_PRICE_INSTRUCTION not in prompt:
            prompt = f"{prompt}\n{MARKET_ENTRY_WITH_PRICE_INSTRUCTION}"
        return _with_reference_strategy_instruction(prompt)
    return _with_reference_strategy_instruction(
        f"{prompt}\n\n{NORMALIZED_STRATEGY_OUTPUT_INSTRUCTIONS.strip()}"
    )


def _with_reference_strategy_instruction(prompt: str) -> str:
    prompt = prompt.strip()
    if REFERENCE_STRATEGY_INSTRUCTION in prompt:
        return prompt
    return f"{prompt}\n{REFERENCE_STRATEGY_INSTRUCTION}"


def _with_market_entry_with_price_instruction(prompt: str) -> str:
    prompt = prompt.strip()
    if MARKET_ENTRY_WITH_PRICE_INSTRUCTION in prompt:
        return prompt
    return f"{prompt}\n\n{MARKET_ENTRY_WITH_PRICE_INSTRUCTION}"


def _with_price_shorthand_instruction(prompt: str) -> str:
    prompt = prompt.strip()
    if PRICE_SHORTHAND_NORMALIZATION_INSTRUCTION in prompt:
        return prompt
    return f"{prompt}\n\n{PRICE_SHORTHAND_NORMALIZATION_INSTRUCTION}"


def _with_lifecycle_event_instructions(prompt: str) -> str:
    return _with_price_shorthand_instruction(prompt)


def _with_mimo_direct_instructions(prompt: str) -> str:
    return _with_price_shorthand_instruction(
        _with_reference_strategy_instruction(
            _with_market_entry_with_price_instruction(prompt)
        )
    )


def _provider_to_payload(config: AiProviderConfig) -> dict[str, Any]:
    return {
        "base_url": config.base_url,
        "api_key": config.api_key,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
    }


def _model_to_payload(config: AiModelConfig) -> dict[str, Any]:
    return {
        "id": config.id,
        "label": config.label,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "supports_text": config.supports_text,
        "supports_image": config.supports_image,
    }

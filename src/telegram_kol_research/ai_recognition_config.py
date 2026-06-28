"""Configuration for message-level AI strategy recognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
- position_update：当前消息是在管理已 entered 策略但没有完全离场，例如提前止盈一半、止盈一半、分批止盈30%、按比例止盈、减仓一半、减仓30%、持仓收益达到100%后分批止盈、带保护、保护止损、上移止损、推保护、继续持有。management_action 可输出 partial_take_profit、move_stop_to_protect、hold_update、risk_update。
- 如果当前消息明确调整止损价，请输出 stop_loss；明确调整止盈价或止盈计划，请输出 take_profit；只是“推保护/带保护/保本”但没有新价格时，management_action 输出 move_stop_to_protect。
- 临时入场、临时离场、部分止盈、调整止盈价、调整止损价都属于生命周期事件，不要当成新的 strategy。
- none：普通聊天、行情观点、广告、复盘、联系方式、无法确定目标策略、或只是识别新策略但不改变已有策略。
- 必须优先依据当前消息，不要把上下文里的旧消息当成当前动作。
- 如果能明确对应活跃策略，请输出 target_lifecycle_id。
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


@dataclass(frozen=True)
class AiProviderConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = 60.0

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

    @property
    def provider(self) -> AiProviderConfig:
        return AiProviderConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class AiRecognitionConfig:
    recognition_prompt: str = DEFAULT_RECOGNITION_PROMPT
    lifecycle_event_prompt: str = DEFAULT_LIFECYCLE_EVENT_PROMPT
    mimo_direct_prompt: str = DEFAULT_MIMO_DIRECT_PROMPT
    mode: str = "local_rule_parser"
    text_provider: AiProviderConfig = field(default_factory=AiProviderConfig)
    image_provider: AiProviderConfig = field(default_factory=AiProviderConfig)
    ai_models: list[AiModelConfig] = field(default_factory=list)
    active_text_model_id: str = ""
    active_image_model_id: str = ""


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


def load_ai_recognition_config(config_path: str | Path) -> AiRecognitionConfig:
    """Load AI recognition settings, falling back to conservative defaults."""

    path = Path(config_path)
    if not path.exists():
        return AiRecognitionConfig(
            mimo_direct_prompt=_with_mimo_direct_instructions(DEFAULT_MIMO_DIRECT_PROMPT)
        )

    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw_data, dict):
        return AiRecognitionConfig()

    recognition_prompt = _with_normalized_strategy_output_instructions(
        str(raw_data.get("recognition_prompt") or DEFAULT_RECOGNITION_PROMPT)
    )
    lifecycle_event_prompt = str(raw_data.get("lifecycle_event_prompt") or DEFAULT_LIFECYCLE_EVENT_PROMPT)
    mimo_direct_prompt = _with_mimo_direct_instructions(
        str(raw_data.get("mimo_direct_prompt") or DEFAULT_MIMO_DIRECT_PROMPT)
    )
    mode = str(raw_data.get("mode") or "local_rule_parser")
    raw_text_provider = _load_provider_config(raw_data.get("text_provider"))
    raw_image_provider = _load_provider_config(raw_data.get("image_provider"))
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
    )


def save_ai_recognition_config(
    config_path: str | Path,
    config: AiRecognitionConfig,
) -> AiRecognitionConfig:
    """Persist AI recognition settings and return the normalized config."""

    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    normalized = AiRecognitionConfig(
        recognition_prompt=_with_normalized_strategy_output_instructions(
            config.recognition_prompt.strip() or DEFAULT_RECOGNITION_PROMPT
        ),
        lifecycle_event_prompt=config.lifecycle_event_prompt.strip() or DEFAULT_LIFECYCLE_EVENT_PROMPT,
        mimo_direct_prompt=_with_mimo_direct_instructions(
            config.mimo_direct_prompt.strip() or DEFAULT_MIMO_DIRECT_PROMPT
        ),
        mode=_resolve_mode(config),
        text_provider=text_model.provider if text_model else _normalize_provider_config(config.text_provider),
        image_provider=image_model.provider if image_model else _normalize_provider_config(config.image_provider),
        ai_models=ai_models,
        active_text_model_id=text_model.id if text_model else "",
        active_image_model_id=image_model.id if image_model else "",
    )
    payload: dict[str, Any] = {
        "mode": normalized.mode,
        "recognition_prompt": normalized.recognition_prompt,
        "lifecycle_event_prompt": normalized.lifecycle_event_prompt,
        "mimo_direct_prompt": normalized.mimo_direct_prompt,
        "active_text_model_id": normalized.active_text_model_id,
        "active_image_model_id": normalized.active_image_model_id,
        "ai_models": [_model_to_payload(model) for model in normalized.ai_models],
        "text_provider": _provider_to_payload(normalized.text_provider),
        "image_provider": _provider_to_payload(normalized.image_provider),
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return normalized


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


def _with_mimo_direct_instructions(prompt: str) -> str:
    return _with_reference_strategy_instruction(
        _with_market_entry_with_price_instruction(prompt)
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

"""Canonical prompt identifiers and initial registry seeds."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import (
    DEFAULT_LIFECYCLE_EVENT_PROMPT,
    DEFAULT_MIMO_DIRECT_PROMPT,
    DEFAULT_RECOGNITION_PROMPT,
    AiRecognitionConfig,
)
from telegram_kol_research.prompt_registry import (
    PromptDetail,
    PromptSeed,
    seed_prompt_definition,
)


SHARED_TRADING_PROMPT = "trading.analysis.shared"
MIMO_VISION_PROMPT = "trading.analysis.mimo_vision"
RESEARCH_CHAT_SYSTEM_PROMPT = "research.chat.system"
STRATEGY_ALERT_PROMPT = "strategy.alert.classifier"
GROUP_RESEARCH_PROMPT = "research.chat.group"
SEMANTIC_DISAGREEMENT_REVIEW_PROMPT = "trading.disagreement.semantic_review"


DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT = """
你是 Telegram 加密货币 KOL 消息的交易策略分析器。你必须同时完成“新开仓识别”和“已有策略生命周期事件识别”，这两个维度相互独立。不要做行情预测，不要补全消息与上下文中没有的事实。

【新开仓识别】
- 只有同时具备明确标的、方向、入场方式，以及至少一个止损/止盈/无效价/保护价/分批止盈计划时，才可判定为新策略。
- 新策略表达的是新开仓或新挂单，不是已有仓位管理、复盘、教学、广告或群公告。
- 只有方向没有入场、只有价格没有方向、已经错过、明确不要进场、普通观点或联系方式，均不是新策略。
- 如果完整的新开仓参数已经明确，不要仅因“可以考虑”“参考”“正常我不做单”等弱提示判为非策略。
- 如果正文明确是“会员单盈利、已盈利、做个参考、复盘”，完整参数只是历史信号回顾，应按历史策略或复盘处理，不要创建新策略。

【生命周期事件与仓位管理】
- entry_confirm：此前 pending_entry 策略现在/现价/市价/直接入场，或明确已经进场。
- cancel_entry：取消此前 pending_entry 挂单或等待入场策略，例如取消限价、撤单、不进了、等后续信号。
- 若输入包含 reply_context，它是精确 Telegram 回复目标而非普通聊天上下文。当前消息明确表达取消且 reply_context 为 pending_entry 时，使用其 lifecycle_id 作为唯一 target_lifecycle_id 并输出 cancel_entry；若 reply_context 已 entered，“取消/撤单”不得自动转为 exit_position，应输出 none 或低置信度并说明需人工处理。
- exit_position：关闭已 entered 策略，例如平仓、全平、清仓、出局、离场、临时离场、止盈了、止损了、先出来、保本走、成本走、求稳可走、稳健者可走、breakeven exit。求稳可走/稳健者可走仅在当前消息能唯一对应一条已 entered 策略时表示全平。
- position_update：管理已 entered 策略但没有完全离场，例如提前止盈一半、第一止盈位、分批止盈30%、减仓一半、推保护、移动止损、调整止盈止损、继续持有。
- “第一止盈点来了”、“已到第一目标”且对应已有持仓时，属于仓位管理 position_update，不是新开仓。
- “第一止盈位 60950，移动止损至成本价”属于 position_update，不能判为完整退出。
- “回成本了，注意保护成本，平加仓”表示减仓并把止损移动到成本保护，management_action 输出 partial_take_profit, move_stop_to_protect。
- 无法唯一对应目标策略时，event_type 输出 none 或置信度低于 0.7。
- 单一策略能明确对应时只输出 target_lifecycle_id，不要输出 targets；多个独立策略均能明确对应时，才输出包含每个唯一 lifecycle ID 的非空 targets。必须优先依据当前消息，不能把旧上下文当成当前动作。

【价格与字段归一化】
- symbol 输出大写币种简称；side 只能是 long 或 short。
- entry、stop_loss、take_profit 都输出字符串；多档价格用“/”分隔，区间用“-”连接。
- order_type 只能是 market、limit、market+limit。
- 同时出现市价进场和具体点位时，例如“Eth(市价进场)，进场点位1730附近”，entry 输出“市价进场/1730附近”。
- BTC 的“5.89-5.93附近、5.89万-5.93万、5.89-5.93w”统一为“58900-59300”；“5.78”按语境输出“57800”；“6万/6.07/6.23”输出“60000/60700/62300”。
- ETH 等其他币种不要套用 BTC 万位规则，除非原文明确带“万”。
- 如果文字标的与全部关键价格尺度明显冲突，例如写 BTC 但入场/止损/止盈都在 ETH 常见千位区间且不是“万/w”简写，必须在 reason 说明疑似 BTC/ETH 笔误；只有在当前消息证据足够明确时才输出最可能的真实 symbol，并把 confidence 降到 0.69 以下等待人工复核。
- 不要把价格字段输出成数组或对象，不要补全原文没有的价格。

【置信度与安全】
- confidence 范围为 0 到 1。
- 缺少关键字段、无法唯一关联、内容矛盾或不可读时，输出非策略、none、识别失败或低置信度，禁止猜测。
- 一条出局消息不是新开仓，recognition_result 可以是“非策略”，但 lifecycle_event.event_type 必须是 exit_position。
- 持仓管理不是新开仓，但不得遗漏 position_update。

只输出一个 JSON 对象，不要输出解释文字：
{
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
    "observed_text": "从当前输入中实际读到的关键内容",
    "image_quality": "clear | blurry | cropped | unreadable | none"
  },
  "confidence": 0.0
}
""".strip()


DEFAULT_MIMO_VISION_PROMPT = """
【图片与图文补充规则】
- 直接读取图片中可见的文字、表格、交易所截图、持仓截图、标注、箭头、标签和图表点位，不依赖外部 OCR 猜测。
- 必须结合当前正文/caption 与图片整体判断，不要只看其中一个。
- 只报告实际看见的内容；不要补全图片或文字中没有的币种、方向、价格、止损、止盈或关联策略。
- 图片模糊、裁切、遮挡、无法读取或内部矛盾时，应输出识别失败或低置信度，禁止猜测。
- image_quality 根据实际情况输出 clear、blurry、cropped、unreadable；没有图片时输出 none。
- 图片中的历史策略、盈利展示或转发截图必须结合正文语境判断，不能因为截图参数完整就自动建立新策略。
""".strip()


DEFAULT_RESEARCH_CHAT_SYSTEM_PROMPT = (
    "你是 Telegram 交易群研究助手。只能依据提供的消息来源上下文回答，"
    "并使用 [1]、[2] 这样的编号引用证据。消息按时间正序排列，后面的消息更新；"
    "分析最新状态时优先考虑后续变化，并明确区分事实、推断和不确定性。"
)

DEFAULT_GROUP_RESEARCH_PROMPT = "本群组暂无额外研究规则，继续遵守全局系统提示词。"


DEFAULT_STRATEGY_ALERT_PROMPT = """
Classify one Telegram trading-group message.
Goal: identify entry or exit strategy messages. Prefer recall over precision.
Extract kol_label from the first line when it names a trader; otherwise use an empty string.
strategy_kind must be one of: "entry", "exit", "other".
confidence must be a number from 0 to 1.
Return compact JSON only with keys: is_strategy, strategy_kind, confidence, kol_label, reason_short.
chat_title={chat_title}
sender_name={sender_name}
first_line={first_line}
message_text:
{message_text}
""".strip()


DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT = """
你是 DeepSeek 语义分歧复核器。你的唯一任务是独立解读当前消息，再评估已给出的两份识别结果是否存在有实质影响的语义分歧。不要做行情预测，不要补全当前消息中没有的事实。

【证据与权限边界】
- 必须引用当前消息中的证据；evidence 只写当前消息中支持独立判断的短引文或可核对事实，没有证据时保持空数组并降低置信度。
- 这个复核仅供通知与人工审查，不得修改交易、阻断交易、取消交易或授权任何交易动作。
- 你只能读取提供给你的文本、结构化识别结果和文本化图片摘要；不得声称能够读取图片像素，不得根据未提供的图片内容推断。

【独立动作判断】
- action_type 只能是：none | entry | entry_confirm | cancel_entry | exit_full | exit_partial | position_update。
- 无法从当前消息独立确定时输出 none，不要把模型结果本身当成当前消息的证据。
- target_lifecycle_id、symbol、side、stop_loss、take_profit 和 management_action 无明确依据时输出 null。

【冲突分类】
- conflict_types 只能包含以下闭合词汇，不得创建其他值：actionability, action_family, full_vs_partial_exit, symbol, side, target_lifecycle, stop_intent, urgent_exit_missed, execution_unresolved, non_material_price_detail, wording_only。
- 仅有会改变是否可执行、动作家族、全平与部分退出、标的/方向/目标策略、止损意图、紧急退出遗漏或执行状态的分歧，才可将 material_disagreement 设为 true。
- 仅价格细节差异或措辞差异分别用 non_material_price_detail 或 wording_only，且 material_disagreement 为 false。
- suggested_severity 只能是 none | normal | critical；紧急退出遗漏或会造成危险执行的实质分歧才能使用 critical。

只输出一个 JSON 对象，不要输出解释文字，不得添加额外字段：
{
  "independent_action": {
    "action_type": "none | entry | entry_confirm | cancel_entry | exit_full | exit_partial | position_update",
    "target_lifecycle_id": null,
    "symbol": null,
    "side": null,
    "stop_loss": null,
    "take_profit": null,
    "management_action": null
  },
  "evidence": [],
  "conflict_types": [],
  "material_disagreement": false,
  "suggested_severity": "none | normal | critical",
  "confidence": 0.0,
  "reason": ""
}
""".strip()


def _legacy_custom_content(value: str, default: str) -> str:
    normalized = value.strip()
    baseline = default.strip()
    if not normalized or normalized == baseline:
        return ""
    if normalized.startswith(baseline):
        return normalized[len(baseline):].strip()
    return normalized


def _legacy_shared_content(config: AiRecognitionConfig) -> str:
    recognition = _legacy_custom_content(
        config.recognition_prompt,
        DEFAULT_RECOGNITION_PROMPT,
    )
    lifecycle = _legacy_custom_content(
        config.lifecycle_event_prompt,
        DEFAULT_LIFECYCLE_EVENT_PROMPT,
    )
    if not recognition and not lifecycle:
        return DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT
    legacy_parts = [
        part
        for part in (recognition, lifecycle)
        if part
    ]
    return "\n\n".join(
        [
            "【旧配置中保留的自定义交易经验】",
            *legacy_parts,
            "【统一输出与公共规则】",
            DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
        ]
    )


def _legacy_image_notes(value: str) -> str:
    lines: list[str] = []
    keywords = (
        "图片",
        "图像",
        "截图",
        "表格",
        "标注",
        "箭头",
        "ocr",
        "image",
        "screenshot",
    )
    for raw_line in value.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if (
            line
            and '"' not in line
            and any(keyword in lowered for keyword in keywords)
        ):
            lines.append(line)
    return "\n".join(dict.fromkeys(lines))


def build_prompt_seeds_from_legacy(
    config: AiRecognitionConfig,
) -> list[PromptSeed]:
    image_notes = _legacy_image_notes(
        _legacy_custom_content(config.mimo_direct_prompt, DEFAULT_MIMO_DIRECT_PROMPT)
    )
    vision_content = "\n\n".join(
        part
        for part in (image_notes, DEFAULT_MIMO_VISION_PROMPT)
        if part
    )
    return [
        PromptSeed(
            prompt_key=SHARED_TRADING_PROMPT,
            display_name="统一交易分析 A",
            description="DeepSeek 与 MiMo 共享的新策略和生命周期分析规则。",
            category="trading",
            consumers=("deepseek", "mimo"),
            required_variables=(),
            validation_profile="trading_shared",
            content=_legacy_shared_content(config),
        ),
        PromptSeed(
            prompt_key=MIMO_VISION_PROMPT,
            display_name="MiMo 图片补充 B",
            description="仅供 MiMo 使用的图片和图文读取规则。",
            category="trading",
            consumers=("mimo",),
            required_variables=(),
            validation_profile="mimo_vision",
            content=vision_content,
        ),
        PromptSeed(
            prompt_key=RESEARCH_CHAT_SYSTEM_PROMPT,
            display_name="群组研究系统提示词",
            description="Web 群组研究问答的系统规则。",
            category="research",
            consumers=("research_chat",),
            required_variables=(),
            validation_profile="plain_system",
            content=DEFAULT_RESEARCH_CHAT_SYSTEM_PROMPT,
        ),
        PromptSeed(
            prompt_key=STRATEGY_ALERT_PROMPT,
            display_name="策略通知分类提示词",
            description="策略提醒二次分类和字段提取。",
            category="notification",
            consumers=("strategy_alert",),
            required_variables=(
                "chat_title",
                "sender_name",
                "first_line",
                "message_text",
            ),
            validation_profile="strategy_alert",
            content=DEFAULT_STRATEGY_ALERT_PROMPT,
        ),
        PromptSeed(
            prompt_key=SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
            display_name="DeepSeek 语义分歧复核",
            description="独立复核 MiMo 与 DeepSeek 识别差异，仅用于通知。",
            category="notification",
            consumers=("deepseek_disagreement_review",),
            required_variables=(),
            validation_profile="semantic_disagreement_review",
            content=DEFAULT_SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
        ),
    ]


def seed_default_prompt_registry(
    session_factory: sessionmaker,
    legacy_config: AiRecognitionConfig | None = None,
) -> list[PromptDetail]:
    config = legacy_config or AiRecognitionConfig()
    return [
        seed_prompt_definition(session_factory, seed)
        for seed in build_prompt_seeds_from_legacy(config)
    ]


def seed_group_research_prompt(
    session_factory: sessionmaker,
    *,
    chat_id: int,
) -> PromptDetail:
    return seed_prompt_definition(
        session_factory,
        PromptSeed(
            prompt_key=GROUP_RESEARCH_PROMPT,
            display_name="群组专属研究提示词",
            description="仅对指定 Telegram 群组生效的附加研究规则。",
            category="research",
            consumers=("research_chat",),
            required_variables=(),
            validation_profile="plain_system",
            content=DEFAULT_GROUP_RESEARCH_PROMPT,
            scope_chat_id=chat_id,
        ),
    )

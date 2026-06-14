"""Configuration for message-level AI strategy recognition."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class AiRecognitionConfig:
    recognition_prompt: str = DEFAULT_RECOGNITION_PROMPT
    mode: str = "local_rule_parser"


def load_ai_recognition_config(config_path: str | Path) -> AiRecognitionConfig:
    """Load AI recognition settings, falling back to conservative defaults."""

    path = Path(config_path)
    if not path.exists():
        return AiRecognitionConfig()

    raw_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw_data, dict):
        return AiRecognitionConfig()

    recognition_prompt = str(
        raw_data.get("recognition_prompt") or DEFAULT_RECOGNITION_PROMPT
    )
    mode = str(raw_data.get("mode") or "local_rule_parser")
    return AiRecognitionConfig(
        recognition_prompt=recognition_prompt,
        mode=mode,
    )


def save_ai_recognition_config(
    config_path: str | Path,
    config: AiRecognitionConfig,
) -> AiRecognitionConfig:
    """Persist AI recognition settings and return the normalized config."""

    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = AiRecognitionConfig(
        recognition_prompt=config.recognition_prompt.strip()
        or DEFAULT_RECOGNITION_PROMPT,
        mode=config.mode.strip() or "local_rule_parser",
    )
    payload: dict[str, Any] = {
        "mode": normalized.mode,
        "recognition_prompt": normalized.recognition_prompt,
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return normalized

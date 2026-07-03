"""Dedicated KOL recognition profile registry."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class RecognitionProfile:
    id: str
    title: str
    chat_id: int
    status: str
    style: str
    description: str
    parse_source: str
    capabilities: tuple[str, ...]
    entry_patterns: tuple[str, ...]
    management_patterns: tuple[str, ...]
    risk_policy: str
    symbol_aliases: Mapping[str, tuple[str, ...]]


BITCOIN_JUNZHANG_PROFILE = RecognitionProfile(
    id="junzhang_profile",
    title="比特币军长-11分组",
    chat_id=-1002282384698,
    status="已启用",
    style="短口令 + 生命周期管理",
    description=(
        "识别军长群常见的短句策略：现价开一层多/空、止盈掉、止损上移到开仓价、"
        "取消挂单等，并把后续管理消息绑定到同群组唯一活跃生命周期。"
    ),
    parse_source="junzhang_profile",
    capabilities=(
        "现价开仓",
        "止损识别",
        "止盈离场",
        "止损上移到开仓价",
        "挂单取消",
    ),
    entry_patterns=(
        "BTC现价开一层空，止损65200",
        "比特现价开一层多，止损写清楚",
        "BTC现价开多，止损、止盈完整才允许自动交易",
    ),
    management_patterns=(
        "多单止盈掉",
        "空单止盈掉",
        "止损上移到开仓价",
        "取消挂单",
    ),
    risk_policy="缺少止损/止盈时只作为半策略记录，不生成可自动交易信号。",
    symbol_aliases=MappingProxyType(
        {
            "BTC": ("BTC", "比特", "比特币"),
            "ETH": ("ETH", "以太", "以太币"),
        }
    ),
)


def list_recognition_profiles() -> list[RecognitionProfile]:
    return [BITCOIN_JUNZHANG_PROFILE]

"""Canonical normalization for Deepcoin trading payload fields."""

from __future__ import annotations


def normalize_deepcoin_swap_instrument(symbol: str) -> str:
    normalized = str(symbol).strip().upper().replace("_", "-")
    if normalized.endswith("-SWAP"):
        return normalized
    if normalized.endswith("-USDT"):
        return f"{normalized}-SWAP"
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT-SWAP"
    return f"{normalized}-USDT-SWAP"


def normalize_deepcoin_margin_mode(value: str) -> str:
    return "cross" if value.lower() in {"cross", "crossed", "full"} else "isolated"


def normalize_deepcoin_position_mode(value: str) -> str:
    return "split" if value.lower() in {"split", "hedge", "long_short"} else "merge"

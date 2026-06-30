"""Stable short codes for KOL/group attribution in exchange client order ids."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


DEFAULT_KOL_CODE_CONFIG = Path("config/kol_codes.yaml")
KOL_CODE_PATTERN = re.compile(r"^[A-Z0-9]{1,8}$")


def load_kol_code_map(config_path: str | Path = DEFAULT_KOL_CODE_CONFIG) -> dict[int, str]:
    """Load chat_id -> short code mapping from config/kol_codes.yaml."""

    return _load_kol_code_map_cached(str(config_path))


@lru_cache(maxsize=8)
def _load_kol_code_map_cached(config_path: str) -> dict[int, str]:
    path = Path(config_path)
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_items = payload.get("kol_codes") if isinstance(payload, dict) else {}
    if not isinstance(raw_items, dict):
        raise ValueError("kol_codes config must contain a mapping")

    result: dict[int, str] = {}
    used_codes: dict[str, int] = {}
    for raw_chat_id, raw_code in raw_items.items():
        chat_id = int(raw_chat_id)
        code = normalize_kol_code(raw_code)
        if code in used_codes and used_codes[code] != chat_id:
            raise ValueError(f"duplicate kol code {code!r}")
        used_codes[code] = chat_id
        result[chat_id] = code
    return result


def resolve_kol_code(
    *,
    chat_id: int | None,
    explicit_code: str | None = None,
    config_path: str | Path = DEFAULT_KOL_CODE_CONFIG,
) -> str | None:
    """Return a configured KOL code, preferring an explicit validated value."""

    if explicit_code:
        return normalize_kol_code(explicit_code)
    if chat_id is None:
        return None
    return load_kol_code_map(config_path).get(int(chat_id))


def normalize_kol_code(value: Any) -> str:
    """Normalize and validate a KOL code for DeepCoin clOrdId usage."""

    code = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if not KOL_CODE_PATTERN.fullmatch(code):
        raise ValueError("kol code must be 1-8 alphanumeric characters")
    return code

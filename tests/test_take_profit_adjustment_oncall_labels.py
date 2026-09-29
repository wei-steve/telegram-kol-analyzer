"""Every take-profit adjustment reason code has an on-call Chinese label."""

from __future__ import annotations

import re
from pathlib import Path

from telegram_kol_research.oncall_alerts import REASON_LABELS, reason_label

_SOURCES = (
    "take_profit_adjustment.py",
    "take_profit_adjustment_executor.py",
    "strategy_management_planner.py",
    "management_recovery_timeout.py",
)


def _emitted_codes() -> set[str]:
    root = Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"
    codes: set[str] = set()
    for name in _SOURCES:
        text = (root / name).read_text(encoding="utf-8")
        codes.update(re.findall(r'"(take_profit_adjust_[a-z_]+)"', text))
    codes.discard("take_profit_adjust_mode")
    return codes


def test_every_emitted_take_profit_adjust_code_has_a_label():
    codes = _emitted_codes()
    assert codes, "expected the take-profit adjustment modules to emit reason codes"
    missing = sorted(code for code in codes if code not in REASON_LABELS)
    assert missing == []


def test_labels_do_not_fall_through_to_unknown():
    for code in _emitted_codes() | {"take_profit_replace_incomplete"}:
        assert "未收录" not in reason_label(code)

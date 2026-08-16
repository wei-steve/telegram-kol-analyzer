from __future__ import annotations

from telegram_kol_research.bound_close_writer_quiescence import (
    WORK_TABLE_CONTRACT,
    inspect_bound_close_writer_quiescence,
)
from telegram_kol_research.deployment_preflight import _WORK_SPECS


def test_writer_quiescence_library_keeps_exact_closed_table_contract():
    assert set(WORK_TABLE_CONTRACT) == {spec.table for spec in _WORK_SPECS}


def test_writer_quiescence_library_exposes_only_the_named_inspector():
    assert inspect_bound_close_writer_quiescence.__name__ == (
        "inspect_bound_close_writer_quiescence"
    )

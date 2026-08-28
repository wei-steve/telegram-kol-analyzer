from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from telegram_kol_research.deployment_entry_freeze import (
    DeploymentEntryFreezeError,
    deployment_entry_admission_frozen,
)


@pytest.mark.parametrize(
    ("environment", "expected"),
    (({}, False), ({"TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN": "0"}, False),
     ({"TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN": "1"}, True)),
)
def test_deployment_entry_freeze_is_exact_root_environment_contract(
    environment, expected
) -> None:
    assert deployment_entry_admission_frozen(environment) is expected


def test_deployment_entry_freeze_rejects_malformed_value() -> None:
    with pytest.raises(DeploymentEntryFreezeError, match="must be 0 or 1"):
        deployment_entry_admission_frozen(
            {"TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN": "true"}
        )


def test_recovery_final_exchange_gate_blocks_frozen_entry(
    monkeypatch,
) -> None:
    import telegram_kol_research.recovery_live_submit as recovery

    monkeypatch.setenv("TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN", "1")
    monkeypatch.setattr(
        recovery,
        "source_message_execution_authority",
        lambda _session_factory: nullcontext(),
    )
    entered = False

    with pytest.raises(recovery.RecoveryLiveSubmitError, match="entry_frozen"):
        with recovery._entry_source_exchange_write_gate(
            object(),
            trade_signal=SimpleNamespace(chat_id=100, message_id=55),
            source={},
        ):
            entered = True

    assert entered is False


def test_entry_revision_worker_does_not_construct_or_call_client_when_frozen(
    monkeypatch,
) -> None:
    from telegram_kol_research.entry_revision_executor import (
        run_entry_revision_worker_once,
    )

    monkeypatch.setenv("TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN", "1")
    result = run_entry_revision_worker_once(
        lambda: pytest.fail("database must remain untouched"),
        deepcoin_client=SimpleNamespace(
            trigger_order=lambda _payload: pytest.fail("entry write must stay closed"),
            place_order=lambda _payload: pytest.fail("entry write must stay closed"),
        ),
    )

    assert result == {"status": "deployment_entry_frozen", "batch_ids": []}

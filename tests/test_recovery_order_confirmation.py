from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecLookup
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


class _CapabilityContractSpecProvider:
    def __init__(self, reason):
        self.reason = reason
        self.snapshot = SimpleNamespace(
            source_digest_sha256="b" * 64,
            fetched_at=datetime(2026, 8, 8, 8, 0, tzinfo=UTC),
            expires_at=datetime(2026, 8, 9, 8, 0, tzinfo=UTC),
        )

    def lookup_contract_spec(self, instrument_id):
        spec = None
        state = "suspend" if self.reason == "venue_instrument_not_live" else None
        if self.reason == "available":
            state = "live"
            spec = DeepcoinContractSpec(
                instrument_id=instrument_id,
                contract_value=0.001,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.001,
            )
        return DeepcoinContractSpecLookup(
            instrument_id=instrument_id,
            reason=self.reason,
            venue_state=state,
            contract_spec=spec,
        )

    def get_contract_spec(self, instrument_id):
        return self.lookup_contract_spec(instrument_id).contract_spec


def _persist_approved_recovery(session_factory):
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol="BTC",
                    side="long",
                    entry_range=(68000.0, 68200.0),
                    stop_loss_text="67500",
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action="eligible_for_recovery_limit_order",
                    reason_codes=["recovery_checks_passed"],
                    entry_range=(68000.0, 68200.0),
                    max_loss_usdt=100.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )


def test_confirm_recovery_order_dry_run_marks_verified_queue_item_ready(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)

    result = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["ready_for_live_order"] is True
    assert result["dry_run_only"] is True
    assert result["reason_codes"] == []
    assert result["contract_spec_status"]["code"] == "verified"
    assert result["deepcoin_order_draft"]["order_legs"][0]["quantity_unit"] == "contracts"
    assert [leg["quantity"] for leg in result["deepcoin_order_draft"]["order_legs"]] == [
        72.0,
        72.0,
    ]


def test_confirm_recovery_order_dry_run_blocks_without_contract_spec(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)

    result = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
    )

    assert result["ready_for_live_order"] is False
    assert result["reason_codes"] == ["contract_spec_missing"]
    assert result["contract_spec_status"]["code"] == "missing"


@pytest.mark.parametrize(
    "capability_reason",
    [
        "venue_instrument_unsupported",
        "venue_instrument_not_live",
        "contract_spec_missing",
        "contract_spec_invalid",
        "contract_spec_stale",
        "contract_spec_sync_unavailable",
    ],
)
def test_confirm_recovery_order_rejects_exact_capability_reason_without_writes(
    tmp_path, capability_reason
):
    session_factory = create_session_factory(tmp_path / f"{capability_reason}.db")
    _persist_approved_recovery(session_factory)

    result = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_CapabilityContractSpecProvider(capability_reason),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
    )

    assert result["ready_for_live_order"] is False
    assert result["reason_codes"] == [capability_reason]
    with session_factory() as session:
        from telegram_kol_research.models import ExecutionBinding, TradeSignal
        from telegram_kol_research.models import RecoveryOrderConfirmation

        assert session.query(TradeSignal).count() == 0
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(RecoveryOrderConfirmation).count() == 0


def test_confirm_recovery_order_embeds_exact_validated_snapshot(tmp_path):
    session_factory = create_session_factory(tmp_path / "dynamic-spec.db")
    _persist_approved_recovery(session_factory)

    result = confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_CapabilityContractSpecProvider("available"),
    )

    assert result["ready_for_live_order"] is True
    assert result["deepcoin_order_draft"]["contract_spec_snapshot"] == {
        "source_digest_sha256": "b" * 64,
        "fetched_at": "2026-08-08T08:00:00+00:00",
        "expires_at": "2026-08-09T08:00:00+00:00",
    }


def test_confirm_recovery_order_dry_run_raises_when_item_is_not_in_execution_queue(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_approved_recovery(session_factory)
    upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="alice",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            order_id="existing-order",
            status="open",
        ),
    )

    with pytest.raises(LookupError, match="recovery execution item not found"):
        confirm_recovery_order_dry_run(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            contract_spec_provider=_StaticContractSpecProvider(),
        )

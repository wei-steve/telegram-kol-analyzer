from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import ExecutionBindingRecord
from telegram_kol_research.execution_bindings import upsert_execution_binding
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_execution_queue import (
    list_recovery_execution_previews,
)
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal


def _persist_reviewed_recovery_decision(session_factory, *, action, review_status):
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
                    action=action,
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
        review_status=review_status,
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )


def test_recovery_execution_preview_lists_approved_limit_orders_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="eligible_for_recovery_limit_order",
        review_status="approved_for_order",
    )

    rows = list_recovery_execution_previews(session_factory)

    assert rows == [
        {
            "kol_id": "alice",
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
            "entry_range_text": "68000-68200",
            "stop_loss_text": "67500",
            "take_profit_text": None,
            "max_loss_usdt": 100.0,
            "action": "eligible_for_recovery_limit_order",
            "review_status": "approved_for_order",
            "execution_status": "pending_execution",
            "contract_spec_status": {
                "code": "missing",
                "label": "缺少规格校验",
                "detail": "contract_size_unverified",
                "quantity_unit": "base_asset_estimate",
            },
            "payload_preview": {
                "venue": "deepcoin",
                "contract": "BTC-USDT",
                "order_type": "limit",
                "open_side": "buy",
                "position_side": "long",
                "margin_mode": "cross",
                "position_mode": "split",
                "entry_range": "68000-68200",
                "stop_loss": "67500",
                "take_profit": None,
                "take_profit_allocations": [50.0, 30.0, 20.0],
                "entry_range_order_style": "eager",
                "risk_budget_usdt": 100.0,
                "source": {
                    "kol_id": "alice",
                    "chat_id": 100,
                    "message_id": 55,
                },
            },
            "deepcoin_order_draft": {
                "venue": "deepcoin",
                "dry_run_only": True,
                "executable": False,
                "blocking_reason_codes": ["contract_size_unverified"],
                "strategy_instance_id": "deepcoin:100:55:BTC:long",
                "symbol": "BTC",
                "instrument_id": "BTC-USDT-SWAP",
                "margin_mode": "cross",
                "position_mode": "split",
                "order_legs": [
                        {
                            "side": "buy",
                            "position_side": "long",
                            "order_type": "limit",
                            "price": 68200.0,
                            "allocation_pct": 50.0,
                            "risk_budget_usdt": 50.0,
                            "client_order_id": "TK649760E806ACF61",
                            "quantity": 0.071429,
                            "quantity_unit": "base_asset_estimate",
                            "estimated_stop_loss_usdt": 50.0003,
                        },
                        {
                            "side": "buy",
                            "position_side": "long",
                            "order_type": "limit",
                            "price": 68100.0,
                            "allocation_pct": 50.0,
                            "risk_budget_usdt": 50.0,
                            "client_order_id": "TK729D11F4739D2A2",
                            "quantity": 0.083333,
                            "quantity_unit": "base_asset_estimate",
                            "estimated_stop_loss_usdt": 49.9998,
                        },
                ],
                "stop_loss": 67500.0,
                "take_profit_legs": [],
                "risk_budget_usdt": 100.0,
                "source": {
                    "kol_id": "alice",
                    "chat_id": 100,
                    "message_id": 55,
                },
                "notes": [
                    "offline_constructor_only",
                    "default_cross_margin_split_position",
                    "strategy_instance_id_required_for_exit_matching",
                    "quantity_uses_linear_price_risk_estimate",
                    "limit_edge_selection_side_aware_default",
                    "contract_size_must_be_verified_before_live_order",
                ],
            },
        }
    ]


def test_recovery_execution_preview_ignores_non_approved_or_manual_review_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="manual_review",
        review_status="approved_for_order",
    )

    rows = list_recovery_execution_previews(session_factory)

    assert rows == []


def test_recovery_execution_preview_excludes_already_bound_open_orders(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="eligible_for_recovery_limit_order",
        review_status="approved_for_order",
    )
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

    rows = list_recovery_execution_previews(session_factory)

    assert rows == []


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        assert instrument_id == "BTC-USDT-SWAP"
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


def test_recovery_execution_preview_applies_contract_specs_when_available(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_reviewed_recovery_decision(
        session_factory,
        action="eligible_for_recovery_limit_order",
        review_status="approved_for_order",
    )

    rows = list_recovery_execution_previews(
        session_factory,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    draft = rows[0]["deepcoin_order_draft"]
    assert rows[0]["contract_spec_status"] == {
        "code": "verified",
        "label": "已应用合约规格",
        "detail": "contracts",
        "quantity_unit": "contracts",
    }
    assert draft["blocking_reason_codes"] == []
    assert draft["contract_spec"]["instrument_id"] == "BTC-USDT-SWAP"
    assert draft["order_legs"][0]["quantity"] == 71.0
    assert draft["order_legs"][0]["quantity_unit"] == "contracts"
    assert draft["order_legs"][1]["quantity"] == 83.0

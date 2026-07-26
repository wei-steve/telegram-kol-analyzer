from telegram_kol_research.current_protection_backfill import (
    SupervisedProtectionMapping,
    build_current_protection_backfill_plan,
)
from typer.testing import CliRunner

from telegram_kol_research.cli import app


def test_review_plan_accepts_only_explicit_order_to_position_mapping():
    plan = build_current_protection_backfill_plan(
        mappings=[
            SupervisedProtectionMapping(
                order_id="order-1",
                pos_id="pos-1",
                evidence_hash="official-ui-hash",
            )
        ],
        positions=[
            {
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "3",
            }
        ],
        pending_orders=[
            {
                "ordId": "order-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerPrice": "61000",
            }
        ],
        verified_order_ids=set(),
    )

    assert [(action.order_id, action.pos_id, action.classification) for action in plan.actions] == [
        ("order-1", "pos-1", "review")
    ]
    assert plan.refusals == ()
    assert len(plan.fingerprint) == 64


def test_review_plan_refuses_a_mapping_when_the_order_is_not_pending():
    plan = build_current_protection_backfill_plan(
        mappings=[SupervisedProtectionMapping("order-1", "pos-1", "evidence")],
        positions=[{"posId": "pos-1", "instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "3"}],
        pending_orders=[],
        verified_order_ids=set(),
    )

    assert plan.actions == ()
    assert plan.refusals[0].reason == "pending_order_missing"


def test_review_plan_refuses_a_mapping_with_mismatched_exchange_identity():
    plan = build_current_protection_backfill_plan(
        mappings=[SupervisedProtectionMapping("order-1", "pos-1", "evidence")],
        positions=[{"posId": "pos-1", "instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "3"}],
        pending_orders=[{"ordId": "order-1", "instId": "ETH-USDT-SWAP", "posSide": "long"}],
        verified_order_ids=set(),
    )

    assert plan.actions == ()
    assert plan.refusals[0].reason == "exchange_identity_mismatch"


def test_review_plan_refuses_an_order_that_already_has_verified_ledger_evidence():
    plan = build_current_protection_backfill_plan(
        mappings=[SupervisedProtectionMapping("order-1", "pos-1", "evidence")],
        positions=[{"posId": "pos-1", "instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "3"}],
        pending_orders=[{"ordId": "order-1", "instId": "BTC-USDT-SWAP", "posSide": "long"}],
        verified_order_ids={"order-1"},
    )

    assert plan.actions == ()
    assert plan.refusals[0].reason == "already_verified"


def test_cli_exposes_read_only_current_protection_backfill_plan_command():
    result = CliRunner().invoke(app, ["plan-current-protection-backfill", "--help"])

    assert result.exit_code == 0
    assert "--mapping-file" in result.stdout
    assert "--apply" not in result.stdout

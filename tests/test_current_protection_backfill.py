from telegram_kol_research.current_protection_backfill import (
    SupervisedProtectionMapping,
    apply_current_protection_backfill_plan,
    build_current_protection_backfill_plan,
)
from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionProtectionLedger,
)


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


def test_cli_exposes_fingerprint_guarded_current_protection_backfill_command():
    result = CliRunner().invoke(app, ["plan-current-protection-backfill", "--help"])

    assert result.exit_code == 0
    assert "--mapping-file" in result.stdout
    assert "--apply" in result.stdout
    assert "--expected-fingerprint" in result.stdout


def test_apply_supervised_plan_writes_verified_ledger_after_fingerprint_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="kol",
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="open",
        )
        session.add(binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id="deepcoin:1:2:BTC:long",
                leg_index=1,
                purpose="entry",
                order_kind="limit",
                order_id="entry-1",
                pos_id="pos-1",
                venue="deepcoin",
                attribution_status="verified",
                status="active",
            )
        )
        session.commit()
    plan = build_current_protection_backfill_plan(
        mappings=[SupervisedProtectionMapping("order-1", "pos-1", "evidence")],
        positions=[{"posId": "pos-1", "instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "3"}],
        pending_orders=[{"ordId": "order-1", "instId": "BTC-USDT-SWAP", "posSide": "long"}],
        verified_order_ids=set(),
    )

    result = apply_current_protection_backfill_plan(
        session_factory,
        plan,
        expected_fingerprint=plan.fingerprint,
    )

    assert result.applied == 1
    with session_factory() as session:
        row = session.query(PositionProtectionLedger).one()
    assert (row.order_id, row.pos_id, row.evidence_source) == (
        "order-1",
        "pos-1",
        "official_ui_supervised",
    )

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg
from telegram_kol_research.position_mutation_authority import (
    PositionMutationAuthority,
    PositionMutationAuthorityError,
    ProtectionOrderOwner,
    build_position_mutation_authority,
    require_order_owned_by_authority,
)


def _authority(**overrides):
    values = {
        "venue": "deepcoin",
        "strategy_instance_id": "strategy-other",
        "execution_binding_id": 2,
        "execution_order_leg_id": 22,
        "pos_id": "pos-other",
        "instrument_id": "BTC-USDT-SWAP",
        "side": "long",
        "position_fingerprint": "position-fp",
        "protection_fingerprint": "protection-fp",
    }
    values.update(overrides)
    return PositionMutationAuthority(**values)


def _owner(**overrides):
    values = {
        "venue": "deepcoin",
        "order_id": "ord-other-stop",
        "strategy_instance_id": "strategy-other",
        "execution_binding_id": 2,
        "execution_order_leg_id": 22,
        "pos_id": "pos-other",
        "instrument_id": "BTC-USDT-SWAP",
        "side": "long",
    }
    values.update(overrides)
    return ProtectionOrderOwner(**values)


def test_foreign_order_is_rejected_even_when_price_matches_position_snapshot():
    authority = _authority()
    sister_owner = _owner(
        order_id="ord-sister-stop",
        strategy_instance_id="strategy-sister",
        execution_binding_id=1,
        execution_order_leg_id=11,
        pos_id="pos-sister",
    )

    with pytest.raises(
        PositionMutationAuthorityError,
        match="order_owner_mismatch",
    ):
        require_order_owned_by_authority(
            authority=authority,
            owner=sister_owner,
        )


def test_missing_order_owner_is_rejected():
    with pytest.raises(
        PositionMutationAuthorityError,
        match="order_owner_missing",
    ):
        require_order_owned_by_authority(authority=_authority(), owner=None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("venue", "other"),
        ("strategy_instance_id", "strategy-foreign"),
        ("execution_binding_id", 999),
        ("execution_order_leg_id", 999),
        ("pos_id", "pos-foreign"),
        ("instrument_id", "ETH-USDT-SWAP"),
        ("side", "short"),
    ],
)
def test_every_owner_identity_field_must_match(field, value):
    with pytest.raises(
        PositionMutationAuthorityError,
        match="order_owner_mismatch",
    ):
        require_order_owned_by_authority(
            authority=_authority(),
            owner=_owner(**{field: value}),
        )


def test_exact_owner_is_accepted():
    require_order_owned_by_authority(
        authority=_authority(),
        owner=_owner(),
    )


def test_authority_builder_requires_authoritative_live_entry_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="strategy-other",
            kol_id="other",
            chat_id=-1002,
            message_id=1466,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id="strategy-other",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            pos_id="pos-other",
            venue="deepcoin",
            attribution_status="verified",
            response_json='{"data":{"posId":"pos-other"}}',
            status="active",
        )
        session.add(leg)
        session.commit()

        authority = build_position_mutation_authority(
            session,
            venue="deepcoin",
            pos_id="pos-other",
            live_position={
                "posId": "pos-other",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "2",
                "avgPx": "63900",
                "mgnMode": "cross",
                "mrgPosition": "split",
            },
        )

        assert authority.execution_binding_id == binding.id
        assert authority.execution_order_leg_id == leg.id
        assert authority.position_fingerprint

        with pytest.raises(
            PositionMutationAuthorityError,
            match="target_live_position_size_invalid",
        ):
            build_position_mutation_authority(
                session,
                venue="deepcoin",
                pos_id="pos-other",
                live_position={
                    "posId": "pos-other",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "",
                    "avgPx": "63900",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                },
            )

"""Resolve exact or verified same-group targets for management directives."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from telegram_kol_research.management_directives import ManagementDirective
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
)


class ManagementScopeError(RuntimeError):
    """Raised when a directive cannot be mapped to safe strategy targets."""


@dataclass(frozen=True, slots=True)
class ManagementScopeTarget:
    lifecycle_id: int
    strategy_instance_id: str
    chat_id: int
    symbol: str
    side: str
    scope_source: str


def resolve_management_scope_in_session(
    session: Session,
    *,
    raw_message: RawMessage,
    directive: ManagementDirective,
    explicit_target_lifecycle_id: int | None,
    reply_target_lifecycle_id: int | None,
) -> tuple[ManagementScopeTarget, ...]:
    """Resolve one exact target or a verified risk-reducing group fan-out."""

    exact_target_id = (
        int(reply_target_lifecycle_id)
        if reply_target_lifecycle_id is not None
        else (
            int(explicit_target_lifecycle_id)
            if explicit_target_lifecycle_id is not None
            else None
        )
    )
    if exact_target_id is not None:
        lifecycle = session.get(StrategyLifecycle, exact_target_id)
        if lifecycle is None:
            raise ManagementScopeError("target_lifecycle_not_found")
        _require_source_identity(
            raw_message=raw_message,
            lifecycle=lifecycle,
            directive=directive,
        )
        target = _verified_live_target(
            session,
            lifecycle=lifecycle,
            scope_source=(
                "reply" if reply_target_lifecycle_id is not None else "explicit"
            ),
        )
        if target is None:
            raise ManagementScopeError("target_position_ownership_not_verified")
        return (target,)

    if not directive.risk_reducing or not directive.fanout_allowed:
        raise ManagementScopeError("risk_increasing_fanout_forbidden")
    if directive.symbol is None or directive.side is None:
        raise ManagementScopeError("management_scope_symbol_or_side_missing")

    lifecycles = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.chat_id == int(raw_message.chat_id))
        .filter(StrategyLifecycle.symbol == directive.symbol)
        .filter(StrategyLifecycle.side == directive.side)
        .filter(StrategyLifecycle.lifecycle_status.in_(("entered", "holding")))
        .order_by(StrategyLifecycle.id.asc())
        .all()
    )
    targets = tuple(
        target
        for lifecycle in lifecycles
        if (
            target := _verified_live_target(
                session,
                lifecycle=lifecycle,
                scope_source="verified_group_fanout",
            )
        )
        is not None
    )
    if not targets:
        raise ManagementScopeError("verified_group_management_target_not_found")
    return targets


def _require_source_identity(
    *,
    raw_message: RawMessage,
    lifecycle: StrategyLifecycle,
    directive: ManagementDirective,
) -> None:
    if int(raw_message.chat_id) != int(lifecycle.chat_id):
        raise ManagementScopeError("target_source_identity_mismatch")
    if (
        directive.symbol is not None
        and directive.symbol != str(lifecycle.symbol or "").upper()
    ):
        raise ManagementScopeError("target_source_identity_mismatch")
    if (
        directive.side is not None
        and directive.side != str(lifecycle.side or "").lower()
    ):
        raise ManagementScopeError("target_source_identity_mismatch")


def _verified_live_target(
    session: Session,
    *,
    lifecycle: StrategyLifecycle,
    scope_source: str,
) -> ManagementScopeTarget | None:
    if str(lifecycle.lifecycle_status or "").lower() not in {"entered", "holding"}:
        return None
    if lifecycle.execution_binding_id is None:
        return None
    binding = session.get(ExecutionBinding, int(lifecycle.execution_binding_id))
    if (
        binding is None
        or str(binding.venue or "").lower() != "deepcoin"
        or str(binding.status or "").lower() not in {"open", "active"}
        or not binding.strategy_instance_id
        or int(binding.chat_id) != int(lifecycle.chat_id)
        or int(binding.message_id) != int(lifecycle.message_id)
        or str(binding.symbol or "").upper() != str(lifecycle.symbol or "").upper()
        or str(binding.side or "").lower() != str(lifecycle.side or "").lower()
    ):
        return None
    duplicate_count = (
        session.query(ExecutionBinding)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(ExecutionBinding.strategy_instance_id == binding.strategy_instance_id)
        .filter(ExecutionBinding.status.in_(("open", "active")))
        .count()
    )
    if duplicate_count != 1:
        return None
    verified_leg_count = (
        session.query(ExecutionOrderLeg)
        .filter(ExecutionOrderLeg.execution_binding_id == binding.id)
        .filter(ExecutionOrderLeg.purpose == "entry")
        .filter(ExecutionOrderLeg.attribution_status == "verified")
        .filter(ExecutionOrderLeg.pos_id.is_not(None))
        .filter(ExecutionOrderLeg.pos_id != "")
        .filter(
            ExecutionOrderLeg.status.in_(("active", "open", "filled", "partial_closed"))
        )
        .count()
    )
    if verified_leg_count < 1:
        return None
    return ManagementScopeTarget(
        lifecycle_id=int(lifecycle.id),
        strategy_instance_id=str(binding.strategy_instance_id),
        chat_id=int(lifecycle.chat_id),
        symbol=str(lifecycle.symbol).upper(),
        side=str(lifecycle.side).lower(),
        scope_source=scope_source,
    )

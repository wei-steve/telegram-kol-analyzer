"""Read-only projection of analytical lifecycle and exchange execution truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


LIVE_BINDING_STATUSES = frozenset({"open", "active"})
_CONTRACT_NOT_LOADED = object()


@dataclass(frozen=True, slots=True)
class ExecutionStateProjection:
    state: str
    label: str
    detail: str
    severity: str
    price_touched: bool
    exchange_verified: bool
    retry_allowed: bool
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "label": self.label,
            "detail": self.detail,
            "severity": self.severity,
            "price_touched": self.price_touched,
            "exchange_verified": self.exchange_verified,
            "retry_allowed": self.retry_allowed,
            "reason_codes": list(self.reason_codes),
        }


def project_execution_state(
    *,
    lifecycle_status: str | None = None,
    contract_state: str | None = None,
    contract_terminal_kind: str | None = None,
    binding_status: str | None = None,
    has_live_position: bool = False,
    contradiction_reason_codes: Iterable[str] = (),
) -> ExecutionStateProjection:
    """Combine lifecycle, contract and binding evidence without mutating it.

    ``StrategyLifecycle.entered`` is only price evidence.  A holding projection
    requires a live exchange binding plus a live position/entry leg.  Legacy
    bindings remain readable without manufacturing execution contracts.
    """

    lifecycle = _normalized(lifecycle_status)
    contract = _normalized(contract_state)
    terminal_kind = _normalized(contract_terminal_kind)
    binding = _normalized(binding_status)
    reasons = _bounded_reason_codes(contradiction_reason_codes)

    if (
        not reasons
        and contract == "verified"
        and terminal_kind == "verified_entry"
        and binding not in LIVE_BINDING_STATUSES
    ):
        reasons = ("verified_without_binding",)

    if reasons:
        return ExecutionStateProjection(
            state="contradiction",
            label="执行状态异常",
            detail="持久化状态与交易所执行证据不一致，需要人工核对。",
            severity="critical",
            price_touched=lifecycle == "entered",
            exchange_verified=False,
            retry_allowed=False,
            reason_codes=reasons,
        )

    if contract == "submit_unknown":
        return ExecutionStateProjection(
            state="submit_unknown",
            label="交易所结果待核对，禁止重试",
            detail="请求可能已到达交易所；在精确读回前不得再次提交。",
            severity="critical",
            price_touched=lifecycle == "entered",
            exchange_verified=False,
            retry_allowed=False,
        )

    if contract == "deferred":
        return ExecutionStateProjection(
            state="deferred",
            label="等待相邻消息确认",
            detail="指令仍在等待相邻消息补全，不是已下单结果。",
            severity="info",
            price_touched=lifecycle == "entered",
            exchange_verified=False,
            retry_allowed=True,
        )

    if contract == "submitting":
        return ExecutionStateProjection(
            state="submitting",
            label="正在提交交易所",
            detail="交易请求正在提交，尚未取得可验证的最终证据。",
            severity="warning",
            price_touched=lifecycle == "entered",
            exchange_verified=False,
            retry_allowed=False,
        )

    if contract == "verified" and terminal_kind == "verified_refusal":
        return ExecutionStateProjection(
            state="verified_refusal",
            label="已明确拒绝，未下单",
            detail="系统已持久化明确拒绝结果，没有向交易所建立订单。",
            severity="info",
            price_touched=lifecycle == "entered",
            exchange_verified=True,
            retry_allowed=False,
        )

    if binding in LIVE_BINDING_STATUSES and has_live_position:
        return ExecutionStateProjection(
            state="holding",
            label="持仓中",
            detail="已核验交易所绑定及真实持仓，继续跟踪保护和离场。",
            severity="normal",
            price_touched=lifecycle == "entered",
            exchange_verified=True,
            retry_allowed=False,
        )

    if (
        contract == "verified"
        and terminal_kind == "verified_entry"
        and binding in LIVE_BINDING_STATUSES
    ):
        return ExecutionStateProjection(
            state="exchange_order_verified",
            label="交易所订单已核验，等待成交",
            detail="交易所订单证据已核验，但尚无真实持仓证据。",
            severity="info",
            price_touched=lifecycle == "entered",
            exchange_verified=True,
            retry_allowed=False,
        )

    if lifecycle == "entered":
        return ExecutionStateProjection(
            state="price_touched",
            label="价格触发，未提交交易所订单",
            detail="行情已触发策略入场价，但没有可验证的交易所订单或持仓证据。",
            severity="warning",
            price_touched=True,
            exchange_verified=False,
            retry_allowed=False,
        )

    if contract in {"failed", "expired"}:
        label = "执行已过期，未下单" if contract == "expired" else "执行失败，未下单"
        return ExecutionStateProjection(
            state=contract,
            label=label,
            detail="执行已终止，没有可验证的交易所持仓。",
            severity="warning",
            price_touched=False,
            exchange_verified=True,
            retry_allowed=False,
        )

    return ExecutionStateProjection(
        state="pending",
        label="等待执行",
        detail="策略尚无可验证的交易所执行结果。",
        severity="info",
        price_touched=False,
        exchange_verified=False,
        retry_allowed=True,
    )


def project_lifecycle_execution_state(
    session,
    lifecycle,
    *,
    binding=None,
    entry_legs: Iterable[object] | None = None,
    contract: object | None = _CONTRACT_NOT_LOADED,
) -> ExecutionStateProjection:
    """Load the bounded durable evidence for one lifecycle and project it."""

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        InstructionExecutionContract,
    )
    from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES

    if binding is None and getattr(lifecycle, "execution_binding_id", None) is not None:
        binding = session.get(
            ExecutionBinding,
            int(lifecycle.execution_binding_id),
        )

    if contract is _CONTRACT_NOT_LOADED:
        contract_query = session.query(InstructionExecutionContract)
        candidate_id = getattr(lifecycle, "signal_candidate_id", None)
        strategy_instance_id = getattr(binding, "strategy_instance_id", None)
        if candidate_id is not None:
            contract_query = contract_query.filter(
                InstructionExecutionContract.signal_candidate_id == int(candidate_id)
            )
        elif strategy_instance_id:
            contract_query = contract_query.filter(
                InstructionExecutionContract.strategy_instance_id
                == str(strategy_instance_id)
            )
        else:
            contract_query = None
        contract = (
            contract_query.order_by(InstructionExecutionContract.id.desc()).first()
            if contract_query is not None
            else None
        )

    if entry_legs is None and binding is not None:
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id == int(binding.id),
                ExecutionOrderLeg.purpose == "entry",
            )
            .order_by(ExecutionOrderLeg.leg_index, ExecutionOrderLeg.id)
            .all()
        )
    legs = tuple(entry_legs or ())
    has_verified_leg_position = any(
        str(getattr(leg, "pos_id", None) or "").strip()
        and _normalized(getattr(leg, "attribution_status", None)) == "verified"
        and _normalized(getattr(leg, "status", None)) not in TERMINAL_ENTRY_LEG_STATES
        for leg in legs
    )
    has_binding_position = bool(
        binding is not None
        and str(getattr(binding, "pos_id", None) or "").strip()
    )

    contradictions: list[str] = []
    if (
        contract is not None
        and contract.execution_binding_id is not None
        and (
            binding is None
            or int(contract.execution_binding_id) != int(binding.id)
        )
    ):
        contradictions.append("contract_binding_mismatch")

    return project_execution_state(
        lifecycle_status=getattr(lifecycle, "lifecycle_status", None),
        contract_state=getattr(contract, "state", None),
        contract_terminal_kind=getattr(contract, "terminal_kind", None),
        binding_status=getattr(binding, "status", None),
        has_live_position=has_verified_leg_position or has_binding_position,
        contradiction_reason_codes=contradictions,
    )


def _normalized(value: str | None) -> str:
    return str(value or "").strip().lower()


def _bounded_reason_codes(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            code
            for value in values
            if (code := str(value or "").strip()[:128])
        )
    )[:20]

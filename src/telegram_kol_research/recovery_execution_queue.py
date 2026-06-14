"""Execution preview queue for manually approved recovery decisions."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecProvider
from telegram_kol_research.deepcoin_order_builder import build_deepcoin_order_draft
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import RecoveryDecisionRecord


def list_recovery_execution_previews(
    session_factory: sessionmaker,
    *,
    limit: int = 100,
    contract_spec_provider: DeepcoinContractSpecProvider | None = None,
) -> list[dict[str, object]]:
    """List approved recovery decisions that are ready for execution preview only."""

    with session_factory() as session:
        rows = (
            session.query(RecoveryDecisionRecord)
            .filter(RecoveryDecisionRecord.review_status == "approved_for_order")
            .filter(RecoveryDecisionRecord.action == "eligible_for_recovery_limit_order")
            .order_by(RecoveryDecisionRecord.reviewed_at.asc(), RecoveryDecisionRecord.id.asc())
            .limit(limit)
            .all()
        )
        active_binding_keys = _load_active_binding_keys(session)
        previews = []
        for row in rows:
            key = (row.chat_id, row.message_id, row.symbol.upper(), row.side.lower())
            if key in active_binding_keys:
                continue
            previews.append(_preview_row(row, contract_spec_provider=contract_spec_provider))
        return previews


def _load_active_binding_keys(session) -> set[tuple[int, int, str, str]]:
    rows = (
        session.query(ExecutionBinding)
        .filter(ExecutionBinding.venue == "deepcoin")
        .filter(ExecutionBinding.status.in_(["open", "active"]))
        .all()
    )
    return {
        (
            row.chat_id,
            row.message_id,
            row.symbol.upper(),
            row.side.lower(),
        )
        for row in rows
    }


def _preview_row(
    row: RecoveryDecisionRecord,
    *,
    contract_spec_provider: DeepcoinContractSpecProvider | None,
) -> dict[str, object]:
    side = row.side.lower()
    contract = _to_deepcoin_contract(row.symbol)
    payload_preview = {
        "venue": "deepcoin",
        "contract": contract,
        "order_type": "limit",
        "open_side": _open_side(side),
        "position_side": side,
        "entry_range": row.entry_range_text,
        "stop_loss": row.stop_loss_text,
        "risk_budget_usdt": row.max_loss_usdt,
        "source": {
            "kol_id": row.kol_id,
            "chat_id": row.chat_id,
            "message_id": row.message_id,
        },
    }
    instrument_id = _to_deepcoin_swap_instrument(contract)
    contract_spec = (
        contract_spec_provider.get_contract_spec(instrument_id)
        if contract_spec_provider is not None
        else None
    )
    deepcoin_order_draft = build_deepcoin_order_draft(
        payload_preview,
        contract_spec=contract_spec,
    )
    return {
        "kol_id": row.kol_id,
        "chat_id": row.chat_id,
        "message_id": row.message_id,
        "symbol": row.symbol,
        "side": side,
        "entry_range_text": row.entry_range_text,
        "stop_loss_text": row.stop_loss_text,
        "max_loss_usdt": row.max_loss_usdt,
        "action": row.action,
        "review_status": row.review_status,
        "execution_status": "pending_execution",
        "contract_spec_status": _contract_spec_status(deepcoin_order_draft),
        "payload_preview": payload_preview,
        "deepcoin_order_draft": deepcoin_order_draft,
    }


def _to_deepcoin_contract(symbol: str) -> str:
    normalized = symbol.upper().replace("_", "-")
    if normalized.endswith("-USDT"):
        return normalized
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT"
    return f"{normalized}-USDT"


def _open_side(side: str) -> str:
    return "buy" if side == "long" else "sell"


def _to_deepcoin_swap_instrument(contract: str) -> str:
    normalized = contract.upper().replace("_", "-")
    if normalized.endswith("-SWAP"):
        return normalized
    if normalized.endswith("-USDT"):
        return f"{normalized}-SWAP"
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT-SWAP"
    return f"{normalized}-USDT-SWAP"


def _contract_spec_status(deepcoin_order_draft: dict[str, object]) -> dict[str, str]:
    blocking_reason_codes = [
        str(reason_code)
        for reason_code in deepcoin_order_draft.get("blocking_reason_codes", [])
    ]
    quantity_unit = _first_quantity_unit(deepcoin_order_draft)
    if "contract_size_unverified" in blocking_reason_codes:
        return {
            "code": "missing",
            "label": "缺少规格校验",
            "detail": "contract_size_unverified",
            "quantity_unit": quantity_unit,
        }
    if "quantity_below_minimum" in blocking_reason_codes:
        return {
            "code": "quantity_below_minimum",
            "label": "数量低于最小下单量",
            "detail": "quantity_below_minimum",
            "quantity_unit": quantity_unit,
        }
    if blocking_reason_codes:
        return {
            "code": "blocked",
            "label": "仍有阻断项",
            "detail": ",".join(blocking_reason_codes),
            "quantity_unit": quantity_unit,
        }
    return {
        "code": "verified",
        "label": "已应用合约规格",
        "detail": quantity_unit,
        "quantity_unit": quantity_unit,
    }


def _first_quantity_unit(deepcoin_order_draft: dict[str, object]) -> str:
    order_legs = deepcoin_order_draft.get("order_legs")
    if not isinstance(order_legs, list):
        return "unknown"
    for order_leg in order_legs:
        if isinstance(order_leg, dict) and order_leg.get("quantity_unit"):
            return str(order_leg["quantity_unit"])
    return "unknown"

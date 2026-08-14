from __future__ import annotations

import ast
import importlib
import json
import sqlite3
import sys
import threading
import unicodedata
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Query, sessionmaker

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_snapshot_authority import (
    AccountSnapshotEvidence,
    ExchangeCollectionEvidence,
    build_exchange_collection_evidence,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    InstructionExecutionContract,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    MessageInstructionItem,
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    PositionMutationIntent,
    PositionProtectionLedger,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    Source,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
    TradeSignal,
    TradingSetting,
)
from telegram_kol_research.strategy_management_contracts import (
    ManagementInstructionContract,
    management_contract_fingerprint,
    serialize_management_contract,
)
from telegram_kol_research.strategy_management_planner import (
    management_target_fingerprint,
)


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
INCIDENT_STARTED = NOW - timedelta(seconds=10)
POS_ID = "sensitive-position-119"
PRIMARY_ORDER_ID = "sensitive-primary-order"
BACKUP_ORDER_ID = "sensitive-backup-order"
SOURCE_TEXT = "private source message text should never be retained"
_ACTIVE_SNAPSHOT_FACTORY = None


def _recovery_module():
    return importlib.import_module(
        "telegram_kol_research.composite_management_batch_recovery"
    )


def _profile():
    module = _recovery_module()
    return module.CompositeBatchRecoveryProfile(
        batch_id=7,
        raw_message_id=11,
        lifecycle_id=13,
        trusted_start_size="38",
        target_remaining_size="19",
        instrument_id="BTC-USDT-SWAP",
        side="long",
    )


def test_batch_119_recovery_profile_is_closed_and_immutable():
    module = _recovery_module()

    assert module.BATCH_119_RECOVERY == module.CompositeBatchRecoveryProfile(
        batch_id=119,
        raw_message_id=10532,
        lifecycle_id=794,
        trusted_start_size="38",
        target_remaining_size="19",
        instrument_id="BTC-USDT-SWAP",
        side="long",
    )
    with pytest.raises((AttributeError, TypeError)):
        module.BATCH_119_RECOVERY.batch_id = 120


def test_batch_119_pending_residue_allowlist_is_exactly_five_sha256_identities():
    module = _recovery_module()

    identities = module._APPROVED_BATCH_119_PENDING_RESIDUE_IDENTITY_DIGESTS

    assert isinstance(identities, frozenset)
    assert len(identities) == 5
    assert all(
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
        for value in identities
    )


def test_batch119_exact_loader_is_only_called_from_allowlisted_cli_command():
    module = _recovery_module()
    package_root = Path(module.__file__).resolve().parent
    helper_name = "load_composite_batch_recovery_snapshot_read_only"
    production_mentions = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*.py")
        if helper_name in path.read_text(encoding="utf-8")
    }

    assert production_mentions == {
        "cli.py",
        "composite_management_batch_recovery.py",
    }

    cli_tree = ast.parse((package_root / "cli.py").read_text(encoding="utf-8"))
    parents = {
        child: parent
        for parent in ast.walk(cli_tree)
        for child in ast.iter_child_nodes(parent)
    }
    imports = [
        alias
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == helper_name
    ]
    references = [
        node
        for node in ast.walk(cli_tree)
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == helper_name
        )
        or (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr == helper_name
        )
    ]

    assert [(alias.name, alias.asname) for alias in imports] == [
        (helper_name, None)
    ]
    assert len(references) == 1
    reference = references[0]
    call = parents[reference]
    assert isinstance(call, ast.Call)
    assert call.func is reference
    owner = parents[call]
    while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owner = parents[owner]
    assert owner.name == "recover_composite_management_batch"


def test_recovery_read_only_session_factory_cannot_write_or_change_file(tmp_path):
    module = _recovery_module()
    database_path = tmp_path / "read-only.db"
    with create_engine(f"sqlite:///{database_path}").begin() as connection:
        connection.execute(text("CREATE TABLE probe (value INTEGER NOT NULL)"))
        connection.execute(text("INSERT INTO probe (value) VALUES (1)"))
    before = database_path.read_bytes()

    factory = module.create_composite_recovery_read_only_session_factory(
        database_path
    )
    with factory() as session:
        assert session.execute(text("SELECT value FROM probe")).scalar_one() == 1
        with pytest.raises(OperationalError, match="readonly"):
            session.execute(text("UPDATE probe SET value = 2"))
            session.commit()

    assert database_path.read_bytes() == before


@pytest.mark.parametrize(
    ("current", "disposition", "close_delta", "effective_remaining"),
    [
        ("38", "resume_to_target", "19", "19"),
        ("19", "protection_only_at_target", "0", "19"),
        ("12", "protection_only_below_target", "0", "12"),
        (None, "position_absent", "0", "0"),
    ],
)
def test_classify_recovery_position(
    current, disposition, close_delta, effective_remaining
):
    module = _recovery_module()
    positions = [] if current is None else [
        {
            "posId": "sensitive-position-id",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": current,
        }
    ]

    result = module.classify_recovery_position(
        profile=_profile(),
        positions=positions,
        expected_pos_id="sensitive-position-id",
        instrument_id="BTC-USDT-SWAP",
        side="long",
        quantity_step="1",
        min_quantity="1",
    )

    assert result.disposition == disposition
    assert result.current_size == current
    assert result.close_delta == close_delta
    assert result.effective_remaining_size == effective_remaining


@pytest.mark.parametrize("current", ["-1", "NaN", "Infinity"])
def test_classify_recovery_position_rejects_invalid_live_size(current):
    module = _recovery_module()
    with pytest.raises(module.CompositeBatchRecoveryRefusal):
        module.classify_recovery_position(
            profile=_profile(),
            positions=[
                {
                    "posId": "sensitive-position-id",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": current,
                }
            ],
            expected_pos_id="sensitive-position-id",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            quantity_step="1",
            min_quantity="1",
        )


def test_classify_recovery_position_rejects_duplicate_exact_positions():
    module = _recovery_module()
    row = {
        "posId": "sensitive-position-id",
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "pos": "38",
    }
    with pytest.raises(module.CompositeBatchRecoveryRefusal):
        module.classify_recovery_position(
            profile=_profile(),
            positions=[row, dict(row)],
            expected_pos_id="sensitive-position-id",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            quantity_step="1",
            min_quantity="1",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"pos": "39"},
        {"posSide": "short"},
        {"instId": "ETH-USDT-SWAP"},
    ],
)
def test_classify_recovery_position_rejects_identity_or_exposure_drift(overrides):
    module = _recovery_module()
    row = {
        "posId": "sensitive-position-id",
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "pos": "38",
        **overrides,
    }
    with pytest.raises(module.CompositeBatchRecoveryRefusal):
        module.classify_recovery_position(
            profile=_profile(),
            positions=[row],
            expected_pos_id="sensitive-position-id",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            quantity_step="1",
            min_quantity="1",
        )


def _snapshot(**overrides):
    requested_overrides = dict(overrides)
    scope_protection_overrides = overrides.pop(
        "scope_protection_overrides",
        {},
    )
    values = {
        "positions": [
            {
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "38",
            }
        ],
        "position_history": [],
        "open_orders": [],
        "pending_trigger_orders": [
            {
                "ordId": PRIMARY_ORDER_ID,
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPx": "64000",
                "sz": "38",
                "state": "live",
            },
            {
                "ordId": BACKUP_ORDER_ID,
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPx": "63000",
                "sz": "38",
                "state": "live",
            },
        ],
        "order_history": [],
        "trade_fills": [],
        "trigger_history": [],
        "pending_tpsl_observations": [
            {"instrument_id": "BTC-USDT-SWAP", "complete": True}
        ],
        "errors": {},
        "capture_started_at": NOW,
        "capture_ended_at": NOW,
    }
    values.update(overrides)
    module = _recovery_module()
    protection_orders = (
        ("backup_stop", BACKUP_ORDER_ID),
        ("stop_loss", PRIMARY_ORDER_ID),
    )
    protection_evidence_fingerprints = tuple(
        (
            purpose,
            module._batch119_protection_scope_evidence_fingerprint(
                row=SimpleNamespace(
                    venue="deepcoin",
                    strategy_instance_id="deepcoin:incident:btc:long",
                    pos_id=POS_ID,
                    instrument_id="BTC-USDT-SWAP",
                    side="long",
                    trigger_price=scope_protection_overrides.get(
                        purpose,
                        {},
                    ).get(
                        "trigger_price",
                        "63000" if purpose == "backup_stop" else "64000",
                    ),
                    size_text=scope_protection_overrides.get(
                        purpose,
                        {},
                    ).get("size_text", "38"),
                    status="verified",
                    evidence_source=scope_protection_overrides.get(
                        purpose,
                        {},
                    ).get("evidence_source", "entry_protection_response"),
                    evidence_json=scope_protection_overrides.get(
                        purpose,
                        {},
                    ).get(
                        "evidence_json",
                        '{"response":"private provider response"}',
                    ),
                ),
                purpose=purpose,
                order_id=order_id,
            ),
        )
        for purpose, order_id in protection_orders
    )
    scope_fingerprint = module._batch119_exact_scope_fingerprint(
        position_id=POS_ID,
        protection_orders=protection_orders,
        protection_evidence_fingerprints=protection_evidence_fingerprints,
    )
    exact_scope = module._Batch119ExactHistoryScope(
        instrument_id="BTC-USDT-SWAP",
        side="long",
        scope_fingerprint=scope_fingerprint,
        position_id=POS_ID,
        protection_orders=protection_orders,
        protection_evidence_fingerprints=protection_evidence_fingerprints,
    )
    trigger_by_id = {
        PRIMARY_ORDER_ID: [],
        BACKUP_ORDER_ID: [],
    }
    for row in values["trigger_history"]:
        order_id = (
            str(
                row.get("ordId")
                or row.get("orderId")
                or row.get("order_id")
                or ""
            )
            if isinstance(row, dict)
            else ""
        )
        target = (
            BACKUP_ORDER_ID
            if order_id == BACKUP_ORDER_ID
            else PRIMARY_ORDER_ID
        )
        trigger_by_id[target].append(row)
    rows_by_endpoint = {
        "positions": values["positions"],
        "open_orders": values["open_orders"],
        "pending_trigger_orders": values["pending_trigger_orders"],
        "position_history": values["position_history"],
        "trigger_history_backup_stop": trigger_by_id[BACKUP_ORDER_ID],
        "trigger_history_stop_loss": trigger_by_id[PRIMARY_ORDER_ID],
        "order_history": values["order_history"],
        "trade_fills": values["trade_fills"],
    }
    inner_authority = []
    for endpoint, rows in rows_by_endpoint.items():
        evidence = build_exchange_collection_evidence(
            endpoint=endpoint,
            response={"data": rows},
        )
        inner_authority.append(
            {
                "endpoint": endpoint,
                "available": True,
                "schema_valid": True,
                "complete": True,
                "row_count": len(rows),
                "page_count": 1,
                "fingerprint": evidence.fingerprint,
                "reason_code": None,
            }
        )
    inner_authority.sort(key=lambda row: row["endpoint"])
    values.update(
        {
            "scope_fingerprint": scope_fingerprint,
            "exact_scope": exact_scope,
            "collection_authority": tuple(inner_authority),
        }
    )
    candidate = module.Batch119ExactRecoverySnapshot(**values)
    try:
        collections_fingerprint = (
            module._batch119_snapshot_collections_fingerprint(candidate)
        )
    except Exception:
        collections_fingerprint = "invalid_snapshot_collections"
    outer_evidence = build_exchange_collection_evidence(
        endpoint="batch119_exact_account_composite",
        response={
            "data": [
                {
                    "scope_fingerprint": scope_fingerprint,
                    "capture_window_fingerprint": (
                        module._batch119_capture_window_fingerprint(candidate)
                    ),
                    "snapshot_collections_fingerprint": collections_fingerprint,
                    "collections": inner_authority,
                }
            ]
        },
    )
    candidate.account_authority = AccountSnapshotEvidence(
        uid_scope_hash="5" * 64,
        start_write_generation=0,
        end_write_generation=0,
        collections=(
            ExchangeCollectionEvidence(
                endpoint="batch119_exact_account_composite",
                available=True,
                schema_valid=True,
                complete=True,
                rows=(),
                row_count=1,
                page_count=1,
                fingerprint=outer_evidence.fingerprint,
                reason_code=None,
            ),
        ),
        complete=True,
        reason_code=None,
    )
    if _ACTIVE_SNAPSHOT_FACTORY is None:
        return candidate

    class CandidateCaptureClient:
        uid_scope_hash = "5" * 64

        def read_positions(self, *, inst_id=None):
            return {"data": list(candidate.positions)}

        def read_open_orders(self, *, inst_id=None):
            return {"data": list(candidate.open_orders)}

        def read_trigger_orders_pending(self, *, inst_id):
            return {"data": list(candidate.pending_trigger_orders)}

        def read_position_history(self, *, inst_id, pos_id):
            return {"data": list(candidate.position_history)}

        def read_trigger_order_history(self, *, inst_id, order_id, limit):
            return {
                "data": [
                    dict(row)
                    for row in candidate.trigger_history
                    if str(row.get("ordId") or row.get("orderId") or "")
                    == order_id
                ]
            }

    issued = module.load_composite_batch_recovery_snapshot_read_only(
        _ACTIVE_SNAPSHOT_FACTORY,
        client=CandidateCaptureClient(),
    )
    loader_supported = {
        "positions",
        "position_history",
        "open_orders",
        "pending_trigger_orders",
        "trigger_history",
    }
    for field_name in requested_overrides:
        if field_name not in loader_supported and hasattr(issued, field_name):
            setattr(issued, field_name, getattr(candidate, field_name))
    if (
        scope_protection_overrides
        and issued.exact_scope != candidate.exact_scope
    ):
        issued.exact_scope = candidate.exact_scope
        issued.scope_fingerprint = candidate.scope_fingerprint
        issued.account_authority = candidate.account_authority
    return issued


def _seed_batch_119_false_submission(tmp_path):
    global _ACTIVE_SNAPSHOT_FACTORY
    database = tmp_path / "batch-119.db"
    factory = create_session_factory(database)
    _ACTIVE_SNAPSHOT_FACTORY = factory
    strategy_id = "deepcoin:incident:btc:long"
    contract = ManagementInstructionContract(
        version=2,
        target_lifecycle_id=794,
        strategy_instance_id=strategy_id,
        symbol="BTC",
        side="long",
        close_fraction="0.5",
        stop_mode="actual_entry_price",
        stop_price=None,
        stop_price_source=None,
        take_profit_consumption="consume_first_stage",
        cancel_deferred_entries=True,
        required_components=(
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        current_message_text="historical message text excluded from plans",
    )
    contract_json = serialize_management_contract(contract)
    fingerprint = management_contract_fingerprint(contract)
    with factory() as session:
        raw = RawMessage(
            id=10532,
            chat_id=701,
            message_id=9001,
            text=SOURCE_TEXT,
            raw_payload=json.dumps({"credential": "secret-api-key"}),
            posted_at=INCIDENT_STARTED,
        )
        decision = RecognitionDecision(
            raw_message_id=10532,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="仓位管理",
            authoritative_payload_json='{"source_text":"private"}',
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        lifecycle = StrategyLifecycle(
            id=794,
            chat_id=701,
            message_id=8999,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        binding = ExecutionBinding(
            strategy_instance_id=strategy_id,
            kol_id="incident-source",
            chat_id=701,
            message_id=8999,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id=POS_ID,
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add_all([raw, decision, lifecycle, binding])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        entry = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=strategy_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            order_id="sensitive-entry-order",
            client_order_id="sensitive-entry-client-order",
            pos_id=POS_ID,
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
        )
        session.add(entry)
        session.flush()
        target_snapshot = {
            "execution_mode": "live",
            "identity": {
                "target_lifecycle_id": 794,
                "execution_binding_id": binding.id,
                "strategy_instance_id": strategy_id,
                "manageable_entry_leg_ids": [entry.id],
                "deferred_entry_leg_ids": [],
                "capability_deferred_entry_leg_ids": [],
                "capability_deferred_pos_ids": [],
            },
            "positions": [
                {
                    "pos_id": POS_ID,
                    "instrument_id": "BTC-USDT-SWAP",
                    "side": "long",
                    "size": "38",
                    "trusted_start_size": "38",
                    "target_remaining_size": "19",
                    "avg_entry_price": "62000",
                    "quantity_step": "1",
                    "min_quantity": "1",
                    "margin_mode": "cross",
                    "position_mode": "split",
                    "execution_order_leg_id": entry.id,
                }
            ],
            "deferred_entry_legs": [],
        }
        batch = StrategyManagementBatch(
            id=119,
            idempotency_fingerprint="i" * 64,
            raw_message_id=10532,
            recognition_decision_id=decision.id,
            recognition_generation="incident-generation",
            target_lifecycle_id=794,
            strategy_instance_id=strategy_id,
            execution_binding_id=binding.id,
            intent="partial_then_break_even",
            effective_action="partial_then_break_even",
            execution_mode="live",
            requested_fraction=0.5,
            effective_fraction=0.5,
            management_contract_json=contract_json,
            management_contract_fingerprint=fingerprint,
            contract_version=2,
            status="reconciling",
            reason_code="management_close_pending_exchange_confirmation",
            target_fingerprint=management_target_fingerprint(target_snapshot),
            target_snapshot_json=json.dumps(
                target_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            planned_at=INCIDENT_STARTED,
            started_at=INCIDENT_STARTED,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.flush()
        target_candidate = SignalCandidate(
            raw_message_id=10532,
            symbol="BTC",
            side="long",
            event_type="position_update",
            target_lifecycle_id=794,
            management_action="partial_then_break_even",
            parse_source="mimo_authoritative",
            review_status="pending",
            created_at=NOW,
        )
        session.add(target_candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=10532,
                signal_candidate_id=target_candidate.id,
                sequence=0,
                instruction_kind="management",
                strategy_instance_id=strategy_id,
                idempotency_key="target-incident-instruction",
                status="unknown",
                result_json=None,
                error_json=json.dumps(
                    {
                        "batch_id": 119,
                        "reason": None,
                        "status": "recovery_required",
                        "submitted": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        leg = StrategyManagementLeg(
            management_batch_id=119,
            execution_order_leg_id=entry.id,
            pos_id=POS_ID,
            leg_index=0,
            status="submitted",
            preflight_size="38",
            planned_close_size="19",
            avg_entry_price="62000",
            quantity_step="1",
            client_order_id=None,
            exchange_order_id=None,
            request_json=None,
            response_json=None,
            last_error=None,
            last_exchange_snapshot_json=json.dumps(
                {
                    "position_rows": [
                        {
                            "posId": POS_ID,
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "pos": "38",
                        }
                    ],
                    "matching_regular_orders": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            created_at=INCIDENT_STARTED,
            updated_at=NOW,
        )
        session.add(leg)
        session.flush()
        for sequence, kind in enumerate(contract.required_components):
            desired = {
                "contract_fingerprint": fingerprint,
                "pos_id": POS_ID,
                "execution_order_leg_id": entry.id,
                "trusted_start_size": "38",
                "target_remaining_size": "19",
                "avg_entry_price": "62000",
                "quantity_step": "1",
                "min_quantity": "1",
                "component_kind": kind,
            }
            session.add(
                StrategyManagementComponent(
                    management_batch_id=119,
                    strategy_management_leg_id=leg.id,
                    strategy_management_leg_scope=leg.id,
                    component_kind=kind,
                    sequence=sequence,
                    status="recovery_required" if sequence == 0 else "pending",
                    idempotency_key=sha256(
                        f"{fingerprint}|119|{leg.id}|{kind}".encode("utf-8")
                    ).hexdigest(),
                    desired_json=json.dumps(desired, sort_keys=True),
                    evidence_json=(
                        '[{"error_type":"RuntimeError"}]'
                        if sequence == 0
                        else "[]"
                    ),
                    reason_code=(
                        "take_profit_exchange_snapshot_incomplete"
                        if sequence == 0
                        else None
                    ),
                    attempt_count=1 if sequence == 0 else 0,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        for order_id, purpose, price in (
            (PRIMARY_ORDER_ID, "stop_loss", "64000"),
            (BACKUP_ORDER_ID, "backup_stop", "63000"),
        ):
            session.add(
                PositionProtectionLedger(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=entry.id,
                    strategy_instance_id=strategy_id,
                    pos_id=POS_ID,
                    instrument_id="BTC-USDT-SWAP",
                    side="long",
                    order_id=order_id,
                    purpose=purpose,
                    trigger_price=price,
                    size_text="38",
                    status="verified",
                    evidence_source="entry_protection_response",
                    evidence_json='{"response":"private provider response"}',
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    last_verified_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.add(
            TradingSetting(
                key="global",
                value_json=json.dumps(
                    {
                        "auto_trade_enabled": True,
                        "management_execution_mode": "live",
                        "composite_management_v2_mode": "live",
                        "mimo_contract_mode": "v1",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                updated_at=NOW,
            )
        )
        session.commit()
        return factory, database, int(binding.id), int(entry.id), int(leg.id)


def _add_audited_instruction_population(factory):
    with factory() as session:
        entry_raw = RawMessage(
            id=20001,
            chat_id=801,
            message_id=9101,
            text="synthetic entry",
            posted_at=NOW,
        )
        entry_candidate = SignalCandidate(
            raw_message_id=20001,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            review_status="pending",
            created_at=NOW,
        )
        entry_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:audit:entry",
            kol_id="audit-source",
            chat_id=801,
            message_id=9101,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            status="closed",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([entry_raw, entry_candidate, entry_binding])
        session.flush()
        trade_signal = TradeSignal(
            signal_uid="audit-entry-signal",
            strategy_instance_id="deepcoin:audit:entry",
            source_type="recovery",
            venue="deepcoin",
            kol_id="audit-source",
            chat_id=801,
            message_id=9101,
            symbol="ETH",
            side="long",
            action="open_position",
            status="submitted",
            payload_json="{}",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(trade_signal)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=20001,
                signal_candidate_id=entry_candidate.id,
                sequence=0,
                instruction_kind="entry",
                strategy_instance_id="deepcoin:audit:entry",
                idempotency_key="audited-entry-terminal-mirror",
                status="submitted",
                result_json=json.dumps(
                    {
                        "entry_execution_type": "standard",
                        "result": {
                            "order_count": 1,
                            "orders": [{"leg_index": 0}],
                            "signal_id": trade_signal.id,
                            "submitted": True,
                        },
                        "status": "submitted",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at=NOW,
                updated_at=NOW,
            )
        )

        historical_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:audit:historical",
            kol_id="audit-source",
            chat_id=802,
            message_id=9200,
            symbol="SOL",
            side="short",
            venue="deepcoin",
            status="closed",
            created_at=NOW,
            updated_at=NOW,
        )
        historical_lifecycle = StrategyLifecycle(
            id=9001,
            chat_id=802,
            message_id=9200,
            symbol="SOL",
            side="short",
            lifecycle_status="exited",
            signal_at=NOW,
            exited_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([historical_binding, historical_lifecycle])
        session.flush()
        historical_lifecycle.execution_binding_id = historical_binding.id

        management_rows = []
        for raw_id, message_id, batch_id, batch_status, item_status in (
            (20002, 9201, 901, "succeeded", "submitted"),
            (20004, 9202, 902, "resolved", "unknown"),
        ):
            raw = RawMessage(
                id=raw_id,
                chat_id=802,
                message_id=message_id,
                text="synthetic management",
                posted_at=NOW,
            )
            decision = RecognitionDecision(
                raw_message_id=raw_id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="仓位管理",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
                automation_status="completed",
                created_at=NOW,
                updated_at=NOW,
            )
            candidate = SignalCandidate(
                raw_message_id=raw_id,
                symbol="SOL",
                side="short",
                event_type="position_update",
                target_lifecycle_id=historical_lifecycle.id,
                management_action="adjust_stop_loss",
                parse_source="mimo_authoritative",
                review_status="pending",
                created_at=NOW,
            )
            session.add_all([raw, decision, candidate])
            session.flush()
            batch = StrategyManagementBatch(
                id=batch_id,
                idempotency_fingerprint=sha256(
                    f"audit-batch:{batch_id}".encode()
                ).hexdigest(),
                raw_message_id=raw_id,
                recognition_decision_id=decision.id,
                recognition_generation="audit-generation",
                target_lifecycle_id=historical_lifecycle.id,
                strategy_instance_id="deepcoin:audit:historical",
                execution_binding_id=historical_binding.id,
                intent="adjust_stop_loss",
                effective_action="adjust_stop_loss",
                execution_mode="live",
                status=batch_status,
                reason_code=(
                    "all_position_protection_replaced"
                    if batch_status == "succeeded"
                    else "history_no_submission_confirmed"
                ),
                target_fingerprint=sha256(
                    f"audit-target:{batch_id}".encode()
                ).hexdigest(),
                target_snapshot_json="{}",
                planned_at=NOW,
                completed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(batch)
            session.flush()
            payload = {
                "batch_id": batch_id,
                "reason": (
                    "all_position_protection_replaced"
                    if item_status == "submitted"
                    else "close_final_preflight_failed"
                ),
                "status": (
                    "succeeded"
                    if item_status == "submitted"
                    else "recovery_required"
                ),
                "submitted": item_status == "submitted",
            }
            item = MessageInstructionItem(
                raw_message_id=raw_id,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                strategy_instance_id="deepcoin:audit:historical",
                idempotency_key=f"audited-management-{item_status}",
                status=item_status,
                result_json=(
                    json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    if item_status == "submitted"
                    else None
                ),
                error_json=(
                    json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    if item_status == "unknown"
                    else None
                ),
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(item)
            management_rows.append(item)

        pending_raw = RawMessage(
            id=20003,
            chat_id=803,
            message_id=9301,
            text="synthetic abandoned instruction",
            posted_at=NOW,
        )
        pending_candidate = SignalCandidate(
            raw_message_id=20003,
            symbol="XRP",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            review_status="pending",
            created_at=NOW,
        )
        session.add_all([pending_raw, pending_candidate])
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=20003,
                signal_candidate_id=pending_candidate.id,
                sequence=0,
                instruction_kind="entry",
                strategy_instance_id=None,
                idempotency_key="audited-pending-residue",
                status="pending",
                result_json=None,
                error_json=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ContextResolutionAttempt(
                raw_message_id=20003,
                context_fingerprint="synthetic-pending-context",
                model="deepseek",
                prompt_versions_json="{}",
                request_summary_json="{}",
                decision_json='{"decision":"new_thread"}',
                status="completed",
                reanalysis_triggers_json="[]",
                attempts=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()


def _approve_audited_pending_residue(monkeypatch, factory):
    module = _recovery_module()
    with factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-pending-residue")
            .one()
        )
        candidate = session.get(SignalCandidate, item.signal_candidate_id)
        raw = session.get(RawMessage, item.raw_message_id)
        digest = module._historical_pending_residue_identity_digest(
            session,
            item=item,
            candidate=candidate,
            raw=raw,
        )
    monkeypatch.setattr(
        module,
        "_APPROVED_BATCH_119_PENDING_RESIDUE_IDENTITY_DIGESTS",
        frozenset({digest}),
    )


def test_instruction_population_allows_audited_dispositions_without_writes(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        before = [
            (
                row.id,
                row.status,
                row.result_json,
                row.error_json,
                row.retired_at,
                row.updated_at,
            )
            for row in session.query(MessageInstructionItem)
            .order_by(MessageInstructionItem.id)
            .all()
        ]

    plan = _plan(factory)

    assert plan.status == "ready"
    population = plan.evidence["durable"]["instruction_population"]
    assert population["schema_version"] == 1
    assert population["total_count"] == 5
    assert population["counts"] == {
        "approved_historical_pending_frozen": 1,
        "historical_unknown_frozen": 1,
        "target_incident_frozen": 1,
        "verified_terminal_mirror": 2,
    }
    assert len(population["digest"]) == 64
    with factory() as session:
        after = [
            (
                row.id,
                row.status,
                row.result_json,
                row.error_json,
                row.retired_at,
                row.updated_at,
            )
            for row in session.query(MessageInstructionItem)
            .order_by(MessageInstructionItem.id)
            .all()
        ]
    assert after == before


def test_instruction_population_rejects_generic_pending_row_that_is_claimable(
    tmp_path,
):
    from telegram_kol_research.message_instruction_items import (
        claim_next_message_instruction_item,
    )

    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)

    plan = _plan(factory)
    claimed = claim_next_message_instruction_item(
        factory,
        raw_message_id=20003,
        now=NOW,
    )

    assert claimed is not None
    assert claimed.idempotency_key == "audited-pending-residue"
    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_instruction_population_rejects_submitted_mirror_with_active_contract(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-entry-terminal-mirror")
            .one()
        )
        session.add(
            InstructionExecutionContract(
                message_instruction_item_id=item.id,
                raw_message_id=item.raw_message_id,
                signal_candidate_id=item.signal_candidate_id,
                strategy_instance_id=item.strategy_instance_id,
                intent_kind="entry",
                state="submitting",
                state_version=1,
                reason_code="exchange_submission_in_progress",
                attempted_exchange_write=True,
                evidence_refs_json="[]",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_instruction_population_rejects_contradictory_historical_unknown(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-management-unknown")
            .one()
        )
        payload = json.loads(item.error_json)
        payload["submitted"] = True
        item.error_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_historical_unknown_rejects_lifecycle_binding_pointer_mismatch(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        submitted = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-management-submitted")
            .one()
        )
        submitted.retired_at = NOW
        lifecycle = session.get(StrategyLifecycle, 9001)
        entry_binding = (
            session.query(ExecutionBinding)
            .filter_by(strategy_instance_id="deepcoin:audit:entry")
            .one()
        )
        lifecycle.execution_binding_id = entry_binding.id
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


@pytest.mark.parametrize(
    "attack",
    [
        "target_missing",
        "target_duplicate",
        "target_candidate_symbol_drift",
        "target_candidate_side_drift",
        "executing_item",
        "entry_signal_not_submitted",
        "entry_signal_action_drift",
        "entry_signal_source_type_drift",
        "entry_duplicate_binding",
        "entry_binding_chat_drift",
        "entry_binding_message_drift",
        "entry_binding_symbol_drift",
        "entry_binding_side_drift",
        "entry_binding_venue_drift",
        "management_batch_identity_drift",
        "management_binding_identity_drift",
        "pending_has_result",
        "pending_has_scheduled_retry",
        "pending_has_contract",
        "pending_has_trade_signal",
        "pending_has_active_lifecycle",
        "pending_context_state_drift",
        "unknown_has_active_lifecycle",
        "unknown_has_active_binding",
        "unknown_has_active_descendant",
        "unknown_has_scheduled_retry",
        "unknown_has_deadline",
        "unknown_has_escalation",
        "unknown_has_progress",
        "unknown_candidate_symbol_drift",
        "unknown_candidate_side_drift",
        "unknown_candidate_event_type_drift",
        "unknown_binding_chat_drift",
        "unknown_binding_message_drift",
        "unknown_binding_symbol_drift",
        "unknown_binding_side_drift",
        "unknown_binding_venue_drift",
        "malformed_entry_payload",
        "oversized_entry_payload",
        "deep_entry_payload",
        "bounded_deep_entry_payload",
        "excessive_entry_payload_nodes",
        "duplicate_target_payload_key",
        "entry_payload_nan",
        "entry_payload_positive_infinity",
        "entry_payload_negative_infinity",
        "target_payload_has_extra_key",
    ],
)
def test_instruction_population_fails_closed_on_ambiguous_or_active_rows(
    tmp_path,
    attack,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        target = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="target-incident-instruction")
            .one()
        )
        entry = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-entry-terminal-mirror")
            .one()
        )
        pending = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-pending-residue")
            .one()
        )
        unknown = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-management-unknown")
            .one()
        )
        if attack == "target_missing":
            target.status = "failed"
        elif attack == "target_duplicate":
            candidate = SignalCandidate(
                raw_message_id=10532,
                symbol="BTC",
                side="long",
                event_type="position_update",
                target_lifecycle_id=794,
                management_action="partial_then_break_even",
                parse_source="mimo_authoritative",
                review_status="pending",
                created_at=NOW,
            )
            session.add(candidate)
            session.flush()
            session.add(
                MessageInstructionItem(
                    raw_message_id=10532,
                    signal_candidate_id=candidate.id,
                    sequence=1,
                    instruction_kind="management",
                    strategy_instance_id="deepcoin:incident:btc:long",
                    idempotency_key="duplicate-target-incident",
                    status="unknown",
                    error_json=target.error_json,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        elif attack == "target_candidate_symbol_drift":
            session.get(SignalCandidate, target.signal_candidate_id).symbol = "ETH"
        elif attack == "target_candidate_side_drift":
            session.get(SignalCandidate, target.signal_candidate_id).side = "short"
        elif attack == "executing_item":
            pending.status = "executing"
        elif attack == "entry_signal_not_submitted":
            signal_id = json.loads(entry.result_json)["result"]["signal_id"]
            session.get(TradeSignal, signal_id).status = "failed"
        elif attack == "entry_signal_action_drift":
            signal_id = json.loads(entry.result_json)["result"]["signal_id"]
            session.get(TradeSignal, signal_id).action = "close_position"
        elif attack == "entry_signal_source_type_drift":
            signal_id = json.loads(entry.result_json)["result"]["signal_id"]
            session.get(TradeSignal, signal_id).source_type = "other"
        elif attack == "entry_duplicate_binding":
            session.add(
                ExecutionBinding(
                    strategy_instance_id=entry.strategy_instance_id,
                    kol_id="audit-source-duplicate",
                    chat_id=899,
                    message_id=9999,
                    symbol="ETH",
                    side="long",
                    venue="deepcoin",
                    status="closed",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        elif attack.startswith("entry_binding_"):
            binding = (
                session.query(ExecutionBinding)
                .filter_by(strategy_instance_id=entry.strategy_instance_id)
                .one()
            )
            if attack == "entry_binding_chat_drift":
                binding.chat_id += 1
            elif attack == "entry_binding_message_drift":
                binding.message_id += 1
            elif attack == "entry_binding_symbol_drift":
                binding.symbol = "SOL"
            elif attack == "entry_binding_side_drift":
                binding.side = "short"
            elif attack == "entry_binding_venue_drift":
                binding.venue = "other"
        elif attack == "management_batch_identity_drift":
            session.get(StrategyManagementBatch, 901).raw_message_id = 20003
        elif attack == "management_binding_identity_drift":
            linked = session.get(StrategyManagementBatch, 901)
            session.get(ExecutionBinding, linked.execution_binding_id).symbol = "BTC"
        elif attack == "pending_has_result":
            pending.result_json = '{"status":"submitted"}'
        elif attack == "pending_has_scheduled_retry":
            pending.visibility_first_failed_at = NOW
            pending.visibility_retry_attempts = 1
            pending.visibility_next_attempt_at = NOW
        elif attack == "pending_has_contract":
            session.add(
                InstructionExecutionContract(
                    message_instruction_item_id=pending.id,
                    raw_message_id=pending.raw_message_id,
                    signal_candidate_id=pending.signal_candidate_id,
                    strategy_instance_id=None,
                    intent_kind="entry",
                    state="pending",
                    state_version=0,
                    attempted_exchange_write=False,
                    evidence_refs_json="[]",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        elif attack == "pending_has_trade_signal":
            raw = session.get(RawMessage, pending.raw_message_id)
            candidate = session.get(SignalCandidate, pending.signal_candidate_id)
            session.add(
                TradeSignal(
                    signal_uid="unexpected-pending-signal",
                    strategy_instance_id=None,
                    source_type="telegram",
                    venue="deepcoin",
                    kol_id="audit-source",
                    chat_id=raw.chat_id,
                    message_id=raw.message_id,
                    symbol=candidate.symbol,
                    side=candidate.side,
                    action="open_position",
                    status="pending",
                    payload_json="{}",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        elif attack == "pending_has_active_lifecycle":
            session.get(SignalCandidate, pending.signal_candidate_id).target_lifecycle_id = 794
        elif attack == "pending_context_state_drift":
            (
                session.query(ContextResolutionAttempt)
                .filter_by(raw_message_id=pending.raw_message_id)
                .one()
            ).status = "pending_reanalysis"
        elif attack == "unknown_has_active_lifecycle":
            session.get(StrategyLifecycle, 9001).lifecycle_status = "entered"
        elif attack == "unknown_has_active_binding":
            binding = (
                session.query(ExecutionBinding)
                .filter_by(strategy_instance_id="deepcoin:audit:historical")
                .one()
            )
            binding.status = "active"
        elif attack == "unknown_has_active_descendant":
            session.get(StrategyManagementBatch, 902).status = "executing"
        elif attack == "unknown_has_scheduled_retry":
            unknown.visibility_next_attempt_at = NOW
        elif attack == "unknown_has_deadline":
            unknown.execution_deadline_at = NOW
        elif attack == "unknown_has_escalation":
            unknown.operator_escalation_at = NOW
            unknown.escalation_state = "pending"
        elif attack == "unknown_has_progress":
            unknown.last_progress_at = NOW
        elif attack == "unknown_candidate_symbol_drift":
            session.get(SignalCandidate, unknown.signal_candidate_id).symbol = "BTC"
        elif attack == "unknown_candidate_side_drift":
            session.get(SignalCandidate, unknown.signal_candidate_id).side = "long"
        elif attack == "unknown_candidate_event_type_drift":
            session.get(
                SignalCandidate, unknown.signal_candidate_id
            ).event_type = "entry_signal"
        elif attack.startswith("unknown_binding_"):
            binding = (
                session.query(ExecutionBinding)
                .filter_by(strategy_instance_id=unknown.strategy_instance_id)
                .one()
            )
            if attack == "unknown_binding_chat_drift":
                binding.chat_id += 1
            elif attack == "unknown_binding_message_drift":
                binding.message_id += 1
            elif attack == "unknown_binding_symbol_drift":
                binding.symbol = "BTC"
            elif attack == "unknown_binding_side_drift":
                binding.side = "long"
            elif attack == "unknown_binding_venue_drift":
                binding.venue = "other"
        elif attack == "malformed_entry_payload":
            entry.result_json = "{"
        elif attack == "oversized_entry_payload":
            entry.result_json = json.dumps({"oversized": "x" * 20_000})
        elif attack == "deep_entry_payload":
            entry.result_json = (
                '{"status":"submitted","result":'
                + "[" * 1100
                + "0"
                + "]" * 1100
                + "}"
            )
        elif attack == "bounded_deep_entry_payload":
            payload = json.loads(entry.result_json)
            nested = 0
            for _ in range(65):
                nested = [nested]
            payload["unexpected_nested"] = nested
            entry.result_json = json.dumps(payload, separators=(",", ":"))
        elif attack == "excessive_entry_payload_nodes":
            payload = json.loads(entry.result_json)
            payload["unexpected_nodes"] = [0] * 3000
            entry.result_json = json.dumps(payload, separators=(",", ":"))
        elif attack == "duplicate_target_payload_key":
            target.error_json = (
                '{"batch_id":119,"batch_id":119,"reason":null,'
                '"status":"recovery_required","submitted":false}'
            )
        elif attack == "entry_payload_nan":
            payload = json.loads(entry.result_json)
            payload["unexpected_number"] = float("nan")
            entry.result_json = json.dumps(payload, separators=(",", ":"))
        elif attack == "entry_payload_positive_infinity":
            payload = json.loads(entry.result_json)
            payload["unexpected_number"] = float("inf")
            entry.result_json = json.dumps(payload, separators=(",", ":"))
        elif attack == "entry_payload_negative_infinity":
            payload = json.loads(entry.result_json)
            payload["unexpected_number"] = float("-inf")
            entry.result_json = json.dumps(payload, separators=(",", ":"))
        elif attack == "target_payload_has_extra_key":
            payload = json.loads(target.error_json)
            payload["unexpected"] = True
            target.error_json = json.dumps(payload, sort_keys=True)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


@pytest.mark.parametrize(
    "drift",
    [
        "raw_message_id",
        "signal_candidate_id",
        "strategy_instance_id",
        "trade_signal_id",
        "execution_binding_id",
    ],
)
def test_instruction_population_rejects_verified_contract_identity_drift(
    tmp_path,
    monkeypatch,
    drift,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-entry-terminal-mirror")
            .one()
        )
        signal_id = json.loads(item.result_json)["result"]["signal_id"]
        binding = (
            session.query(ExecutionBinding)
            .filter_by(strategy_instance_id=item.strategy_instance_id)
            .one()
        )
        contract = InstructionExecutionContract(
            message_instruction_item_id=item.id,
            raw_message_id=item.raw_message_id,
            signal_candidate_id=item.signal_candidate_id,
            strategy_instance_id=item.strategy_instance_id,
            intent_kind="entry",
            state="verified",
            state_version=2,
            trade_signal_id=signal_id,
            execution_binding_id=binding.id,
            attempted_exchange_write=True,
            terminal_kind="verified_entry",
            completion_scope="full",
            evidence_refs_json="[]",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(contract)
        session.flush()
        if drift in {
            "raw_message_id",
            "signal_candidate_id",
            "trade_signal_id",
            "execution_binding_id",
        }:
            setattr(contract, drift, 999_999)
        else:
            contract.strategy_instance_id = "deepcoin:wrong:identity"
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_instruction_population_accepts_exact_verified_contract_identity(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-entry-terminal-mirror")
            .one()
        )
        signal_id = json.loads(item.result_json)["result"]["signal_id"]
        binding = (
            session.query(ExecutionBinding)
            .filter_by(strategy_instance_id=item.strategy_instance_id)
            .one()
        )
        session.add(
            InstructionExecutionContract(
                message_instruction_item_id=item.id,
                raw_message_id=item.raw_message_id,
                signal_candidate_id=item.signal_candidate_id,
                strategy_instance_id=item.strategy_instance_id,
                intent_kind="entry",
                state="verified",
                state_version=2,
                trade_signal_id=signal_id,
                execution_binding_id=binding.id,
                attempted_exchange_write=True,
                terminal_kind="verified_entry",
                completion_scope="full",
                evidence_refs_json="[]",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "ready"


def test_instruction_population_rejects_malformed_verified_contract_evidence(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-entry-terminal-mirror")
            .one()
        )
        signal_id = json.loads(item.result_json)["result"]["signal_id"]
        binding = (
            session.query(ExecutionBinding)
            .filter_by(strategy_instance_id=item.strategy_instance_id)
            .one()
        )
        session.add(
            InstructionExecutionContract(
                message_instruction_item_id=item.id,
                raw_message_id=item.raw_message_id,
                signal_candidate_id=item.signal_candidate_id,
                strategy_instance_id=item.strategy_instance_id,
                intent_kind="entry",
                state="verified",
                state_version=2,
                trade_signal_id=signal_id,
                execution_binding_id=binding.id,
                attempted_exchange_write=True,
                terminal_kind="verified_entry",
                completion_scope="full",
                evidence_refs_json="{",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


@pytest.mark.parametrize(
    "field",
    [
        "visibility_next_attempt_at",
        "execution_deadline_at",
        "operator_escalation_at",
        "last_progress_at",
        "escalation_state",
    ],
)
def test_target_workflow_marker_drift_conflicts_under_lock_without_writes(
    tmp_path,
    field,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory)
    with factory() as session:
        target = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="target-incident-instruction")
            .one()
        )
        setattr(target, field, "pending" if field == "escalation_state" else NOW)
        session.commit()

    assert _plan(factory).status == "refused"
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_unknown_workflow_marker_drift_conflicts_under_lock_without_writes(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    plan = _plan(factory)
    with factory() as session:
        unknown = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-management-unknown")
            .one()
        )
        unknown.visibility_next_attempt_at = NOW
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_approved_pending_execution_semantics_drift_conflicts_without_writes(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    plan = _plan(factory)
    with factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-pending-residue")
            .one()
        )
        candidate = session.get(SignalCandidate, item.signal_candidate_id)
        candidate.entry_text = "100-101"
        candidate.stop_loss_text = "95"
        candidate.stop_price_source = "model"
        candidate.take_profit_text = "110/120"
        candidate.leverage_text = "50"
        candidate.confidence = 0.99
        candidate.review_status = "approved"
        candidate.review_note = "semantics changed"
        session.commit()

    drifted = _plan(factory)

    assert drifted.status == "refused"
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "changed execution text"),
        ("raw_payload", '{"changed":true}'),
        ("reply_to_message_id", 999),
        ("archived_target_group", True),
        ("edit_date", NOW),
        ("source_status", "deleted"),
        ("deleted_at", NOW),
    ],
)
def test_approved_pending_raw_message_drift_is_rejected(
    tmp_path,
    monkeypatch,
    field,
    value,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        raw = session.get(RawMessage, 20003)
        setattr(raw, field, value)
        session.commit()

    assert _plan(factory).status == "refused"


def test_approved_pending_source_identity_drift_is_rejected(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    with factory() as session:
        source = Source(
            telegram_sender_id=803,
            chat_id=803,
            username="synthetic",
            display_name="Synthetic",
            custom_label="approved",
            is_active=True,
            created_at=NOW,
        )
        session.add(source)
        session.flush()
        item = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-pending-residue")
            .one()
        )
        session.get(SignalCandidate, item.signal_candidate_id).source_id = source.id
        session.commit()
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        session.query(Source).filter_by(username="synthetic").one().is_active = False
        session.commit()

    assert _plan(factory).status == "refused"


def test_approved_pending_running_mimo_run_conflicts_without_writes(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    before = _plan(factory)
    with factory() as session:
        session.add(
            MimoRecognitionRun(
                raw_message_id=20003,
                run_kind="v1_authoritative",
                contract_version="v1",
                model="mimo-v2.5",
                input_kind="text",
                input_fingerprint="synthetic-running-recognition",
                prompt_versions_json="{}",
                status="running",
                attempt_count=0,
                became_authoritative=False,
                started_at=NOW,
                created_at=NOW,
            )
        )
        session.commit()

    after = _plan(factory)

    assert before.status == "ready"
    assert after.status == "refused"
    assert after.reason_code == "additional_active_work_present"
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=before,
            expected_fingerprint=before.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_approved_pending_extraction_claim_conflicts_without_writes(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    before = _plan(factory)
    with factory() as session:
        session.add(
            MessageEvidenceExtractionClaim(
                raw_message_id=20003,
                input_fingerprint="synthetic-claim-input",
                claim_token="synthetic-claim-token",
                claimed_at=NOW,
                lease_expires_at=NOW,
            )
        )
        session.commit()

    after = _plan(factory)

    assert before.status == "ready"
    assert after.status == "refused"
    assert after.reason_code == "additional_active_work_present"
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=before,
            expected_fingerprint=before.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_approved_pending_terminal_mimo_and_evidence_drift_is_rejected(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        run = MimoRecognitionRun(
            raw_message_id=20003,
            run_kind="v1_authoritative",
            contract_version="v1",
            model="mimo-v2.5",
            input_kind="text",
            input_fingerprint="synthetic-terminal-recognition",
            prompt_versions_json="{}",
            status="completed",
            attempt_count=1,
            selected_attempt_ordinal=1,
            became_authoritative=True,
            canonical_payload_fingerprint="a" * 64,
            projection_fingerprint="b" * 64,
            started_at=NOW,
            completed_at=NOW,
            created_at=NOW,
        )
        session.add(run)
        session.flush()
        session.add_all(
            [
                MimoRecognitionAttempt(
                    run_id=run.id,
                    ordinal=1,
                    status="completed",
                    response_fingerprint="c" * 64,
                    started_at=NOW,
                    completed_at=NOW,
                    duration_ms=1,
                    created_at=NOW,
                ),
                MessageEvidenceVersion(
                    raw_message_id=20003,
                    mimo_recognition_run_id=run.id,
                    version=1,
                    input_fingerprint="synthetic-terminal-recognition",
                    model="mimo-v2.5",
                    prompt_versions_json="{}",
                    extraction_status="completed",
                    confidence=1.0,
                    text_evidence_json="{}",
                    image_evidence_json="{}",
                    normalized_evidence_json="{}",
                    created_at=NOW,
                ),
            ]
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_resume_rejects_new_valid_instruction_population_after_repair(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="additional_active_work_present",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=_snapshot(),
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "ready"
        assert session.query(ExecutionEvent).count() == 1


def test_entry_terminal_mirror_binding_drift_changes_fingerprint_and_cas(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    before = _plan(factory)
    with factory() as session:
        binding = (
            session.query(ExecutionBinding)
            .filter_by(strategy_instance_id="deepcoin:audit:entry")
            .one()
        )
        binding.status = "open"
        session.commit()

    after = _plan(factory)

    assert before.status == after.status == "ready"
    assert before.evidence["durable"]["instruction_population"]["total_count"] == 5
    assert after.evidence["durable"]["instruction_population"]["total_count"] == 5
    assert before.source_fingerprint != after.source_fingerprint
    assert before.evidence_fingerprint != after.evidence_fingerprint
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_fingerprint_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=before,
            expected_fingerprint=before.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


def test_unknown_lifecycle_full_row_drift_changes_fingerprint_and_cas(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    before = _plan(factory)
    with factory() as session:
        lifecycle = session.get(StrategyLifecycle, 9001)
        lifecycle.management_note = "durable lifecycle drift"
        session.commit()

    after = _plan(factory)

    assert before.status == after.status == "ready"
    assert before.source_fingerprint != after.source_fingerprint
    assert before.evidence_fingerprint != after.evidence_fingerprint
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_fingerprint_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=before,
            expected_fingerprint=before.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


def test_verified_entry_binding_full_row_drift_changes_fingerprint_and_cas(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    before = _plan(factory)
    with factory() as session:
        binding = (
            session.query(ExecutionBinding)
            .filter_by(strategy_instance_id="deepcoin:audit:entry")
            .one()
        )
        binding.margin_mode = "isolated"
        session.commit()

    after = _plan(factory)

    assert before.status == after.status == "ready"
    assert before.source_fingerprint != after.source_fingerprint
    assert before.evidence_fingerprint != after.evidence_fingerprint
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_fingerprint_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=before,
            expected_fingerprint=before.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_verified_management_binding_full_row_drift_changes_fingerprint_and_cas(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        unknown = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-management-unknown")
            .one()
        )
        unknown.retired_at = NOW
        session.commit()
    before = _plan(factory)
    with factory() as session:
        binding = (
            session.query(ExecutionBinding)
            .filter_by(strategy_instance_id="deepcoin:audit:historical")
            .one()
        )
        binding.margin_mode = "isolated"
        session.commit()

    after = _plan(factory)

    assert before.status == after.status == "ready"
    assert before.source_fingerprint != after.source_fingerprint
    assert before.evidence_fingerprint != after.evidence_fingerprint
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_fingerprint_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=before,
            expected_fingerprint=before.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_verified_management_binding_identity_drift_is_refused(
    tmp_path,
    monkeypatch,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    _add_audited_instruction_population(factory)
    _approve_audited_pending_residue(monkeypatch, factory)
    with factory() as session:
        unknown = (
            session.query(MessageInstructionItem)
            .filter_by(idempotency_key="audited-management-unknown")
            .one()
        )
        unknown.retired_at = NOW
        linked = session.get(StrategyManagementBatch, 901)
        session.get(ExecutionBinding, linked.execution_binding_id).symbol = "BTC"
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


_PLAN_SNAPSHOTS = {}
_MISSING_SNAPSHOT = object()


def _plan(factory, snapshot=None):
    module = _recovery_module()
    captured = snapshot or _snapshot()
    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=captured,
        planned_at=NOW,
    )
    _PLAN_SNAPSHOTS[id(plan)] = captured
    return plan


def _apply_recovery(
    module,
    session_factory,
    *,
    plan,
    snapshot=_MISSING_SNAPSHOT,
    **kwargs,
):
    if snapshot is _MISSING_SNAPSHOT:
        snapshot = _PLAN_SNAPSHOTS.get(id(plan))
    if (
        plan.position is not None
        and getattr(plan.position, "disposition", None) != "position_absent"
        and snapshot is not None
    ):
        uid_scope_hash = getattr(
            getattr(snapshot, "account_authority", None),
            "uid_scope_hash",
            None,
        )
        kwargs.setdefault(
            "prepare_writer",
            lambda: SimpleNamespace(uid_scope_hash=uid_scope_hash),
        )
        kwargs.setdefault("expected_uid_scope_hash", uid_scope_hash)
    return module.apply_composite_batch_false_state_repair(
        session_factory,
        plan=plan,
        snapshot=snapshot,
        **kwargs,
    )


def _authorize_recovery(module, session_factory, *, snapshot, **kwargs):
    uid_scope_hash = getattr(
        getattr(snapshot, "account_authority", None),
        "uid_scope_hash",
        None,
    )
    kwargs.setdefault(
        "prepare_writer",
        lambda: SimpleNamespace(uid_scope_hash=uid_scope_hash),
    )
    kwargs.setdefault("expected_uid_scope_hash", uid_scope_hash)
    return module.authorize_composite_batch_recovery_resume(
        session_factory,
        snapshot=snapshot,
        **kwargs,
    )


def _closed_position_history_row(**overrides):
    return {
        "posId": POS_ID,
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "state": "closed",
        "pos": "38",
        "closePos": "38",
        "uTime": str(int((NOW - timedelta(seconds=1)).timestamp() * 1000)),
        **overrides,
    }


def _successful_stop_trigger_row(
    *, order_id=PRIMARY_ORDER_ID, **overrides
):
    return {
        "ordId": order_id,
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "state": "filled",
        "triggerOrderType": "TPSL",
        "slTriggerPx": (
            "63000" if order_id == BACKUP_ORDER_ID else "64000"
        ),
        "sz": "38",
        "triggerTime": str(
            int((NOW - timedelta(seconds=2)).timestamp() * 1000)
        ),
        "errorCode": "0",
        **overrides,
    }


def _natural_stop_snapshot(**overrides):
    values = {
        "positions": [],
        "pending_trigger_orders": [],
        "position_history": [_closed_position_history_row()],
        "trigger_history": [_successful_stop_trigger_row()],
    }
    values.update(overrides)
    return _snapshot(**values)


def test_position_absent_accepts_one_exact_verified_natural_stop(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"
    assert plan.position.disposition == "position_absent"
    assert plan.production_writes == 0
    assert plan.exchange_calls == 0
    assert plan.evidence["natural_stop"] == {
        "purpose": "stop_loss",
        "trigger_status": "successful_terminal",
        "position_status": "closed",
        "time_relation": "trigger_not_after_close",
        "trigger_count": 1,
        "closed_position_count": 1,
        "order_ref": sha256(
            f"natural_stop_order:{PRIMARY_ORDER_ID}".encode()
        ).hexdigest(),
        "position_ref": sha256(
            f"natural_stop_position:{POS_ID}".encode()
        ).hexdigest(),
    }
    serialized = json.dumps(
        _recovery_module().serialize_composite_batch_recovery_plan(plan),
        sort_keys=True,
    )
    for sensitive in (
        POS_ID,
        PRIMARY_ORDER_ID,
        BACKUP_ORDER_ID,
        "64000",
        "63000",
        "entry_protection_response",
        "private provider response",
    ):
        assert sensitive not in serialized


def _assert_natural_stop_refusal(plan, reason_code):
    assert plan.status == "refused"
    assert plan.reason_code == reason_code
    assert plan.production_writes == 0
    assert plan.exchange_calls == 0


def test_natural_stop_refuses_manual_or_unowned_trigger(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot(
        trigger_history=[
            _successful_stop_trigger_row(order_id="manual-unowned-close")
        ]
    )

    plan = _plan(factory, snapshot)

    _assert_natural_stop_refusal(plan, "natural_stop_proof_trigger_invalid")


@pytest.mark.parametrize(
    ("trigger_overrides", "reason_code"),
    [
        ({"posId": "different-position"}, "exchange_snapshot_incomplete"),
        (
            {"closePosId": "different-position"},
            "exchange_snapshot_incomplete",
        ),
        (
            {"posId": POS_ID, "closePosId": "different-position"},
            "exchange_snapshot_incomplete",
        ),
        (
            {"slTriggerPx": None, "tpTriggerPx": "64000"},
            "natural_stop_proof_trigger_invalid",
        ),
        ({"slTriggerPx": "63999"}, "natural_stop_proof_trigger_invalid"),
        ({"sz": "1"}, "natural_stop_proof_trigger_invalid"),
        (
            {"triggerOrderType": "conditional"},
            "natural_stop_proof_trigger_invalid",
        ),
    ],
    ids=[
        "wrong_pos_id",
        "wrong_close_pos_id",
        "conflicting_position_ids",
        "take_profit_shaped",
        "wrong_stop_price",
        "wrong_size",
        "wrong_trigger_type",
    ],
)
def test_natural_stop_refuses_explicit_trigger_identity_or_economics_conflict(
    tmp_path,
    trigger_overrides,
    reason_code,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(
        factory,
        _natural_stop_snapshot(
            trigger_history=[
                _successful_stop_trigger_row(**trigger_overrides)
            ]
        ),
    )

    _assert_natural_stop_refusal(plan, reason_code)


def test_natural_stop_allows_trigger_without_optional_position_or_economics(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(
        factory,
        _natural_stop_snapshot(
            trigger_history=[
                _successful_stop_trigger_row(
                    triggerOrderType=None,
                    slTriggerPx=None,
                    sz=None,
                )
            ]
        ),
    )

    assert plan.status == "ready"
    assert plan.position.disposition == "position_absent"


@pytest.mark.parametrize(
    "position_overrides",
    [
        {"pos": "38", "closePos": "1"},
        {"pos": "38", "closePos": "0"},
        {"pos": "19", "closePos": "19"},
        {"pos": None, "closePos": "38"},
        {"pos": "38", "closePos": None},
    ],
    ids=[
        "partial_close",
        "zero_close",
        "wrong_owned_size",
        "missing_position_size",
        "missing_closed_size",
    ],
)
def test_natural_stop_refuses_explicit_incomplete_position_close_economics(
    tmp_path,
    position_overrides,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(
        factory,
        _natural_stop_snapshot(
            position_history=[
                _closed_position_history_row(**position_overrides)
            ]
        ),
    )

    _assert_natural_stop_refusal(plan, "natural_stop_proof_position_invalid")


def test_natural_stop_refuses_two_owned_stops_claiming_trigger(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot(
        trigger_history=[
            _successful_stop_trigger_row(),
            _successful_stop_trigger_row(order_id=BACKUP_ORDER_ID),
        ]
    )

    plan = _plan(factory, snapshot)

    _assert_natural_stop_refusal(plan, "natural_stop_proof_ambiguous")


@pytest.mark.parametrize(
    ("trigger_row", "reason_code"),
    [
        (
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "state": "filled",
                "triggerTime": str(
                    int((NOW - timedelta(seconds=2)).timestamp() * 1000)
                ),
                "errorCode": "0",
            },
            "natural_stop_proof_trigger_invalid",
        ),
        (
            _successful_stop_trigger_row(instId="ETH-USDT-SWAP"),
            "exchange_snapshot_incomplete",
        ),
        (
            _successful_stop_trigger_row(posSide="short"),
            "exchange_snapshot_incomplete",
        ),
    ],
    ids=["missing_order", "wrong_instrument", "wrong_side"],
)
def test_natural_stop_refuses_incomplete_trigger_identity(
    tmp_path, trigger_row, reason_code
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(
        factory,
        _natural_stop_snapshot(trigger_history=[trigger_row]),
    )

    _assert_natural_stop_refusal(plan, reason_code)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("purpose", "take_profit"),
        ("execution_binding_id", 999999),
        ("execution_order_leg_id", 999999),
    ],
    ids=["purpose", "binding_owner", "entry_owner"],
)
def test_natural_stop_refuses_ledger_purpose_or_owner_conflict(
    tmp_path, field, value
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    with factory() as session:
        primary = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=PRIMARY_ORDER_ID)
            .one()
        )
        setattr(primary, field, value)
        session.commit()

    plan = _plan(factory, snapshot)

    _assert_natural_stop_refusal(plan, "durable_snapshot_scope_mismatch")


@pytest.mark.parametrize(
    ("position_history", "trigger_history", "reason_code"),
    [
        ([_closed_position_history_row()], [
            _successful_stop_trigger_row(triggerTime=None)
        ], "natural_stop_proof_time_invalid"),
        ([_closed_position_history_row()], [
            _successful_stop_trigger_row(triggerTime="malformed")
        ], "natural_stop_proof_time_invalid"),
        ([_closed_position_history_row()], [
            _successful_stop_trigger_row(
                triggerTime=str(
                    int(datetime(2036, 1, 1, tzinfo=UTC).timestamp() * 1000)
                )
            )
        ], "natural_stop_proof_after_capture"),
        ([_closed_position_history_row(uTime=None)], [
            _successful_stop_trigger_row()
        ], "natural_stop_proof_time_invalid"),
        ([_closed_position_history_row(uTime="malformed")], [
            _successful_stop_trigger_row()
        ], "natural_stop_proof_time_invalid"),
        ([_closed_position_history_row(
            uTime=str(
                int(datetime(2036, 1, 1, tzinfo=UTC).timestamp() * 1000)
            )
        )], [_successful_stop_trigger_row()], "natural_stop_proof_after_capture"),
        ([_closed_position_history_row(
            uTime=str(int((NOW - timedelta(seconds=3)).timestamp() * 1000))
        )], [_successful_stop_trigger_row()], "natural_stop_proof_time_invalid"),
    ],
    ids=[
        "missing_trigger_time",
        "malformed_trigger_time",
        "future_trigger_time",
        "missing_close_time",
        "malformed_close_time",
        "future_close_time",
        "close_before_trigger",
    ],
)
def test_natural_stop_refuses_invalid_or_reversed_time_relation(
    tmp_path, position_history, trigger_history, reason_code
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(
        factory,
        _natural_stop_snapshot(
            position_history=position_history,
            trigger_history=trigger_history,
        ),
    )

    _assert_natural_stop_refusal(plan, reason_code)


def test_natural_stop_refuses_2001_history_before_incident_boundary(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    stale_trigger_ms = str(
        int(datetime(2001, 1, 1, tzinfo=UTC).timestamp() * 1000)
    )
    stale_close_ms = str(
        int(datetime(2001, 1, 1, 0, 0, 1, tzinfo=UTC).timestamp() * 1000)
    )

    plan = _plan(
        factory,
        _natural_stop_snapshot(
            position_history=[_closed_position_history_row(uTime=stale_close_ms)],
            trigger_history=[
                _successful_stop_trigger_row(triggerTime=stale_trigger_ms)
            ],
        ),
    )

    _assert_natural_stop_refusal(
        plan, "natural_stop_proof_before_incident"
    )


def test_natural_stop_refuses_2036_history_after_capture_boundary(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    future_trigger_ms = str(
        int(datetime(2036, 1, 1, tzinfo=UTC).timestamp() * 1000)
    )
    future_close_ms = str(
        int(datetime(2036, 1, 1, 0, 0, 1, tzinfo=UTC).timestamp() * 1000)
    )

    plan = _plan(
        factory,
        _natural_stop_snapshot(
            position_history=[_closed_position_history_row(uTime=future_close_ms)],
            trigger_history=[
                _successful_stop_trigger_row(triggerTime=future_trigger_ms)
            ],
        ),
    )

    _assert_natural_stop_refusal(plan, "natural_stop_proof_after_capture")


@pytest.mark.parametrize(
    "position_history",
    [
        [],
        [_closed_position_history_row(state="live")],
        [
            _closed_position_history_row(),
            _closed_position_history_row(uTime=str(int(NOW.timestamp() * 1000))),
        ],
    ],
    ids=["missing", "not_closed", "ambiguous"],
)
def test_natural_stop_refuses_missing_or_ambiguous_closed_position_history(
    tmp_path, position_history
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(
        factory,
        _natural_stop_snapshot(position_history=position_history),
    )

    _assert_natural_stop_refusal(plan, "natural_stop_proof_position_invalid")


def test_natural_stop_refuses_when_current_position_still_exists(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(
        factory,
        _natural_stop_snapshot(positions=_snapshot().positions),
    )

    _assert_natural_stop_refusal(
        plan, "exchange_close_submission_evidence_present"
    )


@pytest.mark.parametrize(
    "position_row",
    [
        {
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "1",
        },
        {
            "posId": "different-live-position",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "1",
        },
    ],
    ids=["missing_pos_id", "wrong_pos_id"],
)
def test_natural_stop_refuses_nonzero_position_identity_conflict_before_apply(
    tmp_path, position_row
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(
        factory,
        _natural_stop_snapshot(positions=[position_row]),
    )

    _assert_natural_stop_refusal(plan, "exact_position_identity_conflict")
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="plan_not_actionable",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_natural_stop_ignores_valid_zero_position_row(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(
        factory,
        _natural_stop_snapshot(
            positions=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "0",
                }
            ]
        ),
    )

    assert plan.status == "ready"
    assert plan.position.disposition == "position_absent"


def test_natural_stop_ignores_valid_opposite_side_position_row(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(
        factory,
        _natural_stop_snapshot(
            positions=[
                {
                    "posId": "unrelated-short-position",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "pos": "2",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "0",
                },
            ]
        ),
    )

    assert plan.status == "ready"
    assert plan.position.disposition == "position_absent"


@pytest.mark.parametrize(
    "position_row",
    [
        {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "NaN"},
        {"instId": "BTC-USDT-SWAP", "pos": "0"},
        {"posSide": "long", "pos": "0"},
    ],
    ids=["malformed_size", "missing_side", "missing_instrument"],
)
def test_natural_stop_refuses_malformed_zero_or_unknown_position_row(
    tmp_path, position_row
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(
        factory,
        _natural_stop_snapshot(positions=[position_row]),
    )

    _assert_natural_stop_refusal(plan, "exact_position_snapshot_invalid")


@pytest.mark.parametrize(
    "field",
    ["request_json", "response_json", "client_order_id", "exchange_order_id"],
)
def test_natural_stop_refuses_new_durable_management_submission_field(
    tmp_path, field
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    with factory() as session:
        leg = session.query(StrategyManagementLeg).one()
        setattr(
            leg,
            field,
            '{"private":"value"}' if field.endswith("json") else "private-id",
        )
        session.commit()

    plan = _plan(factory, snapshot)

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_refuses_new_durable_close_mutation(tmp_path):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    snapshot = _natural_stop_snapshot()
    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="natural-stop-conflicting-close",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json='{"private":"request"}',
                response_json='{"private":"response"}',
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, snapshot)

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_refuses_new_durable_close_event(tmp_path):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    with factory() as session:
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                action="strategy_management_close_submit",
                status="submitted",
                symbol="BTC",
                side="long",
                pos_id=POS_ID,
                request_json='{"private":"request"}',
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, snapshot)

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_refuses_owned_close_mutation_with_wrong_position(tmp_path):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="owned-close-wrong-position",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id="wrong-position",
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json="{}",
                response_json="{}",
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_refuses_target_entry_close_mutation_with_wrong_binding(
    tmp_path,
):
    factory, _, _, entry_id, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        other_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:other:btc:long",
            kol_id="other-source",
            chat_id=998,
            message_id=998,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="other-binding-position",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add(other_binding)
        session.flush()
        session.add(
            PositionMutationIntent(
                idempotency_key="target-entry-close-wrong-binding",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=other_binding.id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json="{}",
                response_json="{}",
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_refuses_target_binding_and_position_other_entry_mutation(
    tmp_path,
):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        other_entry = ExecutionOrderLeg(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:incident:btc:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="other-entry-target-position-order",
            pos_id=POS_ID,
            venue="other",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
        )
        session.add(other_entry)
        session.flush()
        session.add(
            PositionMutationIntent(
                idempotency_key="target-binding-other-entry-target-position",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=other_entry.id,
                pos_id=POS_ID,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json="{}",
                response_json="{}",
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def _add_target_close_mutation_with_operation(
    session,
    *,
    binding_id,
    entry_id,
    operation,
    idempotency_key,
):
    session.add(
        PositionMutationIntent(
            idempotency_key=idempotency_key,
            operation=operation,
            strategy_instance_id="deepcoin:incident:btc:long",
            execution_binding_id=binding_id,
            execution_order_leg_id=entry_id,
            pos_id=POS_ID,
            authority_fingerprint="a" * 64,
            request_fingerprint="r" * 64,
            status="confirmed",
            request_json="{}",
            response_json="{}",
            reserved_at=NOW,
            submitted_at=NOW,
            confirmed_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )


@pytest.mark.parametrize(
    "operation",
    [
        "CLOSE_POSITION",
        " close_position ",
        "\tCLOSE_POSITION\n",
        "\u200bCLOSE_POSITION\u200b",
        "\ufeffCLOSE_POSITION\ufeff",
        "\u2060CLOSE_POSITION\u2060",
        "\u200b\ufeff\u2060CLOSE_POSITION\u2060\ufeff\u200b",
        "close\u200b_position",
        "ＣＬＯＳＥ＿ＰＯＳＩＴＩＯＮ",
        "cl\u2065ose_position",
        "cl\u0378ose_position",
        "cl\x00ose_position",
        "cl\bose_position",
        "cl\x7fose_position",
        "cl!ose_position",
        *(f"close_pos{chr(codepoint)}ition" for codepoint in range(0xFFF0, 0xFFF9)),
    ],
)
def test_natural_stop_refuses_normalized_target_close_mutation_operation(
    tmp_path,
    operation,
):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        _add_target_close_mutation_with_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            operation=operation,
            idempotency_key=(
                "normalized-operation-"
                + sha256(operation.encode()).hexdigest()[:16]
            ),
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_apply_cas_refuses_late_normalized_close_operation(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory, _natural_stop_snapshot())
    with factory() as session:
        _add_target_close_mutation_with_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            operation="cl\x00\u2065ose_pos\ufff8ition",
            idempotency_key="late-uppercase-close-operation",
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


def test_natural_stop_resume_refuses_late_normalized_close_operation(tmp_path):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        _add_target_close_mutation_with_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            operation="cl\b!\x7fose_position",
            idempotency_key="late-spaced-close-operation",
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


def test_natural_stop_allows_target_mutation_with_unrelated_operation(tmp_path):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        _add_target_close_mutation_with_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            operation="adjust_position_margin",
            idempotency_key="unrelated-target-mutation-operation",
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


def _add_target_mutation_with_stored_operation(
    session,
    *,
    binding_id,
    entry_id,
    stored_operation,
    idempotency_key,
):
    _add_target_close_mutation_with_operation(
        session,
        binding_id=binding_id,
        entry_id=entry_id,
        operation="temporary_operation_placeholder",
        idempotency_key=idempotency_key,
    )
    session.flush()
    if stored_operation is None:
        connection = session.connection()
        schema_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'position_mutation_intents'"
        ).scalar_one()
        nullable_schema_sql = schema_sql.replace(
            "operation VARCHAR(64) NOT NULL",
            "operation VARCHAR(64)",
        )
        assert nullable_schema_sql != schema_sql
        connection.exec_driver_sql("PRAGMA writable_schema = ON")
        connection.exec_driver_sql(
            "UPDATE sqlite_master SET sql = ? "
            "WHERE type = 'table' AND name = 'position_mutation_intents'",
            (nullable_schema_sql,),
        )
        schema_version = connection.exec_driver_sql(
            "PRAGMA schema_version"
        ).scalar_one()
        connection.exec_driver_sql(f"PRAGMA schema_version = {schema_version + 1}")
        connection.exec_driver_sql("PRAGMA writable_schema = OFF")
    session.execute(
        text(
            "UPDATE position_mutation_intents "
            "SET operation = :operation WHERE idempotency_key = :key"
        ),
        {"operation": stored_operation, "key": idempotency_key},
    )


@pytest.mark.parametrize(
    "stored_operation",
    [b"close_position", memoryview(b"close_position"), None],
    ids=["bytes", "memoryview", "null"],
)
def test_natural_stop_refuses_target_non_string_mutation_operation(
    tmp_path,
    stored_operation,
):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        _add_target_mutation_with_stored_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            stored_operation=stored_operation,
            idempotency_key="target-non-string-plan",
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


@pytest.mark.parametrize(
    "stored_operation",
    [b"close_position", None],
    ids=["bytes", "null"],
)
def test_natural_stop_apply_refuses_late_target_non_string_operation(
    tmp_path,
    stored_operation,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory, _natural_stop_snapshot())
    with factory() as session:
        _add_target_mutation_with_stored_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            stored_operation=stored_operation,
            idempotency_key="target-non-string-apply",
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


@pytest.mark.parametrize(
    "stored_operation",
    [memoryview(b"close_position"), None],
    ids=["memoryview", "null"],
)
def test_natural_stop_resume_refuses_late_target_non_string_operation(
    tmp_path,
    stored_operation,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        _add_target_mutation_with_stored_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            stored_operation=stored_operation,
            idempotency_key="target-non-string-resume",
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("population", [256, 257])
def test_natural_stop_bounds_all_mutation_candidates(tmp_path, population):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        for index in range(population):
            _add_target_close_mutation_with_operation(
                session,
                binding_id=binding_id,
                entry_id=entry_id,
                operation="adjust_position_margin",
                idempotency_key=f"bounded-unrelated-mutation-{index}",
            )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    if population == 256:
        assert plan.status == "ready"
    else:
        _assert_natural_stop_refusal(
            plan, "durable_close_submission_evidence_present"
        )


def test_natural_stop_allows_early_unrelated_mutation_history(tmp_path):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        _add_target_close_mutation_with_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            operation="adjust_position_margin",
            idempotency_key="early-unrelated-mutation-history",
        )
        session.flush()
        mutation = (
            session.query(PositionMutationIntent)
            .filter_by(idempotency_key="early-unrelated-mutation-history")
            .one()
        )
        mutation.created_at = datetime(2001, 1, 1, tzinfo=UTC)
        mutation.updated_at = datetime(2001, 1, 1, tzinfo=UTC)
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


def test_natural_stop_allows_nonclose_mutation_with_malformed_unread_fields(
    tmp_path,
):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        _add_target_close_mutation_with_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            operation="adjust_position_margin",
            idempotency_key="malformed-mutation-created-at",
        )
        session.flush()
        session.execute(
            text(
                "UPDATE position_mutation_intents "
                "SET reserved_at = 'bad-reserved', "
                "submitted_at = 'bad-submitted', "
                "confirmed_at = 'bad-confirmed', "
                "updated_at = 'bad-updated', "
                "request_json = x'ff', response_json = x'00', "
                "error_json = 'malformed payload' "
                "WHERE idempotency_key = 'malformed-mutation-created-at'"
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


@pytest.mark.parametrize(
    "field_name",
    [
        "reserved_at",
        "submitted_at",
        "confirmed_at",
        "created_at",
        "updated_at",
        "request_json",
        "response_json",
        "error_json",
    ],
)
def test_natural_stop_nonclose_blob_in_unread_field_is_ignored(
    tmp_path,
    field_name,
):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        _add_target_close_mutation_with_operation(
            session,
            binding_id=binding_id,
            entry_id=entry_id,
            operation="adjust_position_margin",
            idempotency_key=f"nonclose-unread-blob-{field_name}",
        )
        session.flush()
        session.execute(
            text(
                f"UPDATE position_mutation_intents SET {field_name} = x'ff' "
                "WHERE idempotency_key = :key"
            ),
            {"key": f"nonclose-unread-blob-{field_name}"},
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


def test_close_operation_ascii_skeleton_never_misses_single_codepoint():
    module = _recovery_module()
    missed = []
    for codepoint in range(sys.maxunicode + 1):
        operation = f"cl{chr(codepoint)}ose_position"
        normalized = unicodedata.normalize("NFKC", operation).casefold()
        skeleton = "".join(
            character
            for character in normalized
            if (
                "a" <= character <= "z"
                or "0" <= character <= "9"
                or character == "_"
            )
        )
        if (
            skeleton == "close_position"
            and not module._mutation_operation_is_close_looking(operation)
        ):
            missed.append(codepoint)
            if len(missed) == 10:
                break

    assert missed == []


@pytest.mark.parametrize("strategy_instance_id", ["wrong-strategy", ""])
def test_natural_stop_refuses_owned_close_mutation_with_strategy_conflict(
    tmp_path,
    strategy_instance_id,
):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key=f"owned-close-strategy-{strategy_instance_id}",
                operation="close_position",
                strategy_instance_id=strategy_instance_id,
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json="{}",
                response_json="{}",
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


@pytest.mark.parametrize("pos_id", [None, "wrong-position"])
def test_natural_stop_refuses_owned_close_event_without_exact_position(
    tmp_path, pos_id
):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                action="strategy_management_close_submit",
                status="submitted",
                pos_id=pos_id,
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


@pytest.mark.parametrize("strategy_instance_id", [None, "wrong-strategy"])
def test_natural_stop_refuses_same_binding_close_event_owner_conflict(
    tmp_path,
    strategy_instance_id,
):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                strategy_instance_id=strategy_instance_id,
                action="strategy_management_close_submit",
                status="submitted",
                pos_id=POS_ID,
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def _add_partially_linked_target_close_event(session, *, leg_id):
    session.add(
        ExecutionEvent(
            execution_binding_id=None,
            strategy_instance_id="deepcoin:incident:btc:long",
            action="strategy_management_close_submit",
            status="submitted",
            pos_id=POS_ID,
            before_json=json.dumps(
                {"management_batch_id": 119, "management_leg_id": leg_id},
                sort_keys=True,
            ),
            created_at=NOW,
        )
    )


def test_natural_stop_refuses_partially_linked_target_event_before_plan(
    tmp_path,
):
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_partially_linked_target_close_event(session, leg_id=leg_id)
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_apply_cas_refuses_late_partially_linked_target_event(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory, _natural_stop_snapshot())
    with factory() as session:
        _add_partially_linked_target_close_event(session, leg_id=leg_id)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


def test_natural_stop_resume_refuses_late_partially_linked_target_event(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        _add_partially_linked_target_close_event(session, leg_id=leg_id)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


def test_natural_stop_refuses_malformed_target_payload_close_event(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.add(
            ExecutionEvent(
                execution_binding_id=None,
                strategy_instance_id=None,
                action="strategy_management_close_submit",
                status="submitted",
                pos_id=None,
                before_json='{"management_batch_id":119,',
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_refuses_nested_string_target_payload_close_event(
    tmp_path,
):
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.add(
            ExecutionEvent(
                execution_binding_id=None,
                strategy_instance_id=None,
                action="strategy_management_close_submit",
                status="submitted",
                pos_id=None,
                before_json=json.dumps(
                    {
                        "owner": {
                            "management_batch_id": "119",
                            "management_leg_id": str(leg_id),
                        }
                    },
                    sort_keys=True,
                ),
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def _add_whitespace_payload_only_target_close_event(session, *, leg_id):
    session.add(
        ExecutionEvent(
            execution_binding_id=None,
            strategy_instance_id=None,
            action="strategy_management_close_submit",
            status="submitted",
            pos_id=None,
            before_json=(
                '{"owner":{"management_batch_id":\n119,'
                f'"management_leg_id":\t{leg_id}}}}}'
            ),
            created_at=NOW,
        )
    )


def test_natural_stop_apply_cas_refuses_late_whitespace_payload_target_event(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory, _natural_stop_snapshot())
    with factory() as session:
        _add_whitespace_payload_only_target_close_event(session, leg_id=leg_id)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


def test_natural_stop_resume_refuses_late_whitespace_payload_target_event(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        _add_whitespace_payload_only_target_close_event(session, leg_id=leg_id)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


def _add_escaped_key_payload_only_target_close_event(session, *, leg_id):
    session.add(
        ExecutionEvent(
            execution_binding_id=None,
            strategy_instance_id=None,
            action="strategy_management_close_submit",
            status="submitted",
            pos_id=None,
            before_json=(
                '{"owner":{"management_\\u0062atch_id":"119",'
                f'"management_\\u006ceg_id":"{leg_id}"}}}}'
            ),
            created_at=NOW,
        )
    )


def test_natural_stop_refuses_escaped_key_payload_target_event(tmp_path):
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_escaped_key_payload_only_target_close_event(session, leg_id=leg_id)
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_apply_cas_refuses_late_escaped_key_target_event(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory, _natural_stop_snapshot())
    with factory() as session:
        _add_escaped_key_payload_only_target_close_event(session, leg_id=leg_id)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


def test_natural_stop_resume_refuses_late_escaped_key_target_event(tmp_path):
    module = _recovery_module()
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        _add_escaped_key_payload_only_target_close_event(session, leg_id=leg_id)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


def test_natural_stop_apply_cas_rejects_late_wrong_position_close_mutation(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory, _natural_stop_snapshot())
    assert plan.status == "ready"
    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="late-owned-close-wrong-position",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id="late-wrong-position",
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json="{}",
                response_json="{}",
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_natural_stop_does_not_claim_other_entry_close_mutation(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        other_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:complete-other:btc:long",
            kol_id="complete-other-source",
            chat_id=997,
            message_id=997,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="complete-other-position",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add(other_binding)
        session.flush()
        other_entry = ExecutionOrderLeg(
            execution_binding_id=other_binding.id,
            strategy_instance_id=other_binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="other-entry-order",
            pos_id=other_binding.pos_id,
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
        )
        session.add(other_entry)
        session.flush()
        other_batch = _add_other_terminal_batch(
            session,
            binding_id=other_binding.id,
            batch119=session.get(StrategyManagementBatch, 119),
        )
        other_batch.strategy_instance_id = other_binding.strategy_instance_id
        other_leg = StrategyManagementLeg(
            management_batch_id=other_batch.id,
            execution_order_leg_id=other_entry.id,
            pos_id=other_binding.pos_id,
            leg_index=0,
            status="succeeded",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(other_leg)
        session.flush()
        session.add(
            PositionMutationIntent(
                idempotency_key="other-entry-close",
                operation="close_position",
                strategy_instance_id=other_binding.strategy_instance_id,
                execution_binding_id=other_binding.id,
                execution_order_leg_id=other_entry.id,
                pos_id=other_binding.pos_id,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json=json.dumps(
                    {
                        "management_batch_id": other_batch.id,
                        "management_leg_id": other_leg.id,
                    },
                    sort_keys=True,
                ),
                response_json="{}",
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


def test_natural_stop_does_not_claim_other_binding_close_event(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        other_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:other:btc:long",
            kol_id="other-source",
            chat_id=999,
            message_id=999,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="other-binding-position",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add(other_binding)
        session.flush()
        session.add(
            ExecutionEvent(
                execution_binding_id=other_binding.id,
                strategy_instance_id=other_binding.strategy_instance_id,
                action="strategy_management_close_submit",
                status="submitted",
                pos_id=None,
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


def test_natural_stop_does_not_claim_other_entry_management_close_event(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch119 = session.get(StrategyManagementBatch, 119)
        other_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:complete-other-event:btc:long",
            kol_id="complete-other-event-source",
            chat_id=996,
            message_id=996,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="complete-other-event-position",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add(other_binding)
        session.flush()
        other_entry = ExecutionOrderLeg(
            execution_binding_id=other_binding.id,
            strategy_instance_id=other_binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="other-event-entry-order",
            pos_id=other_binding.pos_id,
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
        )
        session.add(other_entry)
        session.flush()
        other_batch = _add_other_terminal_batch(
            session,
            binding_id=other_binding.id,
            batch119=batch119,
        )
        other_batch.strategy_instance_id = other_binding.strategy_instance_id
        other_leg = StrategyManagementLeg(
            management_batch_id=other_batch.id,
            execution_order_leg_id=other_entry.id,
            pos_id=other_binding.pos_id,
            leg_index=0,
            status="succeeded",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(other_leg)
        session.flush()
        event_link = json.dumps(
            {"management_batch_id": other_batch.id, "management_leg_id": other_leg.id},
            sort_keys=True,
        )
        session.add(
            ExecutionEvent(
                execution_binding_id=other_binding.id,
                strategy_instance_id=other_binding.strategy_instance_id,
                action="strategy_management_close_submit",
                status="submitted",
                pos_id=other_binding.pos_id,
                before_json=event_link,
                after_json=event_link,
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


def _add_complete_other_close_event(
    session,
    *,
    batch119,
    suffix,
    response_json,
):
    other_binding = ExecutionBinding(
        strategy_instance_id=f"deepcoin:encoded-other-{suffix}:btc:long",
        kol_id=f"encoded-other-{suffix}",
        chat_id=995,
        message_id=995,
        symbol="BTC",
        side="long",
        venue="deepcoin",
        pos_id=f"encoded-other-position-{suffix}",
        margin_mode="cross",
        position_mode="split",
        status="active",
    )
    session.add(other_binding)
    session.flush()
    other_entry = ExecutionOrderLeg(
        execution_binding_id=other_binding.id,
        strategy_instance_id=other_binding.strategy_instance_id,
        leg_index=1,
        purpose="entry",
        order_kind="market",
        order_id=f"encoded-other-entry-{suffix}",
        pos_id=other_binding.pos_id,
        venue="deepcoin",
        attribution_status="verified",
        attribution_evidence_json='{"policy_version":2}',
        status="active",
    )
    session.add(other_entry)
    session.flush()
    other_batch = _add_other_terminal_batch(
        session,
        binding_id=other_binding.id,
        batch119=batch119,
    )
    other_batch.strategy_instance_id = other_binding.strategy_instance_id
    other_leg = StrategyManagementLeg(
        management_batch_id=other_batch.id,
        execution_order_leg_id=other_entry.id,
        pos_id=other_binding.pos_id,
        leg_index=0,
        status="succeeded",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(other_leg)
    session.flush()
    owner_json = json.dumps(
        {
            "management_batch_id": other_batch.id,
            "management_leg_id": other_leg.id,
        },
        sort_keys=True,
    )
    session.add(
        ExecutionEvent(
            execution_binding_id=other_binding.id,
            strategy_instance_id=other_binding.strategy_instance_id,
            action="strategy_management_close_submit",
            status="submitted",
            pos_id=other_binding.pos_id,
            before_json=owner_json,
            response_json=response_json,
            created_at=NOW,
        )
    )
    return other_binding, other_entry


def _add_other_suspicious_close_mutation(
    session,
    *,
    batch119,
    suffix,
    complete_owner=False,
    operation="ＣＬＯＳＥ＿ＰＯＳＩＴＩＯＮ",
):
    other_binding, other_entry = _add_complete_other_close_event(
        session,
        batch119=batch119,
        suffix=f"fullwidth-{suffix}",
        response_json=None,
    )
    session.add(
        PositionMutationIntent(
            idempotency_key=f"incomplete-other-fullwidth-{suffix}",
            operation=operation,
            strategy_instance_id=(
                other_binding.strategy_instance_id
                if complete_owner
                else "conflicting-incomplete-other-owner"
            ),
            execution_binding_id=other_binding.id,
            execution_order_leg_id=other_entry.id,
            pos_id=other_binding.pos_id,
            authority_fingerprint="a" * 64,
            request_fingerprint="r" * 64,
            status="confirmed",
            request_json="{}",
            response_json="{}",
            reserved_at=NOW,
            submitted_at=NOW,
            confirmed_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )


@pytest.mark.parametrize(
    "operation",
    ["cl\x00ose_position", "cl\u2065!\x00ose_position"],
)
def test_natural_stop_allows_suspicious_close_with_complete_other_owner(
    tmp_path,
    operation,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_other_suspicious_close_mutation(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="complete-owner",
            complete_owner=True,
            operation=operation,
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


def test_natural_stop_refuses_complete_other_close_with_malformed_time(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_other_suspicious_close_mutation(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="malformed-time",
            complete_owner=True,
            operation="close_position",
        )
        session.flush()
        session.execute(
            text(
                "UPDATE position_mutation_intents SET reserved_at = 'bad-time' "
                "WHERE idempotency_key = "
                "'incomplete-other-fullwidth-malformed-time'"
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


@pytest.mark.parametrize(
    "field_name",
    [
        "reserved_at",
        "submitted_at",
        "confirmed_at",
        "created_at",
        "updated_at",
        "request_json",
        "response_json",
        "error_json",
    ],
)
def test_natural_stop_close_blob_in_required_field_fails_closed(
    tmp_path,
    field_name,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_other_suspicious_close_mutation(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix=f"close-blob-{field_name}",
            complete_owner=True,
            operation="close_position",
        )
        session.flush()
        session.execute(
            text(
                f"UPDATE position_mutation_intents SET {field_name} = x'ff' "
                "WHERE idempotency_key = :key"
            ),
            {"key": f"incomplete-other-fullwidth-close-blob-{field_name}"},
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def _recovery_write_state(factory):
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        return (
            batch.status,
            batch.updated_at,
            batch.target_snapshot_json,
            batch.reason_code,
            batch.completed_at,
            session.query(ExecutionEvent).count(),
            session.query(StrategyManagementComponent).count(),
        )


def _patch_recovery_query_operational_error(monkeypatch, *, query_kind):
    original_all = Query.all

    def fail_selected_query(query):
        entities = {
            description.get("entity")
            for description in query.column_descriptions
        }
        statement = str(query.statement)
        is_mutation = PositionMutationIntent in entities
        should_fail = (
            query_kind == "mutation_stage1"
            and is_mutation
            and len(query.column_descriptions) == 2
        ) or (
            query_kind == "mutation_stage2"
            and is_mutation
            and len(query.column_descriptions) > 2
        ) or (
            query_kind == "event"
            and ExecutionEvent in entities
            and "execution_events.action LIKE" in statement
        )
        if should_fail:
            raise OperationalError(
                "forced bounded recovery query failure",
                {},
                RuntimeError("forced query failure"),
            )
        return original_all(query)

    monkeypatch.setattr(Query, "all", fail_selected_query)


@pytest.mark.parametrize(
    "query_kind",
    ["mutation_stage1", "mutation_stage2", "event"],
)
@pytest.mark.parametrize("phase", ["plan", "apply", "resume"])
def test_natural_stop_query_operational_error_fails_closed_without_writes(
    tmp_path,
    monkeypatch,
    query_kind,
    phase,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    if query_kind == "mutation_stage2":
        with factory() as session:
            _add_other_suspicious_close_mutation(
                session,
                batch119=session.get(StrategyManagementBatch, 119),
                suffix=f"query-error-{phase}",
                complete_owner=True,
                operation="close_position",
            )
            session.commit()
    plan = None
    if phase in {"apply", "resume"}:
        plan = _plan(factory, snapshot)
        assert plan.status == "ready"
    if phase == "resume":
        _apply_recovery(module,
            factory,
            plan=plan,
            snapshot=snapshot,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    before = _recovery_write_state(factory)
    _patch_recovery_query_operational_error(monkeypatch, query_kind=query_kind)

    if phase == "plan":
        refused = _plan(factory, snapshot)
        _assert_natural_stop_refusal(
            refused, "durable_close_submission_evidence_present"
        )
    elif phase == "apply":
        with pytest.raises(
            module.CompositeBatchRecoveryConflict,
            match="source_state_conflict",
        ):
            _apply_recovery(module,
                factory,
                plan=plan,
                expected_fingerprint=plan.evidence_fingerprint,
                authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
                applied_at=NOW,
            )
    else:
        with pytest.raises(
            module.CompositeBatchRecoveryConflict,
            match="resume_source_state_conflict",
        ):
            _authorize_recovery(module,
                factory,
                expected_fingerprint=plan.evidence_fingerprint,
                snapshot=snapshot,
            )

    assert _recovery_write_state(factory) == before


def _concurrent_close_writer_statement(
    factory,
    *,
    scenario,
    binding_id,
    entry_id,
):
    if scenario == "operation":
        with factory() as session:
            _add_target_close_mutation_with_operation(
                session,
                binding_id=binding_id,
                entry_id=entry_id,
                operation="adjust_position_margin",
                idempotency_key="concurrent-operation-close",
            )
            session.commit()
        return (
            "UPDATE position_mutation_intents SET operation = 'close_position' "
            "WHERE idempotency_key = 'concurrent-operation-close'",
            (),
        )
    if scenario == "owner":
        with factory() as session:
            _add_other_suspicious_close_mutation(
                session,
                batch119=session.get(StrategyManagementBatch, 119),
                suffix="concurrent-owner",
                complete_owner=True,
                operation="close_position",
            )
            session.commit()
        return (
            "UPDATE position_mutation_intents "
            "SET strategy_instance_id = ?, execution_binding_id = ?, "
            "execution_order_leg_id = ?, pos_id = ? "
            "WHERE idempotency_key = "
            "'incomplete-other-fullwidth-concurrent-owner'",
            (
                "deepcoin:incident:btc:long",
                binding_id,
                entry_id,
                POS_ID,
            ),
        )
    timestamp = NOW.isoformat(sep=" ")
    return (
        "INSERT INTO position_mutation_intents ("
        "idempotency_key, venue, operation, strategy_instance_id, "
        "execution_binding_id, execution_order_leg_id, pos_id, "
        "authority_fingerprint, request_fingerprint, status, request_json, "
        "response_json, reserved_at, submitted_at, confirmed_at, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "concurrent-insert-close",
            "deepcoin",
            "close_position",
            "deepcoin:incident:btc:long",
            binding_id,
            entry_id,
            POS_ID,
            "a" * 64,
            "r" * 64,
            "confirmed",
            "{}",
            "{}",
            timestamp,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


@pytest.mark.parametrize("phase", ["plan", "resume"])
@pytest.mark.parametrize("scenario", ["operation", "owner", "insert"])
def test_natural_stop_db_evidence_window_blocks_external_close_writer(
    tmp_path,
    monkeypatch,
    phase,
    scenario,
):
    module = _recovery_module()
    factory, database, binding_id, entry_id, _ = (
        _seed_batch_119_false_submission(tmp_path)
    )
    statement, parameters = _concurrent_close_writer_statement(
        factory,
        scenario=scenario,
        binding_id=binding_id,
        entry_id=entry_id,
    )
    snapshot = _natural_stop_snapshot()
    plan = None
    if phase == "resume":
        plan = _plan(factory, snapshot)
        assert plan.status == "ready"
        _apply_recovery(module,
            factory,
            plan=plan,
            snapshot=snapshot,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    writer_done = threading.Event()
    writer_committed = threading.Event()
    writer_errors = []

    def external_writer():
        connection = sqlite3.connect(str(database), timeout=0.05)
        try:
            connection.execute(statement, parameters)
            connection.commit()
            writer_committed.set()
        except sqlite3.OperationalError as exc:
            writer_errors.append(str(exc))
            connection.rollback()
        finally:
            connection.close()
            writer_done.set()

    original = module._has_durable_close_submission
    invoked = False

    def interleave_after_evidence_read(*args, **kwargs):
        nonlocal invoked
        result = original(*args, **kwargs)
        if not invoked:
            invoked = True
            writer = threading.Thread(target=external_writer, daemon=True)
            writer.start()
            assert writer_done.wait(2)
        return result

    monkeypatch.setattr(
        module,
        "_has_durable_close_submission",
        interleave_after_evidence_read,
    )

    if phase == "plan":
        result = _plan(factory, snapshot)
        assert result.status == "ready"
    else:
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )

    assert invoked is True
    assert writer_committed.is_set() is False
    assert writer_errors and "locked" in writer_errors[0].lower()


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_natural_stop_planner_lock_is_dry_run_database_byte_identical(
    tmp_path,
    journal_mode,
):
    factory, database, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    engine = factory.kw["bind"]
    engine.dispose()
    with sqlite3.connect(str(database)) as connection:
        actual_mode = connection.execute(
            f"PRAGMA journal_mode = {journal_mode}"
        ).fetchone()[0]
    assert str(actual_mode).upper() == journal_mode
    before = Path(database).read_bytes()
    sidecars = [
        Path(f"{database}-journal"),
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    ]
    before_sidecars = {
        path.name: path.read_bytes() if path.exists() else None
        for path in sidecars
    }

    plan = _plan(factory, _natural_stop_snapshot())
    engine.dispose()

    assert plan.status == "ready"
    assert Path(database).read_bytes() == before
    assert {
        path.name: path.read_bytes() if path.exists() else None
        for path in sidecars
    } == before_sidecars


@pytest.mark.parametrize(
    "operation",
    [
        "ＣＬＯＳＥ＿ＰＯＳＩＴＩＯＮ",
        "cl\x00ose_position",
        "cl!<>ose_pos[]ition",
    ],
)
def test_natural_stop_refuses_suspicious_close_with_incomplete_other_owner(
    tmp_path,
    operation,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_other_suspicious_close_mutation(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="plan",
            operation=operation,
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


@pytest.mark.parametrize(
    "operation",
    [
        "ＣＬＯＳＥ＿ＰＯＳＩＴＩＯＮ",
        "cl\x00ose_position",
        "cl!<>ose_pos[]ition",
    ],
)
def test_natural_stop_apply_refuses_late_suspicious_incomplete_other_owner(
    tmp_path,
    operation,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory, _natural_stop_snapshot())
    with factory() as session:
        _add_other_suspicious_close_mutation(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="apply",
            operation=operation,
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


@pytest.mark.parametrize(
    "operation",
    [
        "ＣＬＯＳＥ＿ＰＯＳＩＴＩＯＮ",
        "cl\x00ose_position",
        "cl!<>ose_pos[]ition",
    ],
)
def test_natural_stop_resume_refuses_late_suspicious_incomplete_other_owner(
    tmp_path,
    operation,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        _add_other_suspicious_close_mutation(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="resume",
            operation=operation,
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


def test_natural_stop_complete_other_owner_allows_unrelated_plain_text(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_complete_other_close_event(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="plain",
            response_json="ordinary unrelated provider text",
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


@pytest.mark.parametrize(
    "response_json",
    [
        "ordinary risk management note",
        json.dumps({"note": "ordinary risk management note"}),
        r"ordinary literal \u sequence",
    ],
)
def test_natural_stop_complete_other_owner_allows_non_owner_management_text(
    tmp_path,
    response_json,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_complete_other_close_event(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix=sha256(response_json.encode()).hexdigest()[:10],
            response_json=response_json,
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    assert plan.status == "ready"


def test_natural_stop_refuses_malformed_exact_management_owner_key_text(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_complete_other_close_event(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="malformed-exact-key",
            response_json="management_batch_id: 119",
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_encoded_target_string_overrides_complete_other_event(
    tmp_path,
):
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    encoded_target = json.dumps(
        {"management_batch_id": 119, "management_leg_id": leg_id},
        sort_keys=True,
    )
    with factory() as session:
        _add_complete_other_close_event(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="encoded-target",
            response_json=json.dumps({"encoded": encoded_target}),
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_encoded_target_string_overrides_complete_other_mutation(
    tmp_path,
):
    factory, _, _, _, target_leg_id = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _add_complete_other_close_event(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix="mutation-encoded-target",
            response_json=None,
        )
        session.flush()
        owner_event = (
            session.query(ExecutionEvent)
            .filter_by(kol_id=None)
            .order_by(ExecutionEvent.id.desc())
            .first()
        )
        owner_refs = json.loads(owner_event.before_json)
        owner_leg = session.get(
            StrategyManagementLeg,
            int(owner_refs["management_leg_id"]),
        )
        owner_entry = session.get(
            ExecutionOrderLeg,
            int(owner_leg.execution_order_leg_id),
        )
        session.add(
            PositionMutationIntent(
                idempotency_key="complete-other-mutation-encoded-target",
                operation="close_position",
                strategy_instance_id=owner_event.strategy_instance_id,
                execution_binding_id=owner_event.execution_binding_id,
                execution_order_leg_id=owner_entry.id,
                pos_id=owner_event.pos_id,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json=owner_event.before_json,
                response_json="{}",
                error_json=json.dumps(
                    {
                        "encoded": json.dumps(
                            {
                                "management_batch_id": 119,
                                "management_leg_id": target_leg_id,
                            }
                        )
                    }
                ),
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def _add_double_encoded_payload_only_target_event(session, *, leg_id):
    target_owner = json.dumps(
        {"management_batch_id": 119, "management_leg_id": leg_id},
        sort_keys=True,
    )
    session.add(
        ExecutionEvent(
            execution_binding_id=None,
            strategy_instance_id=None,
            action="strategy_management_close_submit",
            status="submitted",
            pos_id=None,
            before_json=json.dumps(json.dumps(target_owner)),
            created_at=NOW,
        )
    )


def test_natural_stop_apply_cas_refuses_late_double_encoded_target_event(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory, _natural_stop_snapshot())
    with factory() as session:
        _add_double_encoded_payload_only_target_event(session, leg_id=leg_id)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )


def test_natural_stop_resume_refuses_late_double_encoded_target_event(tmp_path):
    module = _recovery_module()
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        _add_double_encoded_payload_only_target_event(session, leg_id=leg_id)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("encoded_kind", ["cumulative_bytes", "deep", "duplicate"])
def test_natural_stop_refuses_unsafe_encoded_management_string(
    tmp_path,
    encoded_kind,
):
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    if encoded_kind == "cumulative_bytes":
        encoded_value = json.dumps(
            {
                "management_batch_id": 119,
                "management_leg_id": leg_id,
                "padding": "x" * 8_500,
            }
        )
    elif encoded_kind == "deep":
        encoded_value = json.dumps(
            {"management_batch_id": 119, "management_leg_id": leg_id}
        )
        for _ in range(12):
            encoded_value = json.dumps(encoded_value)
    else:
        encoded_value = (
            '{"management_batch_id":119,"management_batch_id":120,'
            f'"management_leg_id":{leg_id}}}'
        )
    with factory() as session:
        _add_complete_other_close_event(
            session,
            batch119=session.get(StrategyManagementBatch, 119),
            suffix=encoded_kind,
            response_json=json.dumps({"encoded": encoded_value}),
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


@pytest.mark.parametrize("conflict_kind", ["target_position", "target_batch"])
def test_natural_stop_refuses_inconsistent_other_leg_event_owner_claim(
    tmp_path,
    conflict_kind,
):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch119 = session.get(StrategyManagementBatch, 119)
        other_entry = ExecutionOrderLeg(
            execution_binding_id=binding_id,
            strategy_instance_id=batch119.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id=f"conflicting-other-entry-{conflict_kind}",
            pos_id="conflicting-other-position",
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
        )
        session.add(other_entry)
        session.flush()
        other_batch = _add_other_terminal_batch(
            session,
            binding_id=binding_id,
            batch119=batch119,
        )
        other_batch.strategy_instance_id = batch119.strategy_instance_id
        other_leg = StrategyManagementLeg(
            management_batch_id=other_batch.id,
            execution_order_leg_id=other_entry.id,
            pos_id="conflicting-other-position",
            leg_index=0,
            status="succeeded",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(other_leg)
        session.flush()
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                strategy_instance_id=batch119.strategy_instance_id,
                action="strategy_management_close_submit",
                status="submitted",
                pos_id=(
                    POS_ID
                    if conflict_kind == "target_position"
                    else other_leg.pos_id
                ),
                before_json=json.dumps(
                    {
                        "management_batch_id": (
                            119
                            if conflict_kind == "target_batch"
                            else other_batch.id
                        ),
                        "management_leg_id": other_leg.id,
                    },
                    sort_keys=True,
                ),
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(
        plan, "durable_close_submission_evidence_present"
    )


def test_natural_stop_refuses_residual_unowned_regular_close(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    residual = {
        "ordId": "manual-residual-close",
        "posId": POS_ID,
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "side": "sell",
        "reduceOnly": True,
        "state": "filled",
        "uTime": str(int((NOW - timedelta(milliseconds=500)).timestamp() * 1000)),
    }

    plan = _plan(
        factory,
        _natural_stop_snapshot(order_history=[residual]),
    )

    _assert_natural_stop_refusal(
        plan, "exchange_snapshot_incomplete"
    )


def test_exact_snapshot_authority_binds_capture_window(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot()
    snapshot.capture_started_at = NOW - timedelta(seconds=1)

    plan = _plan(factory, snapshot)

    _assert_natural_stop_refusal(plan, "exchange_snapshot_incomplete")


def test_exact_snapshot_refuses_capture_window_after_private_wall_clock(
    tmp_path,
    monkeypatch,
):
    module = _recovery_module()
    monkeypatch.setattr(module, "_utc_wall_clock", lambda: NOW, raising=False)
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    future = datetime(2036, 1, 1, tzinfo=UTC)
    snapshot = _snapshot(
        capture_started_at=future,
        capture_ended_at=future,
    )

    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=future,
    )

    _assert_natural_stop_refusal(plan, "exchange_snapshot_incomplete")


@pytest.mark.parametrize("field_name", ["capture_started_at", "capture_ended_at"])
def test_exact_snapshot_refuses_naive_capture_window_time(tmp_path, field_name):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot()
    setattr(snapshot, field_name, NOW.replace(tzinfo=None))

    plan = _plan(factory, snapshot)

    _assert_natural_stop_refusal(plan, "exchange_snapshot_incomplete")


def _move_incident_time_boundary(session, value):
    batch = session.get(StrategyManagementBatch, 119)
    leg = session.query(StrategyManagementLeg).one()
    batch.planned_at = value
    batch.started_at = value
    leg.created_at = value


def test_natural_stop_planner_refuses_three_mutable_times_moved_before_raw_anchor(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        _move_incident_time_boundary(
            session,
            datetime(2001, 1, 1, tzinfo=UTC),
        )
        session.commit()

    plan = _plan(factory, _natural_stop_snapshot())

    _assert_natural_stop_refusal(plan, "natural_stop_time_authority_invalid")


def test_natural_stop_apply_cas_binds_durable_incident_time_boundary(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory, _natural_stop_snapshot())
    assert plan.status == "ready"
    with factory() as session:
        _move_incident_time_boundary(
            session,
            datetime(2001, 1, 1, tzinfo=UTC),
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="source_state_conflict",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_natural_stop_resume_rebuilds_same_incident_time_authority(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        _move_incident_time_boundary(
            session,
            datetime(2001, 1, 1, tzinfo=UTC),
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


@pytest.mark.parametrize("evidence_kind", ["mutation", "event"])
def test_natural_stop_resume_refuses_new_exact_durable_close_evidence(
    tmp_path,
    evidence_kind,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        if evidence_kind == "mutation":
            session.add(
                PositionMutationIntent(
                    idempotency_key="post-repair-exact-close",
                    operation="close_position",
                    strategy_instance_id="deepcoin:incident:btc:long",
                    execution_binding_id=binding_id,
                    execution_order_leg_id=entry_id,
                    pos_id=POS_ID,
                    authority_fingerprint="a" * 64,
                    request_fingerprint="r" * 64,
                    status="confirmed",
                    request_json="{}",
                    response_json="{}",
                    reserved_at=NOW,
                    submitted_at=NOW,
                    confirmed_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        else:
            session.add(
                ExecutionEvent(
                    execution_binding_id=binding_id,
                    strategy_instance_id="deepcoin:incident:btc:long",
                    action="strategy_management_close_submit",
                    status="confirmed",
                    pos_id=POS_ID,
                    created_at=NOW,
                )
            )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


def test_natural_stop_resume_without_new_close_evidence_is_repeatable(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )

    resumed = _authorize_recovery(module,
        factory,
        expected_fingerprint=plan.evidence_fingerprint,
        snapshot=snapshot,
    )

    assert resumed.repair_result.status == "already_repaired"


def test_natural_stop_serialization_contains_no_raw_time_authority(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory, _natural_stop_snapshot())

    serialized = json.dumps(
        module.serialize_composite_batch_recovery_plan(plan),
        sort_keys=True,
    )

    assert INCIDENT_STARTED.isoformat() not in serialized
    assert str(int(INCIDENT_STARTED.timestamp() * 1000)) not in serialized
    assert NOW.isoformat() not in serialized
    assert str(int(NOW.timestamp() * 1000)) not in serialized


def test_batch_119_false_submission_is_ready_for_repair(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(factory)

    assert plan.status == "ready"
    assert plan.reason_code == "false_legacy_submission_proven"
    assert plan.position.disposition == "resume_to_target"
    assert plan.position.close_delta == "19"
    assert plan.production_writes == 0
    assert plan.exchange_calls == 0
    assert len(plan.source_fingerprint) == 64
    assert len(plan.exchange_snapshot_fingerprint) == 64


def test_planner_refuses_verified_entry_without_authoritative_ownership_evidence(
    tmp_path,
):
    factory, _, _, entry_id, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        entry = session.get(ExecutionOrderLeg, entry_id)
        entry.attribution_evidence_json = '{"provider_error":"private"}'
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "exchange_snapshot_incomplete"
    assert len(plan.evidence_fingerprint) == 64


def test_planner_refuses_profile_outside_batch_119_allowlist(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    profile = module.CompositeBatchRecoveryProfile(
        batch_id=120,
        raw_message_id=10532,
        lifecycle_id=794,
        trusted_start_size="38",
        target_remaining_size="19",
        instrument_id="BTC-USDT-SWAP",
        side="long",
    )

    plan = module.build_composite_batch_recovery_plan(
        factory, profile=profile, snapshot=_snapshot(), planned_at=NOW
    )

    assert plan.status == "refused"
    assert plan.reason_code == "incident_profile_not_allowlisted"


def test_planner_refuses_malformed_profile_identity_without_raising(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    malformed = module.CompositeBatchRecoveryProfile(
        batch_id="not-an-integer",
        raw_message_id=10532,
        lifecycle_id=794,
        trusted_start_size="38",
        target_remaining_size="19",
        instrument_id="BTC-USDT-SWAP",
        side="long",
    )

    plan = module.build_composite_batch_recovery_plan(
        factory, profile=malformed, snapshot=_snapshot(), planned_at=NOW
    )

    assert plan.status == "refused"
    assert plan.batch_id == 0
    assert plan.reason_code == "incident_profile_not_allowlisted"


@pytest.mark.parametrize(
    ("field", "snapshot"),
    [
        ("positions", SimpleNamespace(**{
            key: value for key, value in vars(_snapshot()).items() if key != "positions"
        })),
        ("pending_trigger_orders", SimpleNamespace(**{
            key: value for key, value in vars(_snapshot()).items()
            if key != "pending_trigger_orders"
        })),
        ("trigger_history", SimpleNamespace(**{
            key: value for key, value in vars(_snapshot()).items()
            if key != "trigger_history"
        })),
        ("order_history", SimpleNamespace(**{
            key: value for key, value in vars(_snapshot()).items()
            if key != "order_history"
        })),
        ("trade_fills", SimpleNamespace(**{
            key: value for key, value in vars(_snapshot()).items()
            if key != "trade_fills"
        })),
        ("pending_tpsl", _snapshot(pending_tpsl_observations=[{
            "instrument_id": "BTC-USDT-SWAP", "complete": False
        }])),
    ],
)
def test_planner_refuses_each_incomplete_exchange_evidence_source(
    tmp_path, field, snapshot
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(factory, snapshot)

    assert plan.status == "refused", field
    assert plan.reason_code == "exchange_snapshot_incomplete"


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (StrategyManagementBatch, "raw_message_id", 1),
        (StrategyManagementBatch, "target_lifecycle_id", 1),
        (StrategyLifecycle, "side", "short"),
        (ExecutionBinding, "strategy_instance_id", "drifted-strategy"),
        (StrategyManagementLeg, "preflight_size", "37"),
    ],
)
def test_planner_refuses_durable_identity_mismatch(
    tmp_path, model, field, value
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        row = session.query(model).first()
        setattr(row, field, value)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert (
        "mismatch" in plan.reason_code
        or "not_allowlisted" in plan.reason_code
        or plan.reason_code == "exchange_snapshot_incomplete"
    )


@pytest.mark.parametrize("drift", ["fingerprint", "topology"])
def test_planner_refuses_contract_fingerprint_or_component_topology_mismatch(
    tmp_path, drift
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        if drift == "fingerprint":
            session.get(StrategyManagementBatch, 119).management_contract_fingerprint = (
                "0" * 64
            )
        else:
            component = (
                session.query(StrategyManagementComponent)
                .filter_by(management_batch_id=119, sequence=2)
                .one()
            )
            component.status = "confirmed"
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code in {
        "management_contract_fingerprint_mismatch",
        "component_topology_mismatch",
        "false_submission_state_mismatch",
    }


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (StrategyManagementBatch, "status", "executing"),
        (StrategyManagementLeg, "status", "planned"),
        (StrategyManagementComponent, "status", "confirmed"),
    ],
)
def test_planner_refuses_status_other_than_exact_false_state(
    tmp_path, model, field, value
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        row = session.query(model).order_by(model.id).first()
        setattr(row, field, value)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code in {
        "false_submission_state_mismatch",
        "component_topology_mismatch",
    }


@pytest.mark.parametrize(
    "field", ["request_json", "response_json", "client_order_id", "exchange_order_id"]
)
def test_planner_refuses_any_management_leg_submission_payload_or_identity(
    tmp_path, field
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        leg = session.query(StrategyManagementLeg).one()
        setattr(leg, field, '{"secret":"value"}' if field.endswith("json") else "id")
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "durable_close_submission_evidence_present"


def test_planner_refuses_matching_close_execution_event(tmp_path):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                action="strategy_management_close_submit",
                status="submitted",
                symbol="BTC",
                side="long",
                pos_id=POS_ID,
                request_json='{"credential":"secret"}',
                created_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "durable_close_submission_evidence_present"


def test_planner_refuses_matching_position_mutation_intent(tmp_path):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="incident-close",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="reserved",
                request_json='{"credential":"secret"}',
                reserved_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "durable_close_submission_evidence_present"


@pytest.mark.parametrize("snapshot_field", ["open_orders", "order_history", "trade_fills"])
def test_planner_refuses_matching_regular_close_order_or_fill(
    tmp_path, snapshot_field
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    close_row = {
        "ordId": "sensitive-close-order",
        "posId": POS_ID,
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "side": "sell",
        "reduceOnly": True,
        "state": "filled",
    }
    snapshot = _snapshot(**{snapshot_field: [close_row]})

    plan = _plan(factory, snapshot)

    assert plan.status == "refused"
    assert plan.reason_code == (
        "exchange_close_submission_evidence_present"
        if snapshot_field == "open_orders"
        else "exchange_snapshot_incomplete"
    )


def test_planner_refuses_additional_active_management_batch(tmp_path):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="other-active",
                raw_message_id=batch.raw_message_id,
                recognition_decision_id=batch.recognition_decision_id,
                recognition_generation="other",
                target_lifecycle_id=batch.target_lifecycle_id,
                strategy_instance_id="other-active-strategy",
                execution_binding_id=binding_id,
                intent="full_exit",
                effective_action="full_exit",
                execution_mode="live",
                status="executing",
                target_fingerprint="other-target",
                target_snapshot_json="{}",
                planned_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_planner_refuses_additional_active_component(tmp_path):
    factory, _, _, _, leg_id = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.add(
            StrategyManagementComponent(
                management_batch_id=119,
                strategy_management_leg_id=leg_id,
                strategy_management_leg_scope=leg_id,
                component_kind="cancel_deferred_entries",
                sequence=9,
                status="pending",
                idempotency_key="unexpected-component",
                desired_json="{}",
                evidence_json="[]",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "component_topology_mismatch"


def test_planner_refuses_additional_active_instruction(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        candidate = SignalCandidate(
            raw_message_id=10532,
            symbol="BTC",
            side="long",
            event_type="management",
            management_action="partial_then_break_even",
            review_status="confirmed",
            created_at=NOW,
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=10532,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                strategy_instance_id="deepcoin:incident:btc:long",
                idempotency_key="instruction-active",
                status="executing",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_planner_refuses_unexpected_protection_ownership(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    unexpected = {
        "ordId": "unowned-sensitive-order",
        "posId": POS_ID,
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "slTriggerPx": "61000",
        "state": "live",
    }
    snapshot = _snapshot(
        pending_trigger_orders=[*_snapshot().pending_trigger_orders, unexpected]
    )

    plan = _plan(factory, snapshot)

    assert plan.status == "refused"
    assert plan.reason_code == "unexpected_protection_ownership"


@pytest.mark.parametrize(
    ("field", "before_rows", "after_rows"),
    [
        (
            "positions",
            [{
                "posId": POS_ID, "instId": "BTC-USDT-SWAP",
                "posSide": "long", "pos": "38", "avgPx": "62000",
            }],
            [{
                "posId": POS_ID, "instId": "BTC-USDT-SWAP",
                "posSide": "long", "pos": "38", "avgPx": "62001",
            }],
        ),
        (
            "position_history",
            [{
                "posId": POS_ID, "instId": "BTC-USDT-SWAP",
                "posSide": "long", "state": "open", "closedSize": "0",
                "uTime": "1",
            }],
            [{
                "posId": POS_ID, "instId": "BTC-USDT-SWAP",
                "posSide": "long", "state": "open", "closedSize": "0",
                "uTime": "2",
            }],
        ),
        (
            "open_orders",
            [{"posId": POS_ID, "side": "buy", "ordId": "historical-a"}],
            [{"posId": POS_ID, "side": "buy", "ordId": "historical-b"}],
        ),
        (
            "pending_trigger_orders",
            [
                {**_snapshot().pending_trigger_orders[0], "uTime": "1"},
                _snapshot().pending_trigger_orders[1],
            ],
            [
                {**_snapshot().pending_trigger_orders[0], "uTime": "2"},
                _snapshot().pending_trigger_orders[1],
            ],
        ),
        (
            "order_history",
            [{"posId": POS_ID, "side": "buy", "ordId": "historical-a"}],
            [{"posId": POS_ID, "side": "buy", "ordId": "historical-b"}],
        ),
        (
            "trade_fills",
            [{"posId": POS_ID, "side": "buy", "fillPx": "62000"}],
            [{"posId": POS_ID, "side": "buy", "fillPx": "62001"}],
        ),
        (
            "trigger_history",
            [{
                "ordId": PRIMARY_ORDER_ID, "posId": POS_ID,
                "instId": "BTC-USDT-SWAP", "posSide": "long",
                "state": "cancelled", "triggerPx": "60000",
            }],
            [{
                "ordId": PRIMARY_ORDER_ID, "posId": POS_ID,
                "instId": "BTC-USDT-SWAP", "posSide": "long",
                "state": "cancelled", "triggerPx": "60001",
            }],
        ),
        (
            "pending_tpsl_observations",
            [{
                "instrument_id": "BTC-USDT-SWAP", "complete": True,
                "response_count": 2,
            }],
            [{
                "instrument_id": "BTC-USDT-SWAP", "complete": True,
                "response_count": 3,
            }],
        ),
    ],
)
def test_exchange_snapshot_fingerprint_changes_when_same_count_content_changes(
    tmp_path, field, before_rows, after_rows
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    before = _plan(factory, _snapshot(**{field: before_rows}))
    after = _plan(factory, _snapshot(**{field: after_rows}))

    if field in {"order_history", "trade_fills", "pending_tpsl_observations"}:
        assert before.status == after.status == "refused"
        assert before.reason_code == after.reason_code == (
            "exchange_snapshot_incomplete"
        )
        return
    assert before.status == after.status == "ready"
    assert before.exchange_snapshot_fingerprint != after.exchange_snapshot_fingerprint
    assert before.evidence_fingerprint != after.evidence_fingerprint


def test_exchange_snapshot_fingerprint_is_stable_across_collection_reordering(
    tmp_path
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    first = _snapshot(
        pending_trigger_orders=list(_snapshot().pending_trigger_orders),
        trigger_history=[
            {
                "ordId": PRIMARY_ORDER_ID, "posId": POS_ID,
                "instId": "BTC-USDT-SWAP", "posSide": "long",
                "state": "cancelled", "triggerPx": "60000",
            },
            {
                "ordId": BACKUP_ORDER_ID, "posId": POS_ID,
                "instId": "BTC-USDT-SWAP", "posSide": "long",
                "state": "cancelled", "triggerPx": "61000",
            },
        ],
    )
    reordered = _snapshot(
        pending_trigger_orders=list(reversed(_snapshot().pending_trigger_orders)),
        trigger_history=list(reversed(first.trigger_history)),
    )

    before = _plan(factory, first)
    after = _plan(factory, reordered)

    assert before.status == after.status == "ready"
    assert before.exchange_snapshot_fingerprint == after.exchange_snapshot_fingerprint
    assert before.evidence_fingerprint == after.evidence_fingerprint


@pytest.mark.parametrize("drift", ["target_fingerprint", "target_snapshot_json"])
def test_planner_refuses_target_snapshot_fingerprint_drift(tmp_path, drift):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        if drift == "target_fingerprint":
            batch.target_fingerprint = "0" * 64
        else:
            payload = json.loads(batch.target_snapshot_json)
            payload["positions"][0]["margin_mode"] = "isolated"
            batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "target_snapshot_fingerprint_mismatch"


def test_planner_refuses_any_incomplete_pending_tpsl_observation(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot(
        pending_tpsl_observations=[
            {"instrument_id": "BTC-USDT-SWAP", "complete": True},
            {"instrument_id": "ETH-USDT-SWAP", "complete": False},
        ]
    )

    plan = _plan(factory, snapshot)

    assert plan.status == "refused"
    assert plan.reason_code == "exchange_snapshot_incomplete"


def test_planner_refuses_active_component_even_beneath_terminal_batch(tmp_path):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch119 = session.get(StrategyManagementBatch, 119)
        terminal = StrategyManagementBatch(
            idempotency_fingerprint="terminal-with-active-component",
            raw_message_id=batch119.raw_message_id,
            recognition_decision_id=batch119.recognition_decision_id,
            recognition_generation="old-terminal",
            target_lifecycle_id=batch119.target_lifecycle_id,
            strategy_instance_id="old-terminal-strategy",
            execution_binding_id=binding_id,
            intent="full_exit",
            effective_action="full_exit",
            execution_mode="live",
            status="resolved",
            target_fingerprint="old-terminal-target",
            target_snapshot_json="{}",
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(terminal)
        session.flush()
        session.add(
            StrategyManagementComponent(
                management_batch_id=terminal.id,
                strategy_management_leg_id=None,
                strategy_management_leg_scope=-1,
                component_kind="cancel_deferred_entries",
                sequence=0,
                status="recovery_required",
                idempotency_key="terminal-active-component",
                desired_json="{}",
                evidence_json="[]",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_planner_refuses_unknown_outcome_position_mutation(tmp_path):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="unknown-unrelated-mutation",
                operation="cancel_tpsl",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                order_id=PRIMARY_ORDER_ID,
                authority_fingerprint="u" * 64,
                request_fingerprint="v" * 64,
                status="submit_unknown",
                request_json='{"sensitive":"payload"}',
                reserved_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


@pytest.mark.parametrize(
    ("field", "rows"),
    [
        ("positions", [object()]),
        ("open_orders", [{"not_json": object()}]),
        ("pending_trigger_orders", [object()]),
        ("order_history", [object()]),
        ("trade_fills", [object()]),
        ("trigger_history", [object()]),
    ],
)
def test_planner_refuses_malformed_snapshot_rows_without_raising(
    tmp_path, field, rows
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(factory, _snapshot(**{field: rows}))

    assert plan.status == "refused"
    assert plan.reason_code == "exchange_snapshot_incomplete"


def test_scope_rejects_same_count_protection_ledger_drift(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot()
    before = _plan(factory, snapshot)
    assert before.status == "ready"
    with factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .order_by(PositionProtectionLedger.id)
            .first()
        )
        row.evidence_json = '{"response":"different private response"}'
        session.commit()

    after = _plan(factory, snapshot)

    assert after.status == "refused"
    assert after.reason_code == "durable_snapshot_scope_mismatch"


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("38", {"batch_status": "ready", "leg_status": "planned"}),
        (
            "12",
            {
                "batch_status": "ready",
                "leg_status": "planned",
                "attestation_kind": "approved_under_target_recovery",
            },
        ),
        (
            None,
            {
                "batch_status": "resolved",
                "leg_status": "failed",
                "component_statuses": (
                    "safely_skipped", "safely_skipped", "safely_skipped"
                ),
            },
        ),
    ],
)
def test_proposed_transition_matches_position_disposition(tmp_path, current, expected):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    positions = [] if current is None else [{
        "posId": POS_ID,
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "pos": current,
    }]

    plan = _plan(
        factory,
        _natural_stop_snapshot() if current is None else _snapshot(positions=positions),
    )

    assert plan.status == "ready"
    transition = plan.evidence["proposed_transition"]
    for key, value in expected.items():
        assert transition[key] == value


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (RawMessage, "chat_id", 999),
        (ExecutionBinding, "chat_id", 999),
        (ExecutionBinding, "message_id", 999),
    ],
)
def test_planner_refuses_chat_or_message_identity_drift(
    tmp_path, model, field, value
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        row = session.query(model).first()
        setattr(row, field, value)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    # The dedicated capture cannot issue provenance for a scope whose durable
    # chat/message identity has drifted, so planning refuses the unissued
    # exchange envelope before consuming its rows.
    assert plan.reason_code == "exchange_snapshot_incomplete"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_lifecycle_id", 1),
        ("execution_binding_id", 999),
        ("strategy_instance_id", "drifted-strategy"),
        ("manageable_entry_leg_ids", [999]),
    ],
)
def test_planner_refuses_self_consistent_target_identity_drift(
    tmp_path, field, value
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        payload = json.loads(batch.target_snapshot_json)
        payload["identity"][field] = value
        batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
        batch.target_fingerprint = management_target_fingerprint(payload)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "target_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    "mutation",
    [
        "batch_execution_mode",
        "batch_action",
        "lifecycle_status",
        "binding_status",
        "entry_status",
        "leg_economics",
        "leg_snapshot",
        "component_attempt",
        "component_desired",
        "component_evidence",
    ],
)
def test_source_fingerprint_covers_every_verified_or_cas_dependency(
    tmp_path, mutation
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    before = _plan(factory)
    assert before.status == "ready"
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        lifecycle = session.get(StrategyLifecycle, 794)
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        leg = session.query(StrategyManagementLeg).one()
        entry = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
        component = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=119, sequence=0)
            .one()
        )
        if mutation == "batch_execution_mode":
            batch.execution_mode = "disabled"
        elif mutation == "batch_action":
            batch.effective_action = "partial_close"
        elif mutation == "lifecycle_status":
            lifecycle.lifecycle_status = "exited"
        elif mutation == "binding_status":
            binding.status = "closed"
        elif mutation == "entry_status":
            entry.status = "closed"
        elif mutation == "leg_economics":
            leg.avg_entry_price = "62001"
        elif mutation == "leg_snapshot":
            payload = json.loads(leg.last_exchange_snapshot_json)
            payload["position_rows"][0]["pos"] = "37"
            leg.last_exchange_snapshot_json = json.dumps(payload, sort_keys=True)
        elif mutation == "component_attempt":
            component.attempt_count += 1
        elif mutation == "component_desired":
            desired = json.loads(component.desired_json)
            desired["avg_entry_price"] = "62001"
            component.desired_json = json.dumps(desired, sort_keys=True)
        elif mutation == "component_evidence":
            component.evidence_json = '[{"error_type":"TimeoutError"}]'
        session.commit()

    after = _plan(factory)

    if after.status == "ready":
        assert after.source_fingerprint != before.source_fingerprint
    else:
        assert after.reason_code != "false_legacy_submission_proven"


def test_serialized_plan_is_strictly_redacted(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)

    serialized = json.dumps(module.serialize_composite_batch_recovery_plan(plan))

    for sensitive in (
        SOURCE_TEXT,
        POS_ID,
        PRIMARY_ORDER_ID,
        BACKUP_ORDER_ID,
        "sensitive-entry-order",
        "sensitive-entry-client-order",
        "private provider response",
        "secret-api-key",
        "credential",
        "request_json",
        "response_json",
        "provider_error",
    ):
        assert sensitive not in serialized


def _file_signature(path: Path):
    if not path.exists():
        return None
    return (path.stat().st_size, sha256(path.read_bytes()).hexdigest())


def test_planner_operates_against_readonly_database_without_file_changes(tmp_path):
    factory, database, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot()
    factory.kw["bind"].dispose()
    paths = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]
    before = [_file_signature(path) for path in paths]
    readonly_engine = create_engine(
        f"sqlite+pysqlite:///file:{database.resolve()}?mode=ro&immutable=1&uri=true"
    )
    readonly_factory = sessionmaker(
        bind=readonly_engine, autoflush=False, expire_on_commit=False
    )

    plan = _plan(readonly_factory, snapshot)

    readonly_engine.dispose()
    assert plan.status == "ready"
    assert [_file_signature(path) for path in paths] == before


@pytest.mark.parametrize(
    ("field", "rows"),
    [
        (
            "position_history",
            [
                {
                    "posId": POS_ID,
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "state": "closed",
                    "closeSz": "19",
                }
            ],
        ),
        (
            "trigger_history",
            [
                {
                    "ordId": PRIMARY_ORDER_ID,
                    "posId": POS_ID,
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "side": "sell",
                    "reduceOnly": True,
                    "state": "triggered",
                }
            ],
        ),
        (
            "open_orders",
            [
                {
                    "closePosId": POS_ID,
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "side": "sell",
                    "state": "filled",
                }
            ],
        ),
    ],
)
def test_planner_refuses_all_exact_exchange_close_history_shapes(
    tmp_path, field, rows
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(factory, _snapshot(**{field: rows}))

    assert plan.status == "refused"
    assert plan.reason_code == "exchange_close_submission_evidence_present"


def test_live_position_requires_primary_and_backup_verified_stops(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot(pending_trigger_orders=[])
    with factory() as session:
        session.query(PositionProtectionLedger).delete()
        session.commit()

    plan = _plan(factory, snapshot)

    assert plan.status == "refused"
    assert plan.reason_code == "durable_snapshot_scope_mismatch"


def test_live_position_rejects_take_profit_without_primary_and_backup_stops(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    pending = [{
        **_snapshot().pending_trigger_orders[0],
        "tpTriggerPx": "65000",
        "slTriggerPx": "",
    }]
    snapshot = _snapshot(pending_trigger_orders=pending)
    with factory() as session:
        rows = session.query(PositionProtectionLedger).order_by(
            PositionProtectionLedger.id
        ).all()
        session.delete(rows[1])
        rows[0].purpose = "take_profit"
        rows[0].trigger_price = "65000"
        session.commit()
    plan = _plan(factory, snapshot)

    assert plan.status == "refused"
    assert plan.reason_code == "durable_snapshot_scope_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("venue", "other"),
        ("strategy_instance_id", "drifted-strategy"),
        ("purpose", "backup_stop_loss"),
        ("status", "active"),
        ("size_text", "37"),
    ],
)
def test_live_position_rejects_protection_ledger_identity_drift(
    tmp_path, field, value
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot()
    with factory() as session:
        row = session.query(PositionProtectionLedger).order_by(
            PositionProtectionLedger.id
        ).first()
        setattr(row, field, value)
        session.commit()

    plan = _plan(factory, snapshot)

    assert plan.status == "refused"
    assert plan.reason_code == "durable_snapshot_scope_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "cancelled"),
        ("triggerOrderType", "conditional"),
        ("slTriggerPx", "63999"),
        ("sz", "37"),
    ],
)
def test_live_position_rejects_pending_protection_exchange_drift(
    tmp_path, field, value
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    pending = [dict(row) for row in _snapshot().pending_trigger_orders]
    pending[0][field] = value

    plan = _plan(factory, _snapshot(pending_trigger_orders=pending))

    assert plan.status == "refused"
    assert plan.reason_code == "unexpected_protection_ownership"


def test_position_absent_allows_exact_natural_stop_with_no_pending_orders(
    tmp_path
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    plan = _plan(
        factory,
        _natural_stop_snapshot(),
    )

    assert plan.status == "ready"
    assert plan.position.disposition == "position_absent"


def _add_other_terminal_batch(session, *, binding_id, batch119):
    row = StrategyManagementBatch(
        idempotency_fingerprint="other-terminal-active-child",
        raw_message_id=batch119.raw_message_id,
        recognition_decision_id=batch119.recognition_decision_id,
        recognition_generation="old-terminal",
        target_lifecycle_id=batch119.target_lifecycle_id,
        strategy_instance_id="other-terminal-strategy",
        execution_binding_id=binding_id,
        intent="full_exit",
        effective_action="full_exit",
        execution_mode="live",
        status="resolved",
        target_fingerprint="other-terminal-target",
        target_snapshot_json="{}",
        planned_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def test_planner_refuses_definitely_rejected_component_as_active_work(tmp_path):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch119 = session.get(StrategyManagementBatch, 119)
        other = _add_other_terminal_batch(
            session, binding_id=binding_id, batch119=batch119
        )
        session.add(
            StrategyManagementComponent(
                management_batch_id=other.id,
                strategy_management_leg_id=None,
                strategy_management_leg_scope=-1,
                component_kind="cancel_deferred_entries",
                sequence=0,
                status="definitely_rejected",
                idempotency_key="other-definitely-rejected",
                desired_json="{}",
                evidence_json="[]",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_planner_refuses_submitted_instruction_as_active_work(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        candidate = SignalCandidate(
            raw_message_id=10532,
            symbol="BTC",
            side="long",
            event_type="management",
            management_action="partial_then_break_even",
            review_status="confirmed",
            created_at=NOW,
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=10532,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                strategy_instance_id="deepcoin:incident:btc:long",
                idempotency_key="submitted-instruction",
                status="submitted",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "additional_active_work_present"


def test_planner_refuses_nonproduction_component_idempotency_key(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        component = session.query(StrategyManagementComponent).order_by(
            StrategyManagementComponent.id
        ).first()
        component.idempotency_key = "not-production-derived"
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "component_topology_mismatch"


def test_planner_refuses_extra_desired_json_key(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        component = session.query(StrategyManagementComponent).order_by(
            StrategyManagementComponent.id
        ).first()
        desired = json.loads(component.desired_json)
        desired["extra_key"] = "not-allowed"
        component.desired_json = json.dumps(desired, sort_keys=True)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "component_topology_mismatch"


def test_source_fingerprint_covers_binding_modes_when_target_remains_coherent(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    before = _plan(factory)
    assert before.status == "ready"
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.margin_mode = "isolated"
        payload = json.loads(batch.target_snapshot_json)
        payload["positions"][0]["margin_mode"] = "isolated"
        batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
        batch.target_fingerprint = management_target_fingerprint(payload)
        session.commit()

    after = _plan(factory)

    assert after.status == "ready"
    assert after.source_fingerprint != before.source_fingerprint


def test_planner_refuses_target_position_execution_leg_drift(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        payload = json.loads(batch.target_snapshot_json)
        payload["positions"][0]["execution_order_leg_id"] = 999
        batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
        batch.target_fingerprint = management_target_fingerprint(payload)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "target_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deferred_entry_leg_ids", [999]),
        ("capability_deferred_entry_leg_ids", [999]),
    ],
)
def test_planner_refuses_target_identity_deferred_topology_drift(
    tmp_path, field, value
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        payload = json.loads(batch.target_snapshot_json)
        payload["identity"][field] = value
        batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
        batch.target_fingerprint = management_target_fingerprint(payload)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "target_snapshot_identity_mismatch"


def test_planner_refuses_binding_and_target_position_mode_mismatch(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        payload = json.loads(batch.target_snapshot_json)
        payload["positions"][0]["position_mode"] = "one_way"
        batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
        batch.target_fingerprint = management_target_fingerprint(payload)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "target_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    ("sequence", "evidence"),
    [
        (1, [{"client_order_id": "sensitive-close-client"}]),
        (0, [{"close_intent_ids": [88]}]),
        (0, [{"error_type": "RuntimeError"}, {"error_type": "TimeoutError"}]),
    ],
)
def test_planner_refuses_component_submission_or_extra_evidence(
    tmp_path, sequence, evidence
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        component = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=119, sequence=sequence)
            .one()
        )
        component.evidence_json = json.dumps(evidence, sort_keys=True)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code in {
        "component_topology_mismatch",
        "durable_close_submission_evidence_present",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "component_evidence",
        "ledger_evidence",
        "leg_last_error",
        "target_identity",
    ],
)
def test_planner_refuses_malformed_durable_evidence_without_raising(
    tmp_path, mutation
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        if mutation == "component_evidence":
            session.query(StrategyManagementComponent).first().evidence_json = "{"
        elif mutation == "ledger_evidence":
            session.query(PositionProtectionLedger).first().evidence_json = "{"
        elif mutation == "leg_last_error":
            session.query(StrategyManagementLeg).one().last_error = "{"
        else:
            payload = json.loads(batch.target_snapshot_json)
            payload["identity"]["execution_binding_id"] = "not-an-integer"
            batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
            batch.target_fingerprint = management_target_fingerprint(payload)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code in {
        "durable_evidence_invalid",
        "durable_snapshot_scope_mismatch",
        "target_snapshot_identity_mismatch",
        "component_topology_mismatch",
        "exchange_snapshot_incomplete",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "matching_order",
        "position_reduced",
        "wrong_position",
        "top_level_provider_payload",
        "row_provider_payload",
    ],
)
def test_planner_refuses_nonexact_legacy_exchange_snapshot(tmp_path, mutation):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        leg = session.query(StrategyManagementLeg).one()
        payload = json.loads(leg.last_exchange_snapshot_json)
        if mutation == "matching_order":
            payload["matching_regular_orders"] = [{"ordId": "close-order"}]
        elif mutation == "position_reduced":
            payload["position_rows"][0]["pos"] = "19"
        elif mutation == "wrong_position":
            payload["position_rows"][0]["posId"] = "other-position"
        elif mutation == "top_level_provider_payload":
            payload["response"] = {"code": "0"}
        else:
            payload["position_rows"][0]["provider_response"] = {"code": "0"}
        leg.last_exchange_snapshot_json = json.dumps(payload, sort_keys=True)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "false_submission_state_mismatch"


def test_planner_allows_exact_legacy_not_found_error(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.query(StrategyManagementLeg).one().last_error = json.dumps(
            {"reason": "management_close_order_not_found"}, sort_keys=True
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "ready"


@pytest.mark.parametrize(
    "last_error",
    [
        {"reason": "management_close_order_identity_conflict"},
        {"reason": "management_close_order_not_found", "provider_error": "x"},
        [],
    ],
)
def test_planner_refuses_nonexact_legacy_last_error(tmp_path, last_error):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        session.query(StrategyManagementLeg).one().last_error = json.dumps(
            last_error, sort_keys=True
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "false_submission_state_mismatch"


@pytest.mark.parametrize(
    ("field", "value", "top_level"),
    [
        ("capability_deferred_pos_ids", ["deferred-pos"], False),
        ("deferred_entry_legs", [{"execution_order_leg_id": 999}], True),
    ],
)
def test_planner_refuses_remaining_target_deferred_topology(
    tmp_path, field, value, top_level
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        payload = json.loads(batch.target_snapshot_json)
        target = payload if top_level else payload["identity"]
        target[field] = value
        batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
        batch.target_fingerprint = management_target_fingerprint(payload)
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code == "target_snapshot_identity_mismatch"


@pytest.mark.parametrize(
    ("purpose", "price"),
    [("stop_loss", "62000"), ("backup_stop", "61000")],
)
def test_live_position_rejects_duplicate_required_stop_role(
    tmp_path, purpose, price
):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    duplicate_order_id = f"duplicate-{purpose}"
    pending = [
        *_snapshot().pending_trigger_orders,
        {
            "ordId": duplicate_order_id,
            "posId": POS_ID,
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "triggerOrderType": "TPSL",
            "slTriggerPx": price,
            "sz": "38",
            "state": "live",
        },
    ]
    snapshot = _snapshot(pending_trigger_orders=pending)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id=batch.strategy_instance_id,
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=duplicate_order_id,
                purpose=purpose,
                trigger_price=price,
                size_text="38",
                status="verified",
                evidence_source="entry_protection_response",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    plan = _plan(factory, snapshot)

    assert plan.status == "refused"
    assert plan.reason_code == "durable_snapshot_scope_mismatch"


def test_live_position_allows_verified_additional_take_profit(tmp_path):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    take_profit_id = "verified-additional-take-profit"
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id=batch.strategy_instance_id,
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=take_profit_id,
                purpose="take_profit",
                trigger_price="65000",
                size_text="38",
                status="verified",
                evidence_source="entry_protection_response",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    pending = [
        *_snapshot().pending_trigger_orders,
        {
            "ordId": take_profit_id,
            "posId": POS_ID,
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "triggerOrderType": "TPSL",
            "tpTriggerPx": "65000",
            "sz": "38",
            "state": "live",
        },
    ]

    plan = _plan(factory, _snapshot(pending_trigger_orders=pending))

    assert plan.status == "ready"


def test_live_position_allows_matching_whole_position_zero_size_semantics(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        for row in session.query(PositionProtectionLedger).all():
            row.size_text = "0"
        session.commit()
    pending = [
        {**row, "sz": "0"} for row in _snapshot().pending_trigger_orders
    ]

    plan = _plan(
        factory,
        _snapshot(
            pending_trigger_orders=pending,
            scope_protection_overrides={
                "backup_stop": {"size_text": "0"},
                "stop_loss": {"size_text": "0"},
            },
        ),
    )

    assert plan.status == "ready"


def test_planner_refuses_cancelled_trigger_history_with_exact_close_position_id(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot(
        trigger_history=[
            {
                "ordId": PRIMARY_ORDER_ID,
                "closePosId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "state": "cancelled",
            }
        ]
    )

    plan = _plan(factory, snapshot)

    assert plan.status == "refused"
    assert plan.reason_code == "exchange_close_submission_evidence_present"


def test_planner_allows_cancelled_nonclose_protection_trigger_history(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = _snapshot(
        trigger_history=[
            {
                "ordId": PRIMARY_ORDER_ID,
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "state": "cancelled",
                "slTriggerPx": "64000",
            }
        ]
    )

    plan = _plan(factory, snapshot)

    assert plan.status == "ready"


@pytest.mark.parametrize(
    "mutation",
    [
        "target_identity_oversized_integer",
        "ledger_json_oversized_integer",
        "ledger_json_deeply_nested",
    ],
)
def test_planner_refuses_oversized_durable_integer_without_raising(
    tmp_path, mutation
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    oversized_digits = "9" * 5000
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        if mutation == "target_identity_oversized_integer":
            payload = json.loads(batch.target_snapshot_json)
            payload["identity"]["execution_binding_id"] = oversized_digits
            batch.target_snapshot_json = json.dumps(payload, sort_keys=True)
            batch.target_fingerprint = management_target_fingerprint(payload)
        elif mutation == "ledger_json_oversized_integer":
            session.query(PositionProtectionLedger).first().evidence_json = (
                '{"sequence":' + oversized_digits + "}"
            )
        else:
            session.query(PositionProtectionLedger).first().evidence_json = (
                "[" * 994 + "0" + "]" * 994
            )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code in {
        "durable_evidence_invalid",
        "durable_snapshot_scope_mismatch",
        "target_snapshot_identity_mismatch",
        "exchange_snapshot_incomplete",
    }


def test_planner_refuses_deep_target_json_without_raising(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    deeply_nested_json = "[" * 994 + "0" + "]" * 994
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        batch.target_snapshot_json = (
            batch.target_snapshot_json[:-1]
            + ',"deeply_nested":'
            + deeply_nested_json
            + "}"
        )
        session.commit()

    plan = _plan(factory)

    assert plan.status == "refused"
    assert plan.reason_code in {
        "durable_evidence_invalid",
        "target_snapshot_invalid",
        "target_snapshot_fingerprint_mismatch",
    }


def test_planner_refuses_deep_exchange_snapshot_row_without_raising(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    deeply_nested = 0
    for _ in range(994):
        deeply_nested = [deeply_nested]
    position = dict(_snapshot().positions[0])
    position["deeply_nested"] = deeply_nested

    plan = _plan(factory, _snapshot(positions=[position]))

    assert plan.status == "refused"
    assert plan.reason_code == "exchange_snapshot_incomplete"


def test_source_fingerprint_covers_linked_lifecycle_and_binding_message_identity(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    before = _plan(factory)
    assert before.status == "ready"
    with factory() as session:
        lifecycle = session.get(StrategyLifecycle, 794)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        lifecycle.message_id = 9000
        binding.message_id = 9000
        session.commit()

    after = _plan(factory)

    assert after.status == "ready"
    assert after.source_fingerprint != before.source_fingerprint


def test_apply_repairs_only_false_legacy_state_in_one_transaction(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)

    result = _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )

    assert result.status == "repaired"
    assert result.batch_id == 119
    assert result.evidence_fingerprint == plan.evidence_fingerprint
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        leg = session.query(StrategyManagementLeg).one()
        components = (
            session.query(StrategyManagementComponent)
            .order_by(StrategyManagementComponent.sequence)
            .all()
        )
        events = session.query(ExecutionEvent).all()
        assert batch.status == "ready"
        assert leg.status == "planned"
        assert [row.status for row in components] == [
            "recovery_required", "pending", "pending"
        ]
        assert leg.request_json is None
        assert leg.response_json is None
        assert leg.client_order_id is None
        assert leg.exchange_order_id is None
        assert len(events) == 1
        event = events[0]
        assert event.action == "composite_batch_false_state_repaired"
        assert event.notification_fingerprint == plan.evidence_fingerprint
        assert event.request_json is None
        assert event.response_json is None
        assert event.order_id is None
        assert event.client_order_id is None
        assert event.pos_id is None
        before = json.loads(event.before_json)
        after = json.loads(event.after_json)
        assert before == {
            "batch_id": 119,
            "batch_status": "reconciling",
            "component_attempt_counts": [1, 0, 0],
            "component_statuses": ["recovery_required", "pending", "pending"],
            "leg_status": "submitted",
        }
        assert after["batch_status"] == "ready"
        assert after["leg_status"] == "planned"
        assert after["component_statuses"] == [
            "recovery_required", "pending", "pending"
        ]
        assert after["source_fingerprint"] == plan.source_fingerprint
        assert after["exchange_snapshot_fingerprint"] == (
            plan.exchange_snapshot_fingerprint
        )
        assert after["evidence_fingerprint"] == plan.evidence_fingerprint
        assert before["component_attempt_counts"] == [1, 0, 0]
        assert after["component_attempt_counts"] == [1, 0, 0]
        assert after["original_owned_stop_refs"] == sorted(
            sha256(f"protection_order:{order_id}".encode("utf-8")).hexdigest()
            for order_id in (PRIMARY_ORDER_ID, BACKUP_ORDER_ID)
        )
        assert all(
            len(value) == 64
            for value in after["original_owned_stop_refs"]
        )
        serialized_event = json.dumps(
            {"before": before, "after": after}, sort_keys=True
        )
        for sensitive in (
            POS_ID,
            PRIMARY_ORDER_ID,
            BACKUP_ORDER_ID,
            SOURCE_TEXT,
            "secret-api-key",
            "private provider response",
        ):
            assert sensitive not in serialized_event


def test_recovery_status_summary_counts_only_component_owned_mutations(tmp_path):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory)
    repair = _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="unrelated-terminal-intent",
                venue="deepcoin",
                operation="cancel_position_sltp",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                order_id="unrelated-order",
                authority_fingerprint="a" * 64,
                request_fingerprint="b" * 64,
                status="confirmed",
                request_json="{}",
                response_json="{}",
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    summary = module.build_composite_batch_recovery_status_summary(
        factory,
        plan=plan,
        repair_result=repair,
        executor_calls=0,
    )

    assert summary["component_mutation_intent_count"] == 0
    assert summary["confirmed_close_intent_count"] == 0
    assert summary["unresolved_mutation_intent_count"] == 0
    assert "position_mutation_intent_count" not in summary


@pytest.mark.parametrize(
    "tamper",
    [
        "source_fingerprint_field",
        "exchange_fingerprint_field",
        "position_field",
        "immutable_target",
        "proposed_transition",
        "plan_reason",
        "plan_counters",
    ],
)
def test_apply_rejects_internally_forged_plan_without_writes(tmp_path, tamper):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    evidence = module.serialize_composite_batch_recovery_plan(plan)["evidence"]
    if tamper == "source_fingerprint_field":
        forged = replace(plan, source_fingerprint="0" * 64)
    elif tamper == "exchange_fingerprint_field":
        forged = replace(plan, exchange_snapshot_fingerprint="1" * 64)
    elif tamper == "position_field":
        forged = replace(
            plan,
            position=replace(plan.position, current_size="37", close_delta="18"),
        )
    elif tamper == "plan_reason":
        forged = replace(plan, reason_code="forged_reason")
    elif tamper == "plan_counters":
        forged = replace(plan, production_writes=1, exchange_calls=1)
    else:
        if tamper == "immutable_target":
            evidence["immutable_target"]["target_remaining_size"] = "18"
        else:
            evidence["proposed_transition"]["batch_status"] = "succeeded"
        forged = replace(plan, evidence=evidence)

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=forged,
            expected_fingerprint=forged.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(StrategyManagementLeg).one().status == "submitted"
        assert session.query(ExecutionEvent).count() == 0


@pytest.mark.parametrize(
    ("change", "reason_code"),
    [
        ("authorization", "authorization_invalid"),
        ("expected_fingerprint", "evidence_fingerprint_mismatch"),
        ("batch_id", "plan_not_actionable"),
        ("status", "plan_not_actionable"),
    ],
)
def test_apply_rejects_invalid_authority_or_plan_envelope(
    tmp_path, change, reason_code
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    authorization = "I_AUTHORIZE_BATCH_119_TO_REMAINING_19"
    expected = plan.evidence_fingerprint
    if change == "authorization":
        authorization = "not-authorized"
    elif change == "expected_fingerprint":
        expected = "0" * 64
    elif change == "batch_id":
        plan = replace(plan, batch_id=120)
    else:
        plan = replace(plan, status="refused")

    with pytest.raises(
        module.CompositeBatchRecoveryConflict, match=reason_code
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=expected,
            authorization=authorization,
            applied_at=NOW,
        )


@pytest.mark.parametrize(
    "forged_fingerprint",
    ["source", "scope", "evidence"],
)
def test_apply_rejects_forged_fingerprint_before_opening_db(
    tmp_path,
    forged_fingerprint,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    expected = plan.evidence_fingerprint
    if forged_fingerprint == "source":
        plan = replace(plan, source_fingerprint="0" * 64)
    elif forged_fingerprint == "scope":
        plan = replace(plan, exchange_snapshot_fingerprint="1" * 64)
    else:
        expected = "2" * 64
    db_calls = []

    def forbidden_factory():
        db_calls.append(True)
        raise AssertionError("stale fingerprint opened DB")

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            forbidden_factory,
            plan=plan,
            expected_fingerprint=expected,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    assert db_calls == []

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


@pytest.mark.parametrize(
    ("mutation", "mutate"),
    [
        ("batch_status", lambda session: setattr(
            session.get(StrategyManagementBatch, 119), "status", "ready"
        )),
        ("leg_status", lambda session: setattr(
            session.query(StrategyManagementLeg).one(), "status", "planned"
        )),
        ("component_status", lambda session: setattr(
            session.query(StrategyManagementComponent).order_by(
                StrategyManagementComponent.sequence
            ).first(), "status", "confirmed"
        )),
        ("request_json", lambda session: setattr(
            session.query(StrategyManagementLeg).one(), "request_json", "{}"
        )),
        ("exchange_order_id", lambda session: setattr(
            session.query(StrategyManagementLeg).one(),
            "exchange_order_id", "drifted-order"
        )),
    ],
)
def test_apply_rejects_durable_state_drift_and_rolls_back(
    tmp_path, mutation, mutate
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    with factory() as session:
        mutate(session)
        session.commit()
    writer_calls = []

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=plan,
            prepare_writer=lambda: writer_calls.append(True)
            or SimpleNamespace(
                uid_scope_hash=(
                    _PLAN_SNAPSHOTS[id(plan)].account_authority.uid_scope_hash
                )
            ),
            expected_uid_scope_hash=(
                _PLAN_SNAPSHOTS[id(plan)].account_authority.uid_scope_hash
            ),
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    assert writer_calls == []
    with factory() as session:
        assert session.query(ExecutionEvent).count() == 0
        if mutation == "batch_status":
            assert session.get(StrategyManagementBatch, 119).status == "ready"
        else:
            assert session.get(StrategyManagementBatch, 119).status == "reconciling"


def test_apply_rejects_new_close_intent_after_plan_and_writes_nothing(tmp_path):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="new-close-intent-after-plan",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="reserved",
                request_json='{"credential":"secret"}',
                reserved_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_apply_acquires_immediate_lock_before_first_read(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    statements = []

    from sqlalchemy import event

    engine = factory.kw["bind"]

    def record_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    first_read = next(
        index for index, statement in enumerate(statements)
        if statement.startswith("SELECT")
    )
    begin_immediate = statements.index("BEGIN IMMEDIATE")
    assert begin_immediate < first_read


def test_concurrent_apply_creates_one_audit_and_returns_already_repaired(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def apply_once():
        try:
            barrier.wait()
            results.append(
                _apply_recovery(module,
                    factory,
                    plan=plan,
                    expected_fingerprint=plan.evidence_fingerprint,
                    authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
                    applied_at=NOW,
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=apply_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(row.status for row in results) == [
        "already_repaired", "repaired"
    ]
    assert len({row.audit_event_id for row in results}) == 1
    with factory() as session:
        assert (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.notification_fingerprint
                == plan.evidence_fingerprint
            )
            .count()
            == 1
        )
        assert session.query(ExecutionEvent).count() == 1


def test_resume_authorization_accepts_only_audited_progressed_batch_state(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=119)
            .order_by(StrategyManagementComponent.sequence)
            .all()
        )
        batch.status = "executing"
        components[0].status = "confirmed"
        components[0].reason_code = None
        components[0].attempt_count = 2
        components[0].completed_at = NOW
        components[0].evidence_json = json.dumps(
            [
                {"error_type": "RuntimeError"},
                {
                    "phase": "no_cancel_required",
                    "evidence_tier": "exact_terminal_no_fill",
                },
                {"proven_filled_quantity": "0"},
            ],
            sort_keys=True,
        )
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="terminal-first-tp",
                purpose="take_profit",
                trigger_price="66000",
                size_text="19",
                status="cancelled",
                evidence_source="test_terminal_readback",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        components[1].status = "awaiting_exchange"
        components[1].attempt_count = 1
        close_request = {
            "clOrdId": "CM119L1A1",
            "closePosId": POS_ID,
            "instId": "BTC-USDT-SWAP",
            "mrgPosition": "split",
            "ordType": "market",
            "posSide": "long",
            "side": "sell",
            "sz": "19",
            "tdMode": "cross",
        }
        close_desired = json.loads(components[1].desired_json)
        close_desired["partial_close_execution"] = {
            "client_order_id": "CM119L1A1",
            "close_delta": "19",
            "intent_id": 1,
            "pre_submit_size": "38",
        }
        components[1].desired_json = json.dumps(close_desired, sort_keys=True)
        session.add(
            PositionMutationIntent(
                id=1,
                idempotency_key=f"{components[1].id}:close:attempt:1",
                venue="deepcoin",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                authority_fingerprint="a" * 64,
                request_fingerprint=sha256(
                    json.dumps(
                        close_request,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                status="submitted",
                request_json=json.dumps(close_request, sort_keys=True),
                response_json='{"data":{"ordId":"close-1"}}',
                reserved_at=NOW,
                submitted_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_component_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=_snapshot(
                positions=[
                    {
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "19",
                    }
                ],
                trigger_history=[
                    {
                        "ordId": "terminal-first-tp",
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "triggerOrderType": "TPSL",
                        "tpTriggerPx": "66000",
                        "sz": "19",
                        "state": "cancelled",
                    }
                ],
            ),
        )


@pytest.mark.parametrize(
    ("sequence", "execution_key", "forged_execution"),
    [
        (
            0,
            "take_profit_consumption_execution",
            {
                "cancel_intent_ids": [],
                "cancel_order_ids": ["foreign-order"],
                "evidence_tier": "owned_pending",
            },
        ),
        (
            1,
            "partial_close_execution",
            {
                "client_order_id": "forged-close",
                "close_delta": "19",
                "intent_id": 999999,
                "pre_submit_size": "38",
            },
        ),
        (
            2,
            "protection_replacement_execution",
            {
                "backup_stop": "63000",
                "effective_remaining_size": "19",
                "old_stop_order_ids": ["foreign-order"],
                "primary_stop": "64000",
                "retained_take_profit_total": "0",
            },
        ),
    ],
)
def test_resume_authorization_rejects_forged_component_execution_plan(
    tmp_path,
    sequence,
    execution_key,
    forged_execution,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        session.get(StrategyManagementBatch, 119).status = "executing"
        component = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=119, sequence=sequence)
            .one()
        )
        desired = json.loads(component.desired_json)
        desired[execution_key] = forged_execution
        component.desired_json = json.dumps(desired, sort_keys=True)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_component_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=_snapshot(
                positions=[
                    {
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "38",
                    }
                ]
            ),
        )


@pytest.mark.parametrize("sequence", [0, 2])
def test_resume_authorization_rejects_forged_confirmed_component(
    tmp_path,
    sequence,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        component = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=119, sequence=sequence)
            .one()
        )
        component.status = "confirmed"
        component.completed_at = NOW
        component.evidence_json = '[{"forged":true}]'
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_component_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=_snapshot(
                positions=[
                    {
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "38",
                    }
                ]
            ),
        )


def test_resume_authorization_rejects_no_cancel_tp_while_still_pending(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        session.get(StrategyManagementBatch, 119).status = "executing"
        component = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=119, sequence=0)
            .one()
        )
        component.status = "confirmed"
        component.attempt_count = 1
        component.completed_at = NOW
        component.evidence_json = json.dumps(
            [
                {"error_type": "RuntimeError"},
                {
                    "phase": "no_cancel_required",
                    "evidence_tier": "exact_terminal_no_fill",
                },
                {"proven_filled_quantity": "0"},
            ],
            sort_keys=True,
        )
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="still-pending-first-tp",
                purpose="take_profit",
                trigger_price="66000",
                size_text="19",
                status="verified",
                evidence_source="test_pending_readback",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_component_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=_snapshot(
                pending_trigger_orders=[
                    *_snapshot().pending_trigger_orders,
                    {
                        "ordId": "still-pending-first-tp",
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "triggerOrderType": "TPSL",
                        "tpTriggerPx": "66000",
                        "sz": "19",
                        "state": "live",
                    },
                ]
            ),
        )


def test_resume_authorization_rejects_recovery_audit_binding_drift(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        event = session.query(ExecutionEvent).filter_by(
            action="composite_batch_false_state_repaired"
        ).one()
        event.execution_binding_id = int(event.execution_binding_id) + 999
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_audit_invalid",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=_snapshot(
                positions=[
                    {
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "38",
                    }
                ]
            ),
        )


def test_resume_authorization_rejects_shrunk_original_stop_set(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        components = (
            session.query(StrategyManagementComponent)
            .filter_by(management_batch_id=119)
            .order_by(StrategyManagementComponent.sequence)
            .all()
        )
        batch.status = "executing"
        components[0].status = "confirmed"
        components[0].attempt_count = 1
        components[0].completed_at = NOW
        components[0].evidence_json = json.dumps(
            [
                {"error_type": "RuntimeError"},
                {
                    "phase": "no_cancel_required",
                    "evidence_tier": "exact_terminal_no_fill",
                },
                {"proven_filled_quantity": "0"},
            ],
            sort_keys=True,
        )
        components[1].status = "confirmed"
        components[1].attempt_count = 1
        components[1].completed_at = NOW
        components[1].evidence_json = json.dumps(
            [
                {"close_delta": "0"},
                {
                    "evidence_tier": "exact_position_target",
                    "remaining_size": "19",
                },
            ],
            sort_keys=True,
        )
        components[2].status = "awaiting_exchange"
        components[2].attempt_count = 1
        desired = json.loads(components[2].desired_json)
        desired["protection_replacement_execution"] = {
            "backup_stop": "63872",
            "effective_remaining_size": "19",
            "old_stop_order_ids": [],
            "primary_stop": "64000",
            "retained_take_profit_total": "0",
        }
        components[2].desired_json = json.dumps(desired, sort_keys=True)
        entry_id = session.query(StrategyManagementLeg).one().execution_order_leg_id
        binding_id = int(batch.execution_binding_id)
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="terminal-first-tp",
                purpose="take_profit",
                trigger_price="66000",
                size_text="19",
                status="cancelled",
                evidence_source="test_terminal_readback",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_component_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=_snapshot(
                positions=[
                    {
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "19",
                    }
                ],
                trigger_history=[
                    {
                        "ordId": "terminal-first-tp",
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "triggerOrderType": "TPSL",
                        "tpTriggerPx": "66000",
                        "sz": "19",
                        "state": "cancelled",
                    }
                ],
            ),
        )


def test_resume_authorization_rejects_unowned_new_exchange_close(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_exchange_close_unowned",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=_snapshot(
                positions=[
                    {
                        "posId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "pos": "19",
                    }
                ],
                open_orders=[
                    {
                        "ordId": "external-close",
                        "closePosId": POS_ID,
                        "instId": "BTC-USDT-SWAP",
                        "posSide": "long",
                        "side": "sell",
                        "reduceOnly": True,
                        "sz": "19",
                        "state": "filled",
                    }
                ],
            ),
        )


def test_resume_authorization_bounds_malformed_snapshot(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    plan = _plan(factory)

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_evidence_invalid",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=object(),
        )


@pytest.mark.parametrize(
    "kind", ["batch", "component", "mutation", "instruction"]
)
def test_apply_rejects_new_additional_active_work_after_plan(tmp_path, kind):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    module = _recovery_module()
    plan = _plan(factory)
    with factory() as session:
        batch119 = session.get(StrategyManagementBatch, 119)
        if kind in {"batch", "component"}:
            other = _add_other_terminal_batch(
                session, binding_id=binding_id, batch119=batch119
            )
            if kind == "batch":
                other.status = "executing"
            else:
                session.add(
                    StrategyManagementComponent(
                        management_batch_id=other.id,
                        strategy_management_leg_id=None,
                        strategy_management_leg_scope=-1,
                        component_kind="cancel_deferred_entries",
                        sequence=0,
                        status="pending",
                        idempotency_key="new-active-component-after-plan",
                        desired_json="{}",
                        evidence_json="[]",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
        elif kind == "mutation":
            session.add(
                PositionMutationIntent(
                    idempotency_key="other-mutation-after-plan",
                    operation="replace_protection",
                    strategy_instance_id="other-strategy",
                    execution_binding_id=binding_id,
                    execution_order_leg_id=entry_id,
                    pos_id="other-position",
                    authority_fingerprint="a" * 64,
                    request_fingerprint="r" * 64,
                    status="reserved",
                    request_json="{}",
                    reserved_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        else:
            candidate = SignalCandidate(
                raw_message_id=10532,
                symbol="BTC",
                side="long",
                event_type="management",
                management_action="partial_then_break_even",
                review_status="confirmed",
                created_at=NOW,
            )
            session.add(candidate)
            session.flush()
            session.add(
                MessageInstructionItem(
                    raw_message_id=10532,
                    signal_candidate_id=candidate.id,
                    sequence=0,
                    instruction_kind="management",
                    strategy_instance_id="other-strategy",
                    idempotency_key="new-instruction-after-plan",
                    status="submitted",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_repeated_apply_requires_exact_audit_and_after_state(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    repaired = _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )

    repeated = _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )

    assert repaired.status == "repaired"
    assert repeated.status == "already_repaired"
    assert repeated.audit_event_id == repaired.audit_event_id
    with factory() as session:
        assert (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action
                == "composite_batch_false_state_repaired"
            )
            .count()
            == 1
        )
        assert session.query(ExecutionEvent).count() == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "batch_state",
        "batch_reconciled_at",
        "leg_last_error",
        "leg_request",
        "component_evidence",
        "component_reason",
        "component_completed",
        "audit_request",
        "audit_after",
        "audit_before_noncanonical",
        "audit_after_duplicate_key",
        "audit_action",
        "new_close_intent",
        "new_close_event",
    ],
)
def test_repeated_apply_rejects_tampered_audit_or_after_state(tmp_path, tamper):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    module = _recovery_module()
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        event = session.query(ExecutionEvent).one()
        if tamper == "batch_state":
            session.get(StrategyManagementBatch, 119).status = "resolved"
        elif tamper == "batch_reconciled_at":
            session.get(StrategyManagementBatch, 119).reconciled_at = NOW
        elif tamper == "leg_last_error":
            session.query(StrategyManagementLeg).one().last_error = "{}"
        elif tamper == "leg_request":
            session.query(StrategyManagementLeg).one().request_json = "{}"
        elif tamper == "component_evidence":
            session.query(StrategyManagementComponent).order_by(
                StrategyManagementComponent.sequence
            ).first().evidence_json = "[]"
        elif tamper == "component_reason":
            session.query(StrategyManagementComponent).order_by(
                StrategyManagementComponent.sequence
            ).first().reason_code = "drifted"
        elif tamper == "component_completed":
            session.query(StrategyManagementComponent).order_by(
                StrategyManagementComponent.sequence
            ).first().completed_at = NOW
        elif tamper == "audit_request":
            event.request_json = "{}"
        elif tamper == "audit_after":
            payload = json.loads(event.after_json)
            payload["batch_status"] = "succeeded"
            event.after_json = json.dumps(payload, sort_keys=True)
        elif tamper == "audit_before_noncanonical":
            event.before_json = json.dumps(json.loads(event.before_json))
        elif tamper == "audit_after_duplicate_key":
            event.after_json = (
                event.after_json[:-1]
                + ',"exchange_call_possible":false}'
            )
        else:
            if tamper == "audit_action":
                event.action = "unrelated_action"
            elif tamper == "new_close_intent":
                session.add(
                    PositionMutationIntent(
                        idempotency_key="new-close-after-repair",
                        operation="close_position",
                        strategy_instance_id="deepcoin:incident:btc:long",
                        execution_binding_id=binding_id,
                        execution_order_leg_id=entry_id,
                        pos_id=POS_ID,
                        authority_fingerprint="a" * 64,
                        request_fingerprint="r" * 64,
                        status="reserved",
                        request_json="{}",
                        reserved_at=NOW,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            else:
                session.add(
                    ExecutionEvent(
                        execution_binding_id=binding_id,
                        action="position_close_submitted",
                        status="submitted",
                        pos_id=POS_ID,
                        created_at=NOW,
                    )
                )
        session.commit()
    writer_calls = []

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=plan,
            prepare_writer=lambda: writer_calls.append(True)
            or SimpleNamespace(
                uid_scope_hash=(
                    _PLAN_SNAPSHOTS[id(plan)].account_authority.uid_scope_hash
                )
            ),
            expected_uid_scope_hash=(
                _PLAN_SNAPSHOTS[id(plan)].account_authority.uid_scope_hash
            ),
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    assert writer_calls == []
    with factory() as session:
        assert (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.notification_fingerprint
                == plan.evidence_fingerprint
            )
            .count()
            == 1
        )
        assert session.query(ExecutionEvent).count() == (
            2 if tamper == "new_close_event" else 1
        )


@pytest.mark.parametrize("kind", ["component", "mutation", "instruction"])
def test_repeated_apply_rejects_new_additional_active_work(tmp_path, kind):
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    module = _recovery_module()
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        batch119 = session.get(StrategyManagementBatch, 119)
        if kind == "component":
            other = _add_other_terminal_batch(
                session, binding_id=binding_id, batch119=batch119
            )
            session.add(
                StrategyManagementComponent(
                    management_batch_id=other.id,
                    strategy_management_leg_id=None,
                    strategy_management_leg_scope=-1,
                    component_kind="cancel_deferred_entries",
                    sequence=0,
                    status="pending",
                    idempotency_key="active-after-recovery-component",
                    desired_json="{}",
                    evidence_json="[]",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        elif kind == "mutation":
            session.add(
                PositionMutationIntent(
                    idempotency_key="active-after-recovery-mutation",
                    operation="replace_protection",
                    strategy_instance_id="other-strategy",
                    execution_binding_id=binding_id,
                    execution_order_leg_id=entry_id,
                    pos_id="other-position",
                    authority_fingerprint="a" * 64,
                    request_fingerprint="r" * 64,
                    status="reserved",
                    request_json="{}",
                    reserved_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        else:
            candidate = SignalCandidate(
                raw_message_id=10532,
                symbol="BTC",
                side="long",
                event_type="management",
                management_action="partial_then_break_even",
                review_status="confirmed",
                created_at=NOW,
            )
            session.add(candidate)
            session.flush()
            session.add(
                MessageInstructionItem(
                    raw_message_id=10532,
                    signal_candidate_id=candidate.id,
                    sequence=0,
                    instruction_kind="management",
                    strategy_instance_id="other-strategy",
                    idempotency_key="active-after-recovery-instruction",
                    status="submitted",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert (
            session.query(ExecutionEvent)
            .filter_by(notification_fingerprint=plan.evidence_fingerprint)
            .count()
            == 1
        )


def test_apply_rejects_deep_forged_plan_as_bounded_conflict(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    evidence = module.serialize_composite_batch_recovery_plan(plan)["evidence"]
    deeply_nested = 0
    for _ in range(1100):
        deeply_nested = [deeply_nested]
    evidence["untrusted"] = deeply_nested
    forged = replace(plan, evidence=evidence)

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=forged,
            expected_fingerprint=forged.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


@pytest.mark.parametrize("field", ["attempt_counts", "protection_count"])
def test_apply_binds_plan_counts_to_locked_durable_rows(tmp_path, field):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    evidence = module.serialize_composite_batch_recovery_plan(plan)["evidence"]
    if field == "attempt_counts":
        evidence["durable"]["component_attempt_counts"] = [99, 98, 97]
    else:
        evidence["exchange"]["owned_protection_count"] = 99
    forged_fingerprint = sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    forged = replace(
        plan,
        evidence=evidence,
        evidence_fingerprint=forged_fingerprint,
    )

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=forged,
            expected_fingerprint=forged.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_apply_rejects_malformed_position_as_bounded_conflict(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    forged = replace(plan, position=object())

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=forged,
            expected_fingerprint=forged.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_apply_under_target_appends_bounded_attestation_without_identity_drift(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    position = dict(_snapshot().positions[0], pos="12")
    plan = _plan(factory, _snapshot(positions=[position]))
    assert plan.position.disposition == "protection_only_below_target"
    with factory() as session:
        components = (
            session.query(StrategyManagementComponent)
            .order_by(StrategyManagementComponent.sequence)
            .all()
        )
        identities = [
            (row.id, row.idempotency_key, row.desired_json) for row in components
        ]

    result = _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )

    assert result.status == "repaired"
    attestation = {
        "kind": "approved_under_target_recovery",
        "actual_remaining_size": "12",
        "original_target_remaining_size": "19",
        "recovery_evidence_fingerprint": plan.evidence_fingerprint,
    }
    with factory() as session:
        components = (
            session.query(StrategyManagementComponent)
            .order_by(StrategyManagementComponent.sequence)
            .all()
        )
        assert [
            (row.id, row.idempotency_key, row.desired_json) for row in components
        ] == identities
        assert json.loads(components[0].evidence_json) == [
            {"error_type": "RuntimeError"},
            attestation,
        ]
        assert all(
            json.loads(row.evidence_json) == [attestation]
            for row in components[1:]
        )


def test_under_target_attestation_cannot_authorize_increased_position(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    position = dict(_snapshot().positions[0], pos="12")
    plan = _plan(factory, _snapshot(positions=[position]))
    increased = replace(
        plan.position,
        current_size="20",
        effective_remaining_size="20",
    )
    evidence = module.serialize_composite_batch_recovery_plan(plan)["evidence"]
    evidence["position"] = {
        "disposition": increased.disposition,
        "current_size": increased.current_size,
        "close_delta": increased.close_delta,
        "effective_remaining_size": increased.effective_remaining_size,
    }
    evidence["proposed_transition"]["actual_remaining_size"] = "20"
    forged_fingerprint = sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    forged = replace(
        plan,
        position=increased,
        evidence=evidence,
        evidence_fingerprint=forged_fingerprint,
    )

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=forged,
            expected_fingerprint=forged.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_apply_position_absent_terminalizes_without_exchange_intent(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    assert plan.position.disposition == "position_absent"
    with factory() as session:
        desired = [
            row.desired_json
            for row in session.query(StrategyManagementComponent)
            .order_by(StrategyManagementComponent.sequence)
            .all()
        ]

    result = _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )

    assert result.status == "repaired"
    terminal_fact = {
        "kind": "composite_recovery_exact_position_absent",
        "recovery_evidence_fingerprint": plan.evidence_fingerprint,
    }
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        leg = session.query(StrategyManagementLeg).one()
        components = (
            session.query(StrategyManagementComponent)
            .order_by(StrategyManagementComponent.sequence)
            .all()
        )
        event = session.query(ExecutionEvent).one()
        assert batch.status == "resolved"
        assert batch.reason_code == "composite_recovery_exact_position_absent"
        assert batch.reconciled_at == NOW.replace(tzinfo=None)
        assert batch.completed_at == NOW.replace(tzinfo=None)
        assert leg.status == "failed"
        assert json.loads(leg.last_error) == {
            "reason": "composite_recovery_exact_position_absent",
            "recovery_evidence_fingerprint": plan.evidence_fingerprint,
        }
        assert all(
            value is None
            for value in (
                leg.request_json,
                leg.response_json,
                leg.client_order_id,
                leg.exchange_order_id,
            )
        )
        assert [row.status for row in components] == [
            "safely_skipped",
            "safely_skipped",
            "safely_skipped",
        ]
        assert [row.reason_code for row in components] == [
            "composite_recovery_exact_position_absent"
        ] * 3
        assert all(row.completed_at == NOW.replace(tzinfo=None) for row in components)
        assert [row.desired_json for row in components] == desired
        assert json.loads(components[0].evidence_json) == [
            {"error_type": "RuntimeError"},
            terminal_fact,
        ]
        assert all(
            json.loads(row.evidence_json) == [terminal_fact]
            for row in components[1:]
        )
        assert session.query(PositionMutationIntent).count() == 0
        assert event.action == "composite_batch_false_state_repaired"
        after = json.loads(event.after_json)
        assert after["batch_status"] == "resolved"
        assert after["exchange_call_possible"] is False
        assert after["original_owned_stop_refs"] == []

    repeated = _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    assert repeated.status == "already_repaired"

    from telegram_kol_research.trading_settings import save_trading_settings

    save_trading_settings(
        factory,
        {"mimo_contract_mode": "v2_live_adapter"},
        updated_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="mimo_contract_mode_not_v1",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            snapshot=snapshot,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    with factory() as session:
        assert session.query(ExecutionEvent).count() == 1

    from telegram_kol_research.strategy_management_composite_executor import (
        execute_composite_management_batch,
    )

    with pytest.raises(ValueError, match="composite_batch_not_executable:resolved"):
        execute_composite_management_batch(
            factory,
            batch_id=119,
            deepcoin_client=SimpleNamespace(),
            contract_spec_provider=None,
            live_execution_gate=lambda: True,
            now_provider=lambda: NOW,
        )


@pytest.mark.parametrize(
    "forgery",
    ["natural_stop_ref", "exchange_scope", "snapshot_authority"],
)
def test_position_absent_apply_rejects_resigned_snapshot_envelope_forgery(
    tmp_path,
    forgery,
):
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    save_trading_settings(
        factory,
        {"mimo_contract_mode": "v1"},
        updated_at=NOW,
    )
    evidence = module.serialize_composite_batch_recovery_plan(plan)["evidence"]
    if forgery == "natural_stop_ref":
        evidence["natural_stop"]["order_ref"] = "f" * 64
        forged = replace(plan, evidence=evidence)
    elif forgery == "exchange_scope":
        evidence["exchange_snapshot_fingerprint"] = "e" * 64
        forged = replace(
            plan,
            exchange_snapshot_fingerprint="e" * 64,
            evidence=evidence,
        )
    else:
        snapshot.scope_fingerprint = "e" * 64
        forged = replace(plan, evidence=evidence)
    resigned_fingerprint = sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    forged = replace(forged, evidence_fingerprint=resigned_fingerprint)

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=forged,
            expected_fingerprint=resigned_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
            snapshot=snapshot,
            require_mimo_v1=True,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_position_absent_apply_rejects_fully_resigned_durable_scope_forgery(
    tmp_path,
):
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory, _natural_stop_snapshot())
    forged_snapshot = _natural_stop_snapshot(
        scope_protection_overrides={
            "backup_stop": {"trigger_price": "1"},
        }
    )
    evidence = module.serialize_composite_batch_recovery_plan(plan)["evidence"]
    with factory() as session:
        ledger = (
            session.query(PositionProtectionLedger)
            .order_by(PositionProtectionLedger.id)
            .all()
        )
    forged_exchange = module._fingerprint(
        module._exchange_evidence_payload(
            forged_snapshot,
            position=plan.position,
            pos_id=POS_ID,
            ledger=ledger,
            profile=module.BATCH_119_RECOVERY,
            natural_stop_proof=evidence["natural_stop"],
        )
    )
    evidence["exchange_snapshot_fingerprint"] = forged_exchange
    forged_fingerprint = module._fingerprint(evidence)
    forged = replace(
        plan,
        exchange_snapshot_fingerprint=forged_exchange,
        evidence_fingerprint=forged_fingerprint,
        evidence=evidence,
    )
    save_trading_settings(
        factory,
        {"mimo_contract_mode": "v1"},
        updated_at=NOW,
    )

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _apply_recovery(module,
            factory,
            plan=forged,
            snapshot=forged_snapshot,
            expected_fingerprint=forged_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


@pytest.mark.parametrize("invalid_snapshot", [None, object()])
def test_apply_rejects_invalid_snapshot_before_opening_session(
    tmp_path,
    invalid_snapshot,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("invalid snapshot opened database session")

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="recovery_snapshot_invalid",
    ):
        _apply_recovery(module,
            forbidden_factory,
            plan=plan,
            snapshot=invalid_snapshot,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    assert factory_calls == []


def test_capture_capability_is_loader_only_not_serialized_and_rejects_copy(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    serialized = json.dumps(
        module.serialize_composite_batch_recovery_plan(plan),
        sort_keys=True,
    )
    assert "capture_seal" not in serialized
    assert not hasattr(module, "_BATCH119_CAPTURE_HMAC_KEY")
    assert not hasattr(module, "_seal_batch119_recovery_snapshot")
    assert not hasattr(snapshot, "_capture_seal")
    tampered = replace(
        snapshot,
        positions=[{**snapshot.positions[0], "pos": "37"}],
    )
    assert module._snapshot_is_complete(
        snapshot,
        profile=module.BATCH_119_RECOVERY,
    )
    assert not module._snapshot_is_complete(
        tampered,
        profile=module.BATCH_119_RECOVERY,
    )


def test_non_absent_fully_resigned_exchange_with_stale_seal_is_preflight_refused(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    original = _snapshot()
    forged_snapshot = _snapshot(
        open_orders=[
            {
                "ordId": "unrelated-open-order",
                "instId": "ETH-USDT-SWAP",
                "state": "live",
            }
        ],
    )
    forged_plan = _plan(factory, forged_snapshot)
    assert forged_plan.status == "ready"
    assert (
        forged_plan.exchange_snapshot_fingerprint
        != _plan(factory, original).exchange_snapshot_fingerprint
    )
    forged_snapshot = replace(forged_snapshot)
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("stale capture seal opened database session")

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="recovery_snapshot_invalid",
    ):
        _apply_recovery(
            module,
            forbidden_factory,
            plan=forged_plan,
            snapshot=forged_snapshot,
            expected_fingerprint=forged_plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    assert factory_calls == []


@pytest.mark.parametrize("malformed", ["wide", "deep", "nan", "bad_type"])
def test_capture_capability_preflight_is_bounded_before_session(
    tmp_path,
    malformed,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    if malformed == "wide":
        snapshot.positions = [dict(snapshot.positions[0]) for _ in range(101)]
    elif malformed == "deep":
        value = {}
        for _ in range(66):
            value = {"nested": value}
        snapshot.positions[0]["deep"] = value
    elif malformed == "nan":
        snapshot.positions[0]["value"] = float("nan")
    else:
        snapshot.positions[0]["value"] = object()
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("unbounded snapshot opened database session")

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="recovery_snapshot_invalid",
    ):
        _apply_recovery(
            module,
            forbidden_factory,
            plan=plan,
            snapshot=snapshot,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    assert factory_calls == []


def test_capture_capability_preflight_rejects_container_subclass_before_session(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)

    class UnboundedList(list):
        def __iter__(self):
            raise RuntimeError("private iterator detail")

    snapshot.positions = UnboundedList()
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("container subclass opened database session")

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="recovery_snapshot_invalid",
    ):
        _apply_recovery(
            module,
            forbidden_factory,
            plan=plan,
            snapshot=snapshot,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    assert factory_calls == []


def test_capture_capability_accepts_bounded_nested_builtin_exchange_row(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot(
        positions=[
            {
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "38",
                "metadata": {"source": "exchange"},
            }
        ]
    )

    assert snapshot.errors == {}
    assert module._snapshot_is_complete(
        snapshot,
        profile=module.BATCH_119_RECOVERY,
    )
    assert _plan(factory, snapshot).status == "ready"


@pytest.mark.parametrize("failure", ["build", "uid"])
def test_non_absent_locked_writer_prepare_failure_rolls_back_without_bytes(
    tmp_path,
    failure,
):
    factory, database, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    before = _file_signature(Path(database))
    calls = []

    def prepare_writer():
        calls.append(True)
        if failure == "build":
            raise RuntimeError("lazy writer build failed")
        return SimpleNamespace(uid_scope_hash="0" * 64)

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match=(
            "exchange_writer_client_unavailable"
            if failure == "build"
            else "exchange_account_scope_mismatch"
        ),
    ):
        _apply_recovery(
            module,
            factory,
            plan=plan,
            snapshot=snapshot,
            prepare_writer=prepare_writer,
            expected_uid_scope_hash=snapshot.account_authority.uid_scope_hash,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )

    assert calls == [True]
    assert _file_signature(Path(database)) == before
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


@pytest.mark.parametrize("action", ["apply", "resume"])
def test_locked_writer_uid_must_match_sealed_capture_authority(
    tmp_path,
    action,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    if action == "resume":
        _apply_recovery(
            module,
            factory,
            plan=plan,
            snapshot=snapshot,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    wrong_uid = "6" * 64

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="exchange_account_scope_mismatch",
    ):
        if action == "apply":
            _apply_recovery(
                module,
                factory,
                plan=plan,
                snapshot=snapshot,
                prepare_writer=lambda: SimpleNamespace(
                    uid_scope_hash=wrong_uid
                ),
                expected_uid_scope_hash=wrong_uid,
                expected_fingerprint=plan.evidence_fingerprint,
                authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
                applied_at=NOW,
            )
        else:
            _authorize_recovery(
                module,
                factory,
                expected_fingerprint=plan.evidence_fingerprint,
                snapshot=snapshot,
                prepare_writer=lambda: SimpleNamespace(
                    uid_scope_hash=wrong_uid
                ),
                expected_uid_scope_hash=wrong_uid,
            )

    with factory() as session:
        expected_events = 1 if action == "resume" else 0
        assert session.query(ExecutionEvent).count() == expected_events
        assert session.get(StrategyManagementBatch, 119).status == (
            "ready" if action == "resume" else "reconciling"
        )


def test_non_absent_apply_rechecks_mimo_v1_inside_locked_transaction(tmp_path):
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    plan = _plan(factory)
    assert plan.position.disposition == "resume_to_target"
    save_trading_settings(
        factory,
        {"mimo_contract_mode": "v2_live_adapter"},
        updated_at=NOW,
    )

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="mimo_contract_mode_not_v1",
    ):
        _apply_recovery(module,
            factory,
            plan=plan,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
            require_mimo_v1=True,
        )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


@pytest.mark.parametrize("action", ["first", "repeat", "resume"])
@pytest.mark.parametrize(
    ("setting_patch", "malformed"),
    [
        ({"auto_trade_enabled": False}, False),
        ({"management_execution_mode": "disabled"}, False),
        ({"management_execution_mode": "shadow"}, False),
        ({"composite_management_v2_mode": "disabled"}, False),
        ({"auto_trade_enabled": "true"}, True),
    ],
)
def test_non_absent_locked_gate_uses_effective_live_settings_without_writer(
    tmp_path,
    action,
    setting_patch,
    malformed,
):
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    if action != "first":
        _apply_recovery(
            module,
            factory,
            plan=plan,
            snapshot=snapshot,
            expected_fingerprint=plan.evidence_fingerprint,
            authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            applied_at=NOW,
        )
    if malformed:
        with factory() as session:
            row = session.query(TradingSetting).filter_by(key="global").one()
            payload = json.loads(row.value_json)
            payload.update(setting_patch)
            row.value_json = json.dumps(payload, sort_keys=True)
            session.commit()
    else:
        save_trading_settings(factory, setting_patch, updated_at=NOW)
    with factory() as session:
        before = (
            session.get(StrategyManagementBatch, 119).status,
            session.query(ExecutionEvent).count(),
            session.query(TradingSetting).filter_by(key="global").one().value_json,
        )
    writer_calls = []
    expected_reason = (
        "mimo_contract_mode_not_v1"
        if malformed
        else "composite_management_live_gate_closed"
    )

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match=expected_reason,
    ):
        if action in {"first", "repeat"}:
            _apply_recovery(
                module,
                factory,
                plan=plan,
                snapshot=snapshot,
                prepare_writer=lambda: writer_calls.append(True)
                or SimpleNamespace(
                    uid_scope_hash=snapshot.account_authority.uid_scope_hash
                ),
                expected_uid_scope_hash=(
                    snapshot.account_authority.uid_scope_hash
                ),
                expected_fingerprint=plan.evidence_fingerprint,
                authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
                applied_at=NOW,
            )
        else:
            _authorize_recovery(
                module,
                factory,
                expected_fingerprint=plan.evidence_fingerprint,
                snapshot=snapshot,
                prepare_writer=lambda: writer_calls.append(True)
                or SimpleNamespace(
                    uid_scope_hash=snapshot.account_authority.uid_scope_hash
                ),
                expected_uid_scope_hash=(
                    snapshot.account_authority.uid_scope_hash
                ),
            )

    assert writer_calls == []
    with factory() as session:
        after = (
            session.get(StrategyManagementBatch, 119).status,
            session.query(ExecutionEvent).count(),
            session.query(TradingSetting).filter_by(key="global").one().value_json,
        )
    assert after == before


def test_non_absent_repeat_and_resume_recheck_locked_mimo_v1(tmp_path):
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    save_trading_settings(
        factory,
        {"mimo_contract_mode": "v1"},
        updated_at=NOW,
    )
    repaired = _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    assert repaired.status == "repaired"
    save_trading_settings(
        factory,
        {"mimo_contract_mode": "v2_live_adapter"},
        updated_at=NOW + timedelta(seconds=1),
    )

    for action in ("repeat", "resume"):
        with pytest.raises(
            module.CompositeBatchRecoveryConflict,
            match="mimo_contract_mode_not_v1",
        ):
            if action == "repeat":
                _apply_recovery(module,
                    factory,
                    plan=plan,
                    expected_fingerprint=plan.evidence_fingerprint,
                    authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
                    applied_at=NOW,
                )
            else:
                _authorize_recovery(module,
                    factory,
                    expected_fingerprint=plan.evidence_fingerprint,
                    snapshot=snapshot,
                    require_mimo_v1=True,
                )

    with factory() as session:
        assert session.query(ExecutionEvent).count() == 1


def test_non_absent_resume_rejects_fresh_exact_position_disappearance(
    tmp_path,
):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(
        module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    disappeared = _snapshot(
        positions=[],
        pending_trigger_orders=[],
    )
    assert module._snapshot_is_complete(
        disappeared,
        profile=module.BATCH_119_RECOVERY,
    )
    writer_calls = []

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="position_absent_snapshot_invalid",
    ):
        _authorize_recovery(
            module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=disappeared,
            prepare_writer=lambda: writer_calls.append(True)
            or SimpleNamespace(
                uid_scope_hash=(
                    disappeared.account_authority.uid_scope_hash
                )
            ),
            expected_uid_scope_hash=(
                disappeared.account_authority.uid_scope_hash
            ),
        )

    assert writer_calls == []


def test_non_absent_resume_rejects_unowned_fresh_position_reduction(tmp_path):
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(
        module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    reduced = _snapshot(
        positions=[
            {
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "19",
            }
        ],
    )

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="position_absent_snapshot_invalid",
    ):
        _authorize_recovery(
            module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=reduced,
        )


@pytest.mark.parametrize(
    "drift",
    ["raw", "lifecycle", "ledger", "audit", "event", "snapshot"],
)
def test_non_absent_resume_validates_all_locked_evidence_before_writer(
    tmp_path,
    drift,
):
    factory, _, binding_id, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _snapshot()
    plan = _plan(factory, snapshot)
    _apply_recovery(
        module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    if drift == "snapshot":
        snapshot = replace(snapshot)
    else:
        with factory() as session:
            if drift == "raw":
                session.get(RawMessage, 10532).chat_id = 999
            elif drift == "lifecycle":
                session.get(StrategyLifecycle, 794).lifecycle_status = "exited"
            elif drift == "ledger":
                session.query(PositionProtectionLedger).first().trigger_price = "1"
            elif drift == "audit":
                session.query(ExecutionEvent).one().action = "unrelated_action"
            else:
                session.add(
                    ExecutionEvent(
                        execution_binding_id=binding_id,
                        strategy_instance_id="deepcoin:incident:btc:long",
                        action="strategy_management_close_submit",
                        status="confirmed",
                        pos_id=POS_ID,
                        created_at=NOW,
                    )
                )
            session.commit()
    with factory() as session:
        before = (
            session.get(StrategyManagementBatch, 119).status,
            session.query(ExecutionEvent).count(),
        )
    writer_calls = []

    with pytest.raises(module.CompositeBatchRecoveryConflict):
        _authorize_recovery(
            module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
            prepare_writer=lambda: writer_calls.append(True)
            or SimpleNamespace(uid_scope_hash="5" * 64),
            expected_uid_scope_hash="5" * 64,
        )

    assert writer_calls == []
    with factory() as session:
        assert (
            session.get(StrategyManagementBatch, 119).status,
            session.query(ExecutionEvent).count(),
        ) == before


def test_position_absent_mimo_gate_query_error_rolls_back_and_releases_lock(
    tmp_path,
    monkeypatch,
):
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    module = _recovery_module()
    snapshot = _natural_stop_snapshot()
    plan = _plan(factory, snapshot)
    save_trading_settings(
        factory,
        {"mimo_contract_mode": "v1"},
        updated_at=NOW,
    )
    original_all = Query.all

    def fail_setting_query(query):
        entities = {
            item.get("entity") for item in query.column_descriptions
        }
        if module.TradingSetting in entities:
            raise OperationalError(
                "forced locked setting query failure",
                {},
                RuntimeError("forced setting query failure"),
            )
        return original_all(query)

    with monkeypatch.context() as gate_patch:
        gate_patch.setattr(Query, "all", fail_setting_query)
        with pytest.raises(
            module.CompositeBatchRecoveryConflict,
            match="mimo_contract_mode_not_v1",
        ):
            _apply_recovery(module,
                factory,
                plan=plan,
                snapshot=snapshot,
                expected_fingerprint=plan.evidence_fingerprint,
                authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
                applied_at=NOW,
            )

    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0
    save_trading_settings(
        factory,
        {"mimo_contract_mode": "v1"},
        updated_at=NOW + timedelta(seconds=1),
    )
    repaired = _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    assert repaired.status == "repaired"


class _Batch119RecoveryClient:
    uid_scope_hash = "9" * 64

    def __init__(self, *, fail_close_readback_once: bool = False):
        self.current_size = "38"
        self.pending = [
            {
                "ordId": "batch-119-first-tp",
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "tpTriggerPx": "66000",
                "sz": "19",
                "state": "live",
            },
            {
                "ordId": PRIMARY_ORDER_ID,
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPx": "64000",
                "sz": "38",
                "state": "live",
            },
            {
                "ordId": BACKUP_ORDER_ID,
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPx": "63000",
                "sz": "38",
                "state": "live",
            },
        ]
        self.order_history = []
        self.trigger_history = []
        self.trade_fills = []
        self.position_history = []
        self.position_history_calls = []
        self.close_calls = []
        self.set_calls = []
        self.cancel_calls = []
        self.fail_close_readback_once = fail_close_readback_once
        self.close_submitted = False

    def list_positions(self, *, inst_id=None):
        return [
            {
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": self.current_size,
                "avgPx": "62000",
                "markPx": "65000",
                "mgnMode": "cross",
                "mrgPosition": "split",
            }
        ]

    def read_positions(self, *, inst_id=None):
        return {"data": self.list_positions(inst_id=inst_id)}

    def list_open_orders(self):
        return []

    def read_open_orders(self, *, inst_id=None):
        return {"data": self.list_open_orders()}

    def list_position_history(self, *, inst_id, pos_id=None):
        if pos_id is not None:
            self.position_history_calls.append((inst_id, pos_id))
        return [dict(row) for row in self.position_history]

    def read_position_history(self, *, inst_id, pos_id):
        return {
            "data": self.list_position_history(
                inst_id=inst_id,
                pos_id=pos_id,
            )
        }

    def list_trigger_orders_pending(self, *, inst_id):
        return [dict(row) for row in self.pending]

    def read_trigger_orders_pending(self, *, inst_id):
        return {"data": self.list_trigger_orders_pending(inst_id=inst_id)}

    def list_trigger_orders_history(self, *, inst_id):
        return [dict(row) for row in self.trigger_history]

    def list_trigger_order_history(self, *, inst_id):
        return [dict(row) for row in self.trigger_history]

    def read_trigger_order_history(self, *, inst_id, order_id, limit):
        assert limit == 100
        return {
            "data": [
                dict(row)
                for row in self.trigger_history
                if str(row.get("ordId") or row.get("orderId") or "")
                == order_id
            ]
        }

    def list_order_history(self, *, inst_id):
        if self.close_submitted and self.fail_close_readback_once:
            self.fail_close_readback_once = False
            raise RuntimeError("simulated close readback interruption")
        return [dict(row) for row in self.order_history]

    def list_trade_fills(self, *, inst_id):
        return [dict(row) for row in self.trade_fills]

    def cancel_position_sltp(self, payload):
        order_id = str(payload["ordId"])
        self.cancel_calls.append(dict(payload))
        matches = [row for row in self.pending if row["ordId"] == order_id]
        if len(matches) != 1:
            return {"code": "0", "data": {"ordId": order_id}}
        row = matches[0]
        self.pending.remove(row)
        self.trigger_history.append({**row, "state": "cancelled"})
        return {"code": "0", "data": {"ordId": order_id}}

    def place_order(self, payload):
        self.close_calls.append(dict(payload))
        self.close_submitted = True
        self.current_size = "19"
        row = {
            "ordId": "batch-119-close",
            "posId": POS_ID,
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "side": "sell",
            "reduceOnly": True,
            "sz": str(payload["sz"]),
            "state": "filled",
        }
        self.order_history.append(row)
        self.trade_fills.append({**row, "fillSz": str(payload["sz"])})
        return {"code": "0", "data": {"ordId": "batch-119-close"}}

    def set_position_sltp(self, payload):
        self.set_calls.append(dict(payload))
        order_id = f"batch-119-new-stop-{len(self.set_calls)}"
        self.pending.append(
            {
                "ordId": order_id,
                "posId": POS_ID,
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "slTriggerPx": str(payload["slTriggerPx"]),
                "sz": str(payload["sz"]),
                "state": "live",
            }
        )
        return {"code": "0", "data": {"ordId": order_id}}


def test_recovery_snapshot_loads_exact_position_history_and_blocks_prior_close(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    client = _Batch119RecoveryClient()
    client.position_history = [
        {
            "closePosId": POS_ID,
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "state": "closed",
            "closeSz": "1",
        }
    ]

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )
    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )

    assert client.position_history_calls == [
        ("BTC-USDT-SWAP", POS_ID)
    ]
    assert snapshot.position_history == client.position_history
    assert plan.status == "refused"
    assert plan.reason_code == "exchange_close_submission_evidence_present"


def test_recovery_snapshot_marks_position_history_failure_incomplete(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    client = _Batch119RecoveryClient()

    def fail_position_history(*, inst_id, pos_id):
        raise RuntimeError("private provider detail")

    client.read_position_history = fail_position_history

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )
    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )

    assert snapshot.position_history == []
    assert snapshot.errors["position_history"] == "snapshot_read_unavailable"
    assert plan.status == "refused"
    assert plan.reason_code == "exchange_snapshot_incomplete"


class _Batch119ExactHistoryClient:
    uid_scope_hash = "8" * 64

    def __init__(self, *, exact_position_response=None):
        self.instrument_wide_history_calls = 0
        self.exact_position_ids = []
        self.exact_order_ids = []
        self.network_calls = 0
        self.exact_position_response = exact_position_response or {
            "data": [
                {
                    "posId": POS_ID,
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "state": "closed",
                }
            ]
        }

    @staticmethod
    def _wide_rows(kind):
        return [
            {
                "ordId": f"wide-{kind}-{index}",
                "instId": "BTC-USDT-SWAP",
            }
            for index in range(100)
        ]

    def read_positions(self, *, inst_id=None):
        self.network_calls += 1
        return {
            "data": [
                {
                    "posId": POS_ID,
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "pos": "38",
                }
            ]
        }

    def list_positions(self, *, inst_id=None):
        return list(self.read_positions(inst_id=inst_id)["data"])

    def read_open_orders(self, *, inst_id=None):
        self.network_calls += 1
        return {"data": []}

    def list_open_orders(self, *, inst_id=None):
        return list(self.read_open_orders(inst_id=inst_id)["data"])

    def read_trigger_orders_pending(self, *, inst_id):
        self.network_calls += 1
        return {
            "data": [
                {
                    "ordId": PRIMARY_ORDER_ID,
                    "posId": POS_ID,
                    "instId": inst_id,
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "slTriggerPx": "64000",
                    "sz": "38",
                    "state": "live",
                },
                {
                    "ordId": BACKUP_ORDER_ID,
                    "posId": POS_ID,
                    "instId": inst_id,
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "slTriggerPx": "63000",
                    "sz": "38",
                    "state": "live",
                },
            ]
        }

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.read_trigger_orders_pending(inst_id=inst_id)["data"])

    def read_position_history(self, *, inst_id, pos_id):
        self.network_calls += 1
        self.exact_position_ids.append(pos_id)
        return self.exact_position_response

    def list_position_history(self, *, inst_id, pos_id=None):
        if pos_id is not None:
            return list(
                self.read_position_history(inst_id=inst_id, pos_id=pos_id)[
                    "data"
                ]
            )
        self.instrument_wide_history_calls += 1
        return self._wide_rows("positions")

    def read_trigger_order_history(self, *, inst_id, order_id, limit):
        self.network_calls += 1
        assert limit == 100
        self.exact_order_ids.append(order_id)
        return {
            "data": [
                {
                    "ordId": order_id,
                    "posId": POS_ID,
                    "instId": inst_id,
                    "posSide": "long",
                    "state": "cancelled",
                }
            ]
        }

    def list_trigger_order_history(self, *, inst_id):
        self.instrument_wide_history_calls += 1
        return self._wide_rows("triggers")

    def list_order_history(self, *, inst_id):
        self.instrument_wide_history_calls += 1
        return self._wide_rows("orders")

    def list_trade_fills(self, *, inst_id):
        self.instrument_wide_history_calls += 1
        return self._wide_rows("fills")


class _Batch119AbsentExactHistoryClient(_Batch119ExactHistoryClient):
    def __init__(self):
        super().__init__(
            exact_position_response={
                "data": [_closed_position_history_row()]
            }
        )

    def read_positions(self, *, inst_id=None):
        self.network_calls += 1
        return {"data": []}

    def read_trigger_orders_pending(self, *, inst_id):
        self.network_calls += 1
        return {"data": []}

    def read_trigger_order_history(self, *, inst_id, order_id, limit):
        self.network_calls += 1
        assert limit == 100
        self.exact_order_ids.append(order_id)
        return {
            "data": (
                [_successful_stop_trigger_row()]
                if order_id == PRIMARY_ORDER_ID
                else []
            )
        }


def test_batch119_snapshot_uses_only_exact_history_scope(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {}
    assert client.instrument_wide_history_calls == 0
    assert client.exact_position_ids == [POS_ID]
    assert set(client.exact_order_ids) == {
        PRIMARY_ORDER_ID,
        BACKUP_ORDER_ID,
    }
    assert len(client.exact_order_ids) == 2
    assert client.network_calls == 6
    assert snapshot.account_authority.complete is True
    assert len(snapshot.scope_fingerprint) == 64
    assert POS_ID not in repr(snapshot)
    assert PRIMARY_ORDER_ID not in repr(snapshot)
    assert BACKUP_ORDER_ID not in repr(snapshot)


def test_batch119_exact_history_call_bound_has_six_gets_and_no_write_reachability(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)

    class StrictCallBoundClient(_Batch119ExactHistoryClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        def read_positions(self, *, inst_id=None):
            self.calls.append(("read_positions", {"inst_id": inst_id}))
            return super().read_positions(inst_id=inst_id)

        def read_open_orders(self, *, inst_id=None):
            self.calls.append(("read_open_orders", {"inst_id": inst_id}))
            return super().read_open_orders(inst_id=inst_id)

        def read_trigger_orders_pending(self, *, inst_id):
            self.calls.append(
                ("read_trigger_orders_pending", {"inst_id": inst_id})
            )
            return super().read_trigger_orders_pending(inst_id=inst_id)

        def read_position_history(self, *, inst_id, pos_id):
            self.calls.append(
                (
                    "read_position_history",
                    {"inst_id": inst_id, "pos_id": pos_id},
                )
            )
            self.network_calls += 1
            self.exact_position_ids.append(pos_id)
            return self.exact_position_response

        def read_trigger_order_history(self, *, inst_id, order_id, limit):
            self.calls.append(
                (
                    "read_trigger_order_history",
                    {
                        "inst_id": inst_id,
                        "order_id": order_id,
                        "limit": limit,
                    },
                )
            )
            return super().read_trigger_order_history(
                inst_id=inst_id,
                order_id=order_id,
                limit=limit,
            )

        def read_order_history(self, **kwargs):
            raise AssertionError("regular-order history is not in batch119 scope")

        def read_trade_fills(self, **kwargs):
            raise AssertionError("trade fills are not in batch119 scope")

        def list_position_history(self, **kwargs):
            raise AssertionError("instrument-wide position history is forbidden")

        def list_trigger_order_history(self, **kwargs):
            raise AssertionError("instrument-wide trigger history is forbidden")

        def list_order_history(self, **kwargs):
            raise AssertionError("instrument-wide order history is forbidden")

        def list_trade_fills(self, **kwargs):
            raise AssertionError("instrument-wide fills are forbidden")

        def place_order(self, payload):
            raise AssertionError("POST is unreachable from exact dry-run")

        def cancel_position_sltp(self, payload):
            raise AssertionError("cancel is unreachable from exact dry-run")

        def close_position(self, payload):
            raise AssertionError("close is unreachable from exact dry-run")

        def set_position_sltp(self, payload):
            raise AssertionError("TPSL write is unreachable from exact dry-run")

    client = StrictCallBoundClient()
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {}
    assert client.calls == [
        ("read_positions", {"inst_id": "BTC-USDT-SWAP"}),
        ("read_open_orders", {"inst_id": "BTC-USDT-SWAP"}),
        (
            "read_trigger_orders_pending",
            {"inst_id": "BTC-USDT-SWAP"},
        ),
        (
            "read_position_history",
            {"inst_id": "BTC-USDT-SWAP", "pos_id": POS_ID},
        ),
        (
            "read_trigger_order_history",
            {
                "inst_id": "BTC-USDT-SWAP",
                "order_id": BACKUP_ORDER_ID,
                "limit": 100,
            },
        ),
        (
            "read_trigger_order_history",
            {
                "inst_id": "BTC-USDT-SWAP",
                "order_id": PRIMARY_ORDER_ID,
                "limit": 100,
            },
        ),
    ]
    assert client.network_calls == 6
    assert client.instrument_wide_history_calls == 0


def test_batch119_planner_rejects_durable_scope_drift_after_exact_capture(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119AbsentExactHistoryClient(),
    )
    assert snapshot.errors == {}
    assert snapshot.exact_scope.protection_orders == (
        ("backup_stop", BACKUP_ORDER_ID),
        ("stop_loss", PRIMARY_ORDER_ID),
    )
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.order_id = "safe-backup-after-exact-capture"
        session.commit()

    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )

    assert plan.status == "refused"
    assert plan.reason_code == "durable_snapshot_scope_mismatch"


def test_batch119_planner_rejects_purpose_mapping_drift_after_exact_capture(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119AbsentExactHistoryClient(),
    )
    with factory() as session:
        primary = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=PRIMARY_ORDER_ID)
            .one()
        )
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        primary.purpose = "backup_stop"
        backup.purpose = "stop_loss"
        session.commit()

    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )

    assert plan.status == "refused"
    assert plan.reason_code == "durable_snapshot_scope_mismatch"


@pytest.mark.parametrize(
    ("field_name", "drifted_value"),
    [
        ("trigger_price", "1"),
        ("size_text", "1"),
        ("evidence_json", '{"drift":"after-capture"}'),
        ("evidence_source", "drifted_after_exact_capture"),
    ],
)
def test_batch119_planner_rejects_protection_evidence_drift_after_capture(
    tmp_path,
    field_name,
    drifted_value,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119AbsentExactHistoryClient(),
    )
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        setattr(backup, field_name, drifted_value)
        session.commit()

    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )

    assert plan.status == "refused"
    assert plan.reason_code == "durable_snapshot_scope_mismatch"


def test_batch119_resume_rejects_durable_scope_drift_after_exact_capture(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119AbsentExactHistoryClient(),
    )
    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )
    assert plan.status == "ready"
    assert plan.position.disposition == "position_absent"
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.order_id = "safe-backup-after-exact-resume-capture"
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


@pytest.mark.parametrize(
    ("field_name", "drifted_value"),
    [
        ("trigger_price", "1"),
        ("size_text", "1"),
        ("evidence_json", '{"drift":"after-capture"}'),
        ("evidence_source", "drifted_after_exact_capture"),
    ],
)
def test_batch119_resume_rejects_protection_evidence_drift_after_capture(
    tmp_path,
    field_name,
    drifted_value,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119AbsentExactHistoryClient(),
    )
    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )
    _apply_recovery(module,
        factory,
        plan=plan,
        snapshot=snapshot,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        setattr(backup, field_name, drifted_value)
        session.commit()

    with pytest.raises(
        module.CompositeBatchRecoveryConflict,
        match="resume_source_state_conflict",
    ):
        _authorize_recovery(module,
            factory,
            expected_fingerprint=plan.evidence_fingerprint,
            snapshot=snapshot,
        )


def test_batch119_exact_history_page_limit_without_completion_is_incomplete(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    client = _Batch119ExactHistoryClient(
        exact_position_response={
            "data": [
                {
                    "posId": POS_ID,
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "state": "closed",
                    "row": index,
                }
                for index in range(100)
            ]
        }
    )

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert "snapshot_page_limit_ambiguous" in snapshot.errors.values()
    assert client.instrument_wide_history_calls == 0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("status", "pending"),
        ("purpose", "unsupported_stop"),
        ("strategy_instance_id", "deepcoin:wrong-owner"),
    ],
)
def test_batch119_exact_history_scope_rejects_invalid_ledger_before_network(
    tmp_path,
    field_name,
    value,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        setattr(row, field_name, value)
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0
    assert client.instrument_wide_history_calls == 0


def test_batch119_exact_history_scope_rejects_duplicate_role_before_network(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="sensitive-duplicate-stop",
                purpose="stop_loss",
                trigger_price="62000",
                size_text="38",
                status="verified",
                evidence_source="entry_protection_response",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0
    assert client.instrument_wide_history_calls == 0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("raw_message_id", 10531),
        ("target_lifecycle_id", 793),
    ],
)
def test_batch119_exact_history_scope_rejects_batch_pointer_drift_before_network(
    tmp_path,
    field_name,
    value,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        batch = session.get(StrategyManagementBatch, 119)
        setattr(batch, field_name, value)
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "incident_identity_mismatch"}
    assert client.network_calls == 0


def test_batch119_exact_history_scope_rejects_unsafe_order_id_before_network(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        row.order_id = "DC-ACCESS-KEY:private"
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


@pytest.mark.parametrize(
    "evidence_json",
    [
        "{",
        "[" * 1100 + "0" + "]" * 1100,
    ],
)
def test_batch119_exact_scope_rejects_invalid_evidence_json_before_network(
    tmp_path,
    evidence_json,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.evidence_json = evidence_json
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


@pytest.mark.parametrize("field_name", ["trigger_price", "size_text"])
def test_batch119_exact_scope_rejects_huge_decimal_before_network(
    tmp_path,
    field_name,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        setattr(backup, field_name, "1e1000000000")
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("trigger_price", None),
        ("trigger_price", "0"),
        ("trigger_price", "-0"),
        ("trigger_price", "-1"),
        ("size_text", None),
        ("size_text", "-1"),
    ],
)
@pytest.mark.parametrize("order_id", [PRIMARY_ORDER_ID, BACKUP_ORDER_ID])
def test_batch119_exact_scope_rejects_invalid_protection_economics_before_network(
    tmp_path,
    field_name,
    value,
    order_id,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=order_id)
            .one()
        )
        setattr(backup, field_name, value)
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("trigger_price", "NaN"),
        ("trigger_price", "Infinity"),
        ("size_text", "-Infinity"),
    ],
)
@pytest.mark.parametrize("order_id", [PRIMARY_ORDER_ID, BACKUP_ORDER_ID])
def test_batch119_exact_scope_rejects_nonfinite_economics_before_network(
    tmp_path,
    field_name,
    value,
    order_id,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=order_id)
            .one()
        )
        setattr(backup, field_name, value)
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


def test_batch119_exact_scope_decimal_markers_are_canonical_and_economic():
    module = _recovery_module()

    assert module._batch119_scope_decimal_marker(
        "1",
        value_kind="trigger_price",
    ) == module._batch119_scope_decimal_marker(
        "1.0",
        value_kind="trigger_price",
    )
    assert module._batch119_scope_decimal_marker(
        "1",
        value_kind="trigger_price",
    ) == module._batch119_scope_decimal_marker(
        "1e0",
        value_kind="trigger_price",
    )
    assert module._batch119_scope_decimal_marker(
        "0",
        value_kind="size",
    ) == {"kind": "decimal", "value": "0"}


def test_batch119_unverified_stop_is_not_skipped_for_unvalidated_recovery_event(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.status = "superseded"
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="replacement-backup-after-forged-audit",
                purpose="backup_stop",
                trigger_price="62000",
                size_text="19",
                status="verified",
                evidence_source="composite_management_replacement",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                action="composite_batch_false_state_repaired",
                status="resolved",
                notification_fingerprint="3" * 64,
                before_json="{}",
                after_json="{}",
                created_at=NOW,
            )
        )
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


def test_batch119_canonical_audit_replay_does_not_authorize_unverified_stop_skip(
    tmp_path,
):
    module = _recovery_module()
    donor_path = tmp_path / "donor"
    donor_path.mkdir()
    donor_factory, _, _, _, _ = _seed_batch_119_false_submission(donor_path)
    donor_plan = _plan(donor_factory)
    _apply_recovery(module,
        donor_factory,
        plan=donor_plan,
        expected_fingerprint=donor_plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    with donor_factory() as session:
        donor_event = session.query(ExecutionEvent).one()
        copied_audit = {
            "venue": donor_event.venue,
            "action": donor_event.action,
            "status": donor_event.status,
            "reason": donor_event.reason,
            "before_json": donor_event.before_json,
            "after_json": donor_event.after_json,
            "notification_fingerprint": donor_event.notification_fingerprint,
            "created_at": donor_event.created_at,
        }

    target_path = tmp_path / "target"
    target_path.mkdir()
    target_factory, _, binding_id, entry_id, _ = (
        _seed_batch_119_false_submission(target_path)
    )
    with target_factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.status = "superseded"
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="replacement-backup-after-canonical-audit-replay",
                purpose="backup_stop",
                trigger_price="62000",
                size_text="19",
                status="verified",
                evidence_source="composite_management_replacement",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                **copied_audit,
            )
        )
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        target_factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


@pytest.mark.parametrize(
    (
        "replacement_trigger",
        "replacement_size",
        "expected_errors",
        "expected_network_calls",
    ),
    [
        ("62000", "19", {}, 6),
        (
            None,
            "19",
            {"exact_scope": "exact_history_scope_invalid"},
            0,
        ),
        (
            "62000",
            "-1",
            {"exact_scope": "exact_history_scope_invalid"},
            0,
        ),
    ],
)
def test_batch119_canonical_audit_skips_superseded_stop_for_exact_after_state(
    tmp_path,
    replacement_trigger,
    replacement_size,
    expected_errors,
    expected_network_calls,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    replacement_order_id = "replacement-backup-after-matching-audit"
    replacement_at = NOW.replace(second=1)
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.status = "superseded"
        backup.updated_at = replacement_at
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=replacement_order_id,
                purpose="backup_stop",
                trigger_price=replacement_trigger,
                size_text=replacement_size,
                status="verified",
                evidence_source="composite_management_replacement",
                evidence_json="{}",
                first_seen_at=replacement_at,
                last_seen_at=replacement_at,
                last_verified_at=replacement_at,
                created_at=replacement_at,
                updated_at=replacement_at,
            )
        )
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == expected_errors
    assert client.network_calls == expected_network_calls
    if not expected_errors:
        assert set(client.exact_order_ids) == {
            PRIMARY_ORDER_ID,
            replacement_order_id,
        }


def test_batch119_canonical_audit_does_not_hide_original_stop_source_drift(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    replacement_at = NOW.replace(second=1)
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.status = "superseded"
        backup.trigger_price = "1"
        backup.size_text = "1"
        backup.evidence_json = '{"drift":"not-the-audited-source"}'
        backup.updated_at = replacement_at
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="replacement-backup-after-source-drift",
                purpose="backup_stop",
                trigger_price="62000",
                size_text="19",
                status="verified",
                evidence_source="composite_management_replacement",
                evidence_json="{}",
                first_seen_at=replacement_at,
                last_seen_at=replacement_at,
                last_verified_at=replacement_at,
                created_at=replacement_at,
                updated_at=replacement_at,
            )
        )
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


@pytest.mark.parametrize(
    ("field_name", "drifted_value"),
    [
        ("evidence_source", "forged_recovery_source"),
        ("venue", "forged-venue"),
        ("strategy_instance_id", "deepcoin:incident:other"),
    ],
)
def test_batch119_canonical_audit_does_not_hide_original_stop_identity_drift(
    tmp_path,
    field_name,
    drifted_value,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    replacement_at = NOW.replace(second=1)
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.status = "superseded"
        setattr(backup, field_name, drifted_value)
        backup.updated_at = replacement_at
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=f"replacement-backup-after-{field_name}-drift",
                purpose="backup_stop",
                trigger_price="62000",
                size_text="19",
                status="verified",
                evidence_source="composite_management_replacement",
                evidence_json="{}",
                first_seen_at=replacement_at,
                last_seen_at=replacement_at,
                last_verified_at=replacement_at,
                created_at=replacement_at,
                updated_at=replacement_at,
            )
        )
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


def test_batch119_invalid_audit_cannot_hide_stop_that_escaped_owner_query(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    replacement_at = NOW.replace(second=1)
    with factory() as session:
        escaped_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:escaped:btc:long",
            kol_id="escaped-source",
            chat_id=999,
            message_id=999,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="escaped-position",
            margin_mode="cross",
            position_mode="split",
            status="active",
        )
        session.add(escaped_binding)
        session.flush()
        escaped_entry = ExecutionOrderLeg(
            execution_binding_id=escaped_binding.id,
            strategy_instance_id=escaped_binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            order_id="escaped-entry-order",
            pos_id="escaped-position",
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
        )
        session.add(escaped_entry)
        session.flush()
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.status = "superseded"
        backup.execution_binding_id = escaped_binding.id
        backup.execution_order_leg_id = escaped_entry.id
        backup.pos_id = "escaped-position"
        backup.updated_at = replacement_at
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="replacement-after-owner-query-escape",
                purpose="backup_stop",
                trigger_price="62000",
                size_text="19",
                status="verified",
                evidence_source="composite_management_replacement",
                evidence_json="{}",
                first_seen_at=replacement_at,
                last_seen_at=replacement_at,
                last_verified_at=replacement_at,
                created_at=replacement_at,
                updated_at=replacement_at,
            )
        )
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


def test_batch119_canonical_audit_only_allows_superseded_original_stop(
    tmp_path,
):
    module = _recovery_module()
    factory, _, binding_id, entry_id, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    plan = _plan(factory)
    _apply_recovery(module,
        factory,
        plan=plan,
        expected_fingerprint=plan.evidence_fingerprint,
        authorization="I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        applied_at=NOW,
    )
    replacement_at = NOW.replace(second=1)
    with factory() as session:
        backup = (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=BACKUP_ORDER_ID)
            .one()
        )
        backup.status = "cancelled"
        backup.updated_at = replacement_at
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="replacement-after-cancelled-original",
                purpose="backup_stop",
                trigger_price="62000",
                size_text="19",
                status="verified",
                evidence_source="composite_management_replacement",
                evidence_json="{}",
                first_seen_at=replacement_at,
                last_seen_at=replacement_at,
                last_verified_at=replacement_at,
                created_at=replacement_at,
                updated_at=replacement_at,
            )
        )
        session.commit()
    client = _Batch119ExactHistoryClient()

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors == {"exact_scope": "exact_history_scope_invalid"}
    assert client.network_calls == 0


@pytest.mark.parametrize(
    "row",
    [
        {"instId": "BTC-USDT-SWAP", "posSide": "long", "state": "closed"},
        {"posId": POS_ID, "posSide": "long", "state": "closed"},
        {
            "posId": POS_ID,
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "state": "closed",
        },
        {
            "posId": POS_ID,
            "instId": "BTC-USDT-SWAP",
            "state": "closed",
        },
    ],
)
def test_batch119_exact_position_history_requires_complete_scope_identity(
    tmp_path,
    row,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    client = _Batch119ExactHistoryClient(
        exact_position_response={"data": [row]}
    )

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors["position_history"] == (
        "exact_scope_identity_mismatch"
    )


@pytest.mark.parametrize(
    "row",
    [
        {"instId": "BTC-USDT-SWAP", "posSide": "long", "state": "cancelled"},
        {"ordId": PRIMARY_ORDER_ID, "posSide": "long", "state": "cancelled"},
        {
            "ordId": PRIMARY_ORDER_ID,
            "instId": "ETH-USDT-SWAP",
            "posSide": "long",
            "state": "cancelled",
        },
        {
            "ordId": PRIMARY_ORDER_ID,
            "instId": "BTC-USDT-SWAP",
            "state": "cancelled",
        },
    ],
)
def test_batch119_exact_trigger_history_requires_complete_scope_identity(
    tmp_path,
    row,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    client = _Batch119ExactHistoryClient()
    original = client.read_trigger_order_history

    def read_trigger_order_history(*, inst_id, order_id, limit):
        if order_id == PRIMARY_ORDER_ID:
            return {"data": [row]}
        return original(inst_id=inst_id, order_id=order_id, limit=limit)

    client.read_trigger_order_history = read_trigger_order_history

    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=client,
    )

    assert snapshot.errors["trigger_history_stop_loss"] == (
        "exact_scope_identity_mismatch"
    )


def test_batch119_scope_and_account_authority_bind_exchange_fingerprint(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119ExactHistoryClient(
            exact_position_response={"data": []}
        ),
    )

    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )
    changed_scope = replace(snapshot, scope_fingerprint="7" * 64)
    changed_plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=changed_scope,
        planned_at=snapshot.capture_ended_at,
    )
    incomplete_authority = replace(
        snapshot.account_authority,
        complete=False,
        reason_code="snapshot_write_generation_changed",
    )
    incomplete_plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=replace(snapshot, account_authority=incomplete_authority),
        planned_at=snapshot.capture_ended_at,
    )

    assert plan.status == "ready"
    assert changed_plan.status == "refused"
    assert changed_plan.reason_code == "exchange_snapshot_incomplete"
    assert incomplete_plan.status == "refused"
    assert incomplete_plan.reason_code == "exchange_snapshot_incomplete"
    serialized = json.dumps(
        module.serialize_composite_batch_recovery_plan(plan),
        sort_keys=True,
    )
    assert POS_ID not in serialized
    assert PRIMARY_ORDER_ID not in serialized
    assert BACKUP_ORDER_ID not in serialized


def test_batch119_snapshot_collection_mutation_breaks_account_authority(
    tmp_path,
):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119ExactHistoryClient(
            exact_position_response={"data": []}
        ),
    )
    snapshot.positions[0]["pos"] = "19"

    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )

    assert plan.status == "refused"
    assert plan.reason_code == "exchange_snapshot_incomplete"


def test_batch119_snapshot_rejects_wrong_account_authority_endpoint(tmp_path):
    module = _recovery_module()
    factory, _, _, _, _ = _seed_batch_119_false_submission(tmp_path)
    snapshot = module.load_composite_batch_recovery_snapshot_read_only(
        factory,
        client=_Batch119ExactHistoryClient(
            exact_position_response={"data": []}
        ),
    )
    forged_collection = replace(
        snapshot.account_authority.collections[0],
        endpoint="forged_account_composite",
    )
    snapshot.account_authority = replace(
        snapshot.account_authority,
        collections=(forged_collection,),
    )

    plan = module.build_composite_batch_recovery_plan(
        factory,
        profile=module.BATCH_119_RECOVERY,
        snapshot=snapshot,
        planned_at=snapshot.capture_ended_at,
    )

    assert plan.status == "refused"
    assert plan.reason_code == "exchange_snapshot_incomplete"


def test_recovery_cli_position_absent_is_repeatable_without_exchange_writes(
    tmp_path,
    monkeypatch,
):
    from typer.testing import CliRunner

    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.cli import app
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, database_path, _, _, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    save_trading_settings(
        factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "composite_management_v2_mode": "live",
            "mimo_contract_mode": "v1",
        },
        updated_at=NOW,
    )
    specs_path = tmp_path / "deepcoin-contract-specs.yaml"
    specs_path.write_text(
        "contracts:\n"
        "  - instrument_id: BTC-USDT-SWAP\n"
        "    contract_value: 1\n"
        "    quantity_step: 1\n"
        "    min_quantity: 1\n"
        "    price_tick: 0.1\n",
        encoding="utf-8",
    )
    client = _Batch119AbsentExactHistoryClient()
    read_client_builds = []
    monkeypatch.setattr(
        cli_module,
        "_build_batch119_recovery_read_client",
        lambda: read_client_builds.append(True) or client,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: (_ for _ in ()).throw(
            AssertionError("position-absent must not build a writer client")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "execute_composite_management_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("position-absent must not enter the executor")
        ),
    )
    common = [
        "recover-composite-management-batch",
        "--database-path",
        str(database_path),
        "--batch-id",
        "119",
        "--deepcoin-contract-specs-path",
        str(specs_path),
    ]
    runner = CliRunner()
    dry_run = runner.invoke(app, common)

    assert dry_run.exit_code == 0, dry_run.stdout
    dry_payload = json.loads(dry_run.stdout)
    assert dry_payload["plan"]["position"]["disposition"] == "position_absent"
    serialized = json.dumps(dry_payload, sort_keys=True)
    assert POS_ID not in serialized
    assert PRIMARY_ORDER_ID not in serialized
    assert BACKUP_ORDER_ID not in serialized
    assert "wide-" not in serialized
    fingerprint = dry_payload["plan"]["evidence_fingerprint"]
    apply_args = [
        *common,
        "--apply",
        "--expected-fingerprint",
        fingerprint,
        "--authorization",
        "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
    ]

    scope_drift_client = _Batch119AbsentExactHistoryClient()
    scope_drift_client.uid_scope_hash = "7" * 64
    writable_factory_calls = []
    original_writable_factory = cli_module.create_existing_session_factory
    monkeypatch.setattr(
        cli_module,
        "_build_batch119_recovery_read_client",
        lambda: scope_drift_client,
    )
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        lambda path: writable_factory_calls.append(path),
    )
    stale_scope = runner.invoke(app, apply_args)
    assert stale_scope.exit_code == 2
    assert json.loads(stale_scope.stdout)["reason_code"] in {
        "evidence_fingerprint_mismatch",
        "resume_evidence_invalid",
        "resume_snapshot_scope_conflict",
    }
    assert writable_factory_calls == []
    monkeypatch.setattr(
        cli_module,
        "_build_batch119_recovery_read_client",
        lambda: read_client_builds.append(True) or client,
    )
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        original_writable_factory,
    )

    applied = runner.invoke(app, apply_args)
    repeated = runner.invoke(app, apply_args)

    assert applied.exit_code == 0, applied.stdout
    assert repeated.exit_code == 0, repeated.stdout
    assert read_client_builds == [True, True, True]
    assert client.instrument_wide_history_calls == 0
    save_trading_settings(
        factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "composite_management_v2_mode": "live",
            "mimo_contract_mode": "v2_live_adapter",
        },
        updated_at=NOW + timedelta(seconds=1),
    )
    setting_changed = runner.invoke(app, apply_args)
    assert setting_changed.exit_code == 2
    assert json.loads(setting_changed.stdout)["reason_code"] == (
        "mimo_contract_mode_not_v1"
    )
    with factory() as session:
        event = (
            session.query(ExecutionEvent)
            .filter_by(action="composite_batch_false_state_repaired")
            .one()
        )
        assert json.loads(event.after_json)["original_owned_stop_refs"] == []


def test_recovery_cli_position_absent_apply_rejects_new_durable_close(
    tmp_path,
    monkeypatch,
):
    from typer.testing import CliRunner

    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.cli import app
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, database_path, binding_id, entry_id, _ = (
        _seed_batch_119_false_submission(tmp_path)
    )
    save_trading_settings(
        factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "composite_management_v2_mode": "live",
            "mimo_contract_mode": "v1",
        },
        updated_at=NOW,
    )
    specs_path = tmp_path / "deepcoin-contract-specs.yaml"
    specs_path.write_text("contracts: []\n", encoding="utf-8")
    client = _Batch119AbsentExactHistoryClient()
    monkeypatch.setattr(
        cli_module,
        "_build_batch119_recovery_read_client",
        lambda: client,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: (_ for _ in ()).throw(
            AssertionError("position-absent must not build a writer client")
        ),
    )
    common = [
        "recover-composite-management-batch",
        "--database-path",
        str(database_path),
        "--batch-id",
        "119",
        "--deepcoin-contract-specs-path",
        str(specs_path),
    ]
    dry_run = CliRunner().invoke(app, common)
    assert dry_run.exit_code == 0, dry_run.stdout
    fingerprint = json.loads(dry_run.stdout)["plan"]["evidence_fingerprint"]

    with factory() as session:
        session.add(
            PositionMutationIntent(
                idempotency_key="cli-late-natural-stop-close",
                operation="close_position",
                strategy_instance_id="deepcoin:incident:btc:long",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                pos_id=POS_ID,
                authority_fingerprint="a" * 64,
                request_fingerprint="r" * 64,
                status="confirmed",
                request_json='{"private":"request"}',
                response_json='{"private":"response"}',
                reserved_at=NOW,
                submitted_at=NOW,
                confirmed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    writable_factory_calls = []
    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        lambda path: writable_factory_calls.append(path),
    )

    applied = CliRunner().invoke(
        app,
        [
            *common,
            "--apply",
            "--expected-fingerprint",
            fingerprint,
            "--authorization",
            "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        ],
    )

    assert applied.exit_code == 2
    payload = json.loads(applied.stdout)
    assert payload["reason_code"] in {
        "durable_close_submission_evidence_present",
        "resume_audit_missing",
        "resume_evidence_invalid",
    }
    assert "private" not in applied.stdout
    assert writable_factory_calls == []
    with factory() as session:
        assert session.query(ExecutionEvent).count() == 0


def test_recovery_cli_position_absent_refuses_non_v1_without_database_write(
    tmp_path,
    monkeypatch,
):
    from typer.testing import CliRunner

    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.cli import app
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, database_path, _, _, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    save_trading_settings(
        factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "composite_management_v2_mode": "live",
            "mimo_contract_mode": "v2_live_adapter",
        },
        updated_at=NOW,
    )
    specs_path = tmp_path / "deepcoin-contract-specs.yaml"
    specs_path.write_text(
        "contracts:\n"
        "  - instrument_id: BTC-USDT-SWAP\n"
        "    contract_value: 1\n"
        "    quantity_step: 1\n"
        "    min_quantity: 1\n"
        "    price_tick: 0.1\n",
        encoding="utf-8",
    )
    client = _Batch119RecoveryClient()
    client.pending = []
    client.position_history = [_closed_position_history_row()]
    client.trigger_history = [_successful_stop_trigger_row()]
    client.list_positions = lambda *, inst_id=None: []
    monkeypatch.setattr(
        cli_module,
        "_build_batch119_recovery_read_client",
        lambda: client,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: (_ for _ in ()).throw(
            AssertionError("non-v1 must not build a writer client")
        ),
    )
    common = [
        "recover-composite-management-batch",
        "--database-path",
        str(database_path),
        "--batch-id",
        "119",
        "--deepcoin-contract-specs-path",
        str(specs_path),
    ]
    runner = CliRunner()
    dry_run = runner.invoke(app, common)

    assert dry_run.exit_code == 0, dry_run.stdout
    fingerprint = json.loads(dry_run.stdout)["plan"]["evidence_fingerprint"]
    before = database_path.read_bytes()

    applied = runner.invoke(
        app,
        [
            *common,
            "--apply",
            "--expected-fingerprint",
            fingerprint,
            "--authorization",
            "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        ],
    )

    assert applied.exit_code == 2
    assert json.loads(applied.stdout)["reason_code"] == (
        "mimo_contract_mode_not_v1"
    )
    assert database_path.read_bytes() == before
    assert client.cancel_calls == []
    assert client.close_calls == []
    assert client.set_calls == []
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_recovery_cli_position_absent_rechecks_mimo_v1_inside_apply_lock(
    tmp_path,
    monkeypatch,
):
    from typer.testing import CliRunner

    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.cli import app
    from telegram_kol_research.trading_settings import (
        load_trading_settings,
        save_trading_settings,
    )

    factory, database_path, _, _, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    v1_settings = {
        "auto_trade_enabled": True,
        "management_execution_mode": "live",
        "composite_management_v2_mode": "live",
        "mimo_contract_mode": "v1",
    }
    save_trading_settings(factory, v1_settings, updated_at=NOW)
    specs_path = tmp_path / "deepcoin-contract-specs.yaml"
    specs_path.write_text("contracts: []\n", encoding="utf-8")
    client = _Batch119AbsentExactHistoryClient()
    monkeypatch.setattr(
        cli_module,
        "_build_batch119_recovery_read_client",
        lambda: client,
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: (_ for _ in ()).throw(
            AssertionError("position-absent must not build writer")
        ),
    )
    common = [
        "recover-composite-management-batch",
        "--database-path",
        str(database_path),
        "--batch-id",
        "119",
        "--deepcoin-contract-specs-path",
        str(specs_path),
    ]
    dry_run = CliRunner().invoke(app, common)
    assert dry_run.exit_code == 0, dry_run.stdout
    fingerprint = json.loads(dry_run.stdout)["plan"]["evidence_fingerprint"]
    original_writable_factory = cli_module.create_existing_session_factory
    setting_switched = []

    def switch_setting_before_apply(path):
        save_trading_settings(
            factory,
            {**v1_settings, "mimo_contract_mode": "v2_live_adapter"},
            updated_at=NOW + timedelta(seconds=1),
        )
        setting_switched.append(True)
        return original_writable_factory(path)

    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        switch_setting_before_apply,
    )
    applied = CliRunner().invoke(
        app,
        [
            *common,
            "--apply",
            "--expected-fingerprint",
            fingerprint,
            "--authorization",
            "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        ],
    )

    assert setting_switched == [True]
    assert applied.exit_code == 2
    assert json.loads(applied.stdout)["reason_code"] == (
        "mimo_contract_mode_not_v1"
    )
    assert load_trading_settings(factory).mimo_contract_mode == "v2_live_adapter"
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


def test_recovery_cli_non_absent_builds_writer_only_after_locked_mimo_gate(
    tmp_path,
    monkeypatch,
):
    from typer.testing import CliRunner

    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.cli import app
    from telegram_kol_research.trading_settings import save_trading_settings

    factory, database_path, _, _, _ = _seed_batch_119_false_submission(
        tmp_path
    )
    v1_settings = {
        "auto_trade_enabled": True,
        "management_execution_mode": "live",
        "composite_management_v2_mode": "live",
        "mimo_contract_mode": "v1",
    }
    save_trading_settings(factory, v1_settings, updated_at=NOW)
    specs_path = tmp_path / "deepcoin-contract-specs.yaml"
    specs_path.write_text(
        "contracts:\n"
        "  - instrument_id: BTC-USDT-SWAP\n"
        "    contract_value: 1\n"
        "    quantity_step: 1\n"
        "    min_quantity: 1\n"
        "    price_tick: 0.1\n",
        encoding="utf-8",
    )
    client = _Batch119ExactHistoryClient(exact_position_response={"data": []})
    writer_calls = []
    monkeypatch.setattr(
        cli_module,
        "_build_batch119_recovery_read_client",
        lambda: client,
    )
    monkeypatch.setattr(
        cli_module,
        "build_deepcoin_client_from_env",
        lambda: writer_calls.append(True) or client,
    )
    common = [
        "recover-composite-management-batch",
        "--database-path",
        str(database_path),
        "--batch-id",
        "119",
        "--deepcoin-contract-specs-path",
        str(specs_path),
    ]
    dry_run = CliRunner().invoke(app, common)
    assert dry_run.exit_code == 0, dry_run.stdout
    fingerprint = json.loads(dry_run.stdout)["plan"]["evidence_fingerprint"]
    original_writable_factory = cli_module.create_existing_session_factory

    def switch_setting_before_apply(path):
        save_trading_settings(
            factory,
            {**v1_settings, "mimo_contract_mode": "v2_live_adapter"},
            updated_at=NOW + timedelta(seconds=1),
        )
        return original_writable_factory(path)

    monkeypatch.setattr(
        cli_module,
        "create_existing_session_factory",
        switch_setting_before_apply,
    )
    applied = CliRunner().invoke(
        app,
        [
            *common,
            "--apply",
            "--expected-fingerprint",
            fingerprint,
            "--authorization",
            "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        ],
    )

    assert applied.exit_code == 2
    assert json.loads(applied.stdout)["reason_code"] == (
        "mimo_contract_mode_not_v1"
    )
    assert writer_calls == []
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "reconciling"
        assert session.query(ExecutionEvent).count() == 0


@pytest.mark.parametrize("interrupt_close_readback", [False, True])
def test_recovery_cli_dry_run_then_apply_closes_exactly_once_and_preserves_settings(
    tmp_path,
    monkeypatch,
    interrupt_close_readback,
):
    from typer.testing import CliRunner

    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.cli import app
    from telegram_kol_research.trading_settings import (
        load_trading_settings,
        save_trading_settings,
    )

    factory, database_path, binding_id, entry_id, _ = (
        _seed_batch_119_false_submission(tmp_path)
    )
    with factory() as session:
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=entry_id,
                strategy_instance_id="deepcoin:incident:btc:long",
                pos_id=POS_ID,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="batch-119-first-tp",
                purpose="take_profit",
                trigger_price="66000",
                size_text="19",
                status="verified",
                evidence_source="entry_protection_response",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                last_verified_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    save_trading_settings(
        factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "composite_management_v2_mode": "live",
            "mimo_contract_mode": "v1",
        },
        updated_at=NOW,
    )
    settings_before = load_trading_settings(factory).to_dict()
    specs_path = tmp_path / "deepcoin-contract-specs.yaml"
    specs_path.write_text(
        "contracts:\n"
        "  - instrument_id: BTC-USDT-SWAP\n"
        "    contract_value: 1\n"
        "    quantity_step: 1\n"
        "    min_quantity: 1\n"
        "    price_tick: 0.1\n",
        encoding="utf-8",
    )
    client = _Batch119RecoveryClient(
        fail_close_readback_once=interrupt_close_readback
    )
    monkeypatch.setattr(
        cli_module, "build_deepcoin_client_from_env", lambda: client
    )
    monkeypatch.setattr(
        cli_module,
        "_build_batch119_recovery_read_client",
        lambda: client,
        raising=False,
    )
    common = [
        "recover-composite-management-batch",
        "--database-path",
        str(database_path),
        "--batch-id",
        "119",
        "--deepcoin-contract-specs-path",
        str(specs_path),
    ]
    before = database_path.read_bytes()

    dry_run = CliRunner().invoke(app, common)

    assert dry_run.exit_code == 0, dry_run.stdout
    assert database_path.read_bytes() == before
    dry_payload = json.loads(dry_run.stdout)
    fingerprint = dry_payload["plan"]["evidence_fingerprint"]
    assert dry_payload["plan"]["position"]["close_delta"] == "19"

    applied = CliRunner().invoke(
        app,
        [
            *common,
            "--apply",
            "--expected-fingerprint",
            fingerprint,
            "--authorization",
            "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        ],
    )

    assert applied.exit_code == 0, applied.stdout
    if interrupt_close_readback:
        interrupted_summary = json.loads(applied.stdout)["result"]
        assert interrupted_summary["batch_status"] == "executing"
        assert interrupted_summary["confirmed_close_intent_count"] == 0
        assert interrupted_summary["unresolved_mutation_intent_count"] == 1
        assert len(client.close_calls) == 1
        applied = CliRunner().invoke(
            app,
            [
                *common,
                "--apply",
                "--expected-fingerprint",
                fingerprint,
                "--authorization",
                "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
            ],
        )
        assert applied.exit_code == 0, applied.stdout
    applied_summary = json.loads(applied.stdout)["result"]
    with factory() as diagnostic_session:
        intent_states = [
            (row.operation, row.status, row.idempotency_key)
            for row in diagnostic_session.query(PositionMutationIntent)
            .order_by(PositionMutationIntent.id)
            .all()
        ]
    assert applied_summary["component_status_counts"] == {"confirmed": 3}
    assert applied_summary["component_mutation_intent_count"] == 6
    assert applied_summary["confirmed_close_intent_count"] == 1, intent_states
    assert applied_summary["unresolved_mutation_intent_count"] == 0
    assert applied_summary["recovery_audit_event_count"] == 1
    assert len(client.close_calls) == 1
    assert client.close_calls[0]["sz"] == "19"
    with factory() as session:
        assert session.get(StrategyManagementBatch, 119).status == "succeeded"
        assert (
            session.query(PositionMutationIntent)
            .filter_by(execution_binding_id=binding_id, operation="close_position")
            .count()
            == 1
        )
        assert (
            session.query(ExecutionEvent)
            .filter_by(action="composite_batch_false_state_repaired")
            .count()
            == 1
        )
    assert load_trading_settings(factory).to_dict() == settings_before

    old_stops = [
        {
            "ordId": PRIMARY_ORDER_ID,
            "posId": POS_ID,
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "triggerOrderType": "TPSL",
            "slTriggerPx": "64000",
            "sz": "38",
            "state": "live",
        },
        {
            "ordId": BACKUP_ORDER_ID,
            "posId": POS_ID,
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "triggerOrderType": "TPSL",
            "slTriggerPx": "63000",
            "sz": "38",
            "state": "live",
        },
    ]
    client.pending.extend(old_stops)
    unsafe_repeat = CliRunner().invoke(
        app,
        [
            *common,
            "--apply",
            "--expected-fingerprint",
            fingerprint,
            "--authorization",
            "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        ],
    )
    assert unsafe_repeat.exit_code == 2
    assert json.loads(unsafe_repeat.stdout)["reason_code"] == (
        "resume_evidence_invalid"
    )
    assert len(client.close_calls) == 1
    assert len(client.set_calls) == 2
    client.pending = [
        row for row in client.pending
        if row["ordId"] not in {PRIMARY_ORDER_ID, BACKUP_ORDER_ID}
    ]

    repeated = CliRunner().invoke(
        app,
        [
            *common,
            "--apply",
            "--expected-fingerprint",
            fingerprint,
            "--authorization",
            "I_AUTHORIZE_BATCH_119_TO_REMAINING_19",
        ],
    )

    assert repeated.exit_code == 2
    assert json.loads(repeated.stdout)["reason_code"] == (
        "resume_evidence_invalid"
    )
    assert len(client.close_calls) == 1

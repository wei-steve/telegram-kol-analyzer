"""Supervised terminalization for six exact historical management batches.

The planner is read-only.  It accepts normalized, previously captured exchange
evidence and never imports an exchange client.  Mutation helpers are added only
after their compare-and-set behavior is covered by focused tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


TARGET_BATCH_IDS = (123, 127, 129, 133, 144, 146)
REPAIR_REASON = "historical_position_fully_closed"
TERMINAL_LEG_REASON = "historical_exchange_position_closed"

_TARGETS: dict[int, dict[str, Any]] = {
    123: {
        "batch_reason": "management_reconciliation_identity_mismatch",
        "batch_fp": "d08672fd18fab476a9b7ed70d195d9ff2ccf27eb6d570a78c3b163ba58f6be7c",
        "contract_fp": "cbddc9b6dd2ec5fc26c49c5211ef0dcd73aaafdb2c1aea3faa796b10805b5192",
        "target_fp": "1b46a80258e1ccfea24451b1c59cb1724328b4563e9c303ff61651c5b03dbf90",
        "raw": 10696, "source_message": 4250,
        "source_text_fp": "23b53ed239358a6ff259d99fd8a072d3fbc427bbe2cad4f41e3b35e4ac56dc17",
        "decision": 10527, "lifecycle": 819,
        "binding": 283, "execution_leg": 497, "management_leg": 107,
        "components": ((4, "recovery_required"), (5, "pending"), (6, "pending")),
        "pos_id": "1001124765619311", "size": "2.3",
        "instrument": "ETH-USDT-SWAP", "side": "long",
        "execution_status": "filled", "management_status": "submitted",
    },
    127: {
        "batch_reason": "take_profit_cancel_retry_exhausted",
        "batch_fp": "265f3c080298324bf0e2e1277a1205c5ddba0979e841e09549a69c604f89f95b",
        "contract_fp": "5329927c8a9a8ce17a4e912d64bac83c6d53f7162382ce16d009e1ab2aec8624",
        "target_fp": "38df032b721c7b25585c8a6f23c384c4dd5ed1754ec2317bb14ca29fbbb80c04",
        "raw": 10747, "source_message": 10009,
        "source_text_fp": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "decision": 10578, "lifecycle": 816,
        "binding": 282, "execution_leg": 496, "management_leg": 110,
        "components": ((7, "operator_required"), (8, "pending"), (9, "pending")),
        "pos_id": "1001124765261315", "size": "12",
        "instrument": "BTC-USDT-SWAP", "side": "long",
        "execution_status": "filled", "management_status": "planned",
    },
    129: {
        "batch_reason": "take_profit_order_identity_conflict",
        "batch_fp": "4502bcd6b647a17724beb9c4ab150005a402435da0ea366bbf9c175f08744c71",
        "contract_fp": "117bf9461ae417610429f4f341660e7325ddd7294945ad0e206e46115e423c54",
        "target_fp": "90144990d71b1058815b0e9f116ee4c0f71fc0fd73e714a1456c4d10d7b1f640",
        "raw": 10839, "source_message": 4255,
        "source_text_fp": "23b53ed239358a6ff259d99fd8a072d3fbc427bbe2cad4f41e3b35e4ac56dc17",
        "decision": 10670, "lifecycle": 834,
        "binding": 287, "execution_leg": 503, "management_leg": 112,
        "components": ((10, "pending"), (11, "pending"), (12, "pending")),
        "pos_id": "1001124787260932", "size": "2.3",
        "instrument": "ETH-USDT-SWAP", "side": "short",
        "execution_status": "active", "management_status": "planned",
    },
    133: {
        "batch_reason": "management_reconciliation_identity_mismatch",
        "batch_fp": "d7ca719ce01bfb29923a2cdfdc7e08f086922386322480724768569c81d973c5",
        "contract_fp": "6680b2b5865195c909c086fc614da34e4eefdeae845fe6d738993145f80203e2",
        "target_fp": "9b397543737394df0be4a9bb682bb330b872d1781cd768ced430e9df32c6e59e",
        "raw": 11279, "source_message": 4275,
        "source_text_fp": "23b53ed239358a6ff259d99fd8a072d3fbc427bbe2cad4f41e3b35e4ac56dc17",
        "decision": 11110, "lifecycle": 859,
        "binding": 292, "execution_leg": 511, "management_leg": 117,
        "components": ((13, "recovery_required"), (14, "pending"), (15, "pending")),
        "pos_id": "1001124837556751", "size": "15",
        "instrument": "BTC-USDT-SWAP", "side": "long",
        "execution_status": "filled", "management_status": "submitted",
    },
    144: {
        "batch_reason": "management_reconciliation_identity_mismatch",
        "batch_fp": "d3caa0f8209c445f181dbf06725b9dd7f2e60a222297160baa98dd31eca45d25",
        "contract_fp": "e0b5a2a31c10792bb64d0654dfe055064e2d46477d1836dfdf710bc5454bf8bd",
        "target_fp": "54241e51585e92532c62d92e15251ec49aee809e0b3f839a7a43c5f921222fa5",
        "raw": 11892, "source_message": 4332,
        "source_text_fp": "c3d414b80c2f6c90b6539bbe51b5001083f045a846e85de705d23dd76e02cfa3",
        "decision": 11891, "lifecycle": 910,
        "binding": 307, "execution_leg": 530, "management_leg": 125,
        "components": ((16, "recovery_required"), (17, "pending"), (18, "pending")),
        "pos_id": "1001124898122909", "size": "8",
        "instrument": "BTC-USDT-SWAP", "side": "short",
        "execution_status": "filled", "management_status": "submitted",
    },
    146: {
        "batch_reason": "take_profit_cancel_retry_exhausted",
        "batch_fp": "c934ceffc43062fb4d63bd3d7210c2f15661121d60b51453973fa5dfb0f31d40",
        "contract_fp": "3e3ec28e10a34d6afbc487f0f6a463a4fd5d5aa2e39e1c4aafdbf6619c04d0e6",
        "target_fp": "40a60800f3e56adf425d3b14b6bf7d64cfaf6d5b758ae82a4aa34da6509f64b4",
        "raw": 12068, "source_message": 8823,
        "source_text_fp": "46607cc0eeafc4a7d9bb9f696594a005b0c5675ebeb9ce5db3ce0d69c079fe6d",
        "decision": 12066, "lifecycle": 921,
        "binding": 313, "execution_leg": 540, "management_leg": 127,
        "components": ((19, "operator_required"), (20, "pending"), (21, "pending")),
        "pos_id": "1001124908211764", "size": "2.2",
        "instrument": "ETH-USDT-SWAP", "side": "long",
        "execution_status": "filled", "management_status": "planned",
    },
}

_COUNT_TABLES = (
    "strategy_management_batches",
    "strategy_management_legs",
    "strategy_management_components",
    "execution_bindings",
    "execution_order_legs",
    "strategy_lifecycles",
    "position_mutation_intents",
    "execution_events",
    "raw_messages",
    "recognition_decisions",
    "position_protection_ledger",
    "position_protection_incidents",
)

_BASE_EVIDENCE_SHA256 = {
    "management-batches.json": "b4e12cc76570f9a9c9f7c35ac05f09109a1989571e820e5ba92fbd83d291f841",
    "protection-incidents.json": "2bbf89c66e41beeb706dd91fdff9ac2792c3994dc8285f393773277707e1a6c9",
    "six-batches-classification.json": "93e78ecb1759d2c50ebcb5ffa3fb8ad85d0694886df857b51247860d2cda19f1",
    "six-batches-exchange-chain.json": "c6dc1d61b205a27ee0f4e6a8cd325b8ba47f67f523fe837a14326a9fd84d0b62",
    "six-batches-local-chain.json": "e18a4f4791f9fb6c057232d848f1a0caf2083000526d2fd6a56c5cae54db6220",
    "tpsl-ownership.json": "b51b7c1c86b8ad88b23c3327071f788943d207c32162034176c3744864ed7d56",
}


class HistoricalManagementTerminalizationRefused(RuntimeError):
    """Raised before mutation when any exact safety assertion fails."""


@dataclass(frozen=True, slots=True)
class TerminalizationAction:
    table: str
    pk: int
    before: Mapping[str, Any]
    after: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TerminalizationPlan:
    schema_version: int
    mode: str
    database_path: str
    snapshot_method: str
    target_batch_ids: tuple[int, ...]
    code_sha: str
    repair_ts_utc: str
    database_fingerprint: str
    exchange_fingerprint: str
    action_fingerprint: str
    rollback_fingerprint: str
    plan_fingerprint: str
    confirmation_token: str
    exchange_write_count: int
    quick_check: str
    table_counts: Mapping[str, int]
    database_evidence: Mapping[str, Any]
    exchange_evidence: Mapping[str, Any]
    actions: tuple[TerminalizationAction, ...]

    @property
    def action_count(self) -> int:
        return len(self.actions)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action_count"] = self.action_count
        return payload


def build_terminalization_plan(
    database_path: str | Path,
    *,
    exchange_evidence: Mapping[str, Any],
    repair_ts: datetime,
    code_sha: str,
) -> TerminalizationPlan:
    """Build the exact 45-action plan without opening the database writable."""

    resolved = Path(database_path).expanduser().resolve()
    if not resolved.is_file():
        raise HistoricalManagementTerminalizationRefused("database_missing")
    if repair_ts.tzinfo is None:
        raise HistoricalManagementTerminalizationRefused("repair_timestamp_not_utc")
    repair_ts = repair_ts.astimezone(UTC)
    if len(code_sha) != 40 or any(ch not in "0123456789abcdef" for ch in code_sha):
        raise HistoricalManagementTerminalizationRefused("code_sha_invalid")
    exchange_payload = _validate_exchange_evidence(exchange_evidence)
    repair_db_value = _database_timestamp(repair_ts)

    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise HistoricalManagementTerminalizationRefused("query_only_unavailable")
        connection.execute("BEGIN")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise HistoricalManagementTerminalizationRefused("quick_check_failed")
        recovery_ids = tuple(
            int(row[0]) for row in connection.execute(
                "SELECT id FROM strategy_management_batches "
                "WHERE status='recovery_required' ORDER BY id"
            )
        )
        if recovery_ids != TARGET_BATCH_IDS:
            raise HistoricalManagementTerminalizationRefused("target_set_changed")
        unsafe_batches = connection.execute(
            "SELECT id FROM strategy_management_batches WHERE status IN "
            "('executing','reserved','submitted','submit_unknown','reconciling') "
            "ORDER BY id"
        ).fetchall()
        if unsafe_batches:
            raise HistoricalManagementTerminalizationRefused("management_window_not_quiet")
        table_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _COUNT_TABLES
        }
        actions, database_evidence = _build_actions(
            connection, repair_db_value=repair_db_value
        )
        if len(actions) != 45:
            raise HistoricalManagementTerminalizationRefused("action_count_changed")
        connection.rollback()
    finally:
        connection.close()

    database_fingerprint = _fingerprint(database_evidence)
    exchange_fingerprint = _fingerprint(exchange_payload)
    action_payload = [_action_payload(action) for action in actions]
    action_fingerprint = _fingerprint(action_payload)
    rollback_payload = [_reverse_action_payload(action) for action in reversed(actions)]
    rollback_fingerprint = _fingerprint(rollback_payload)
    plan_payload = {
        "schema_version": 1,
        "code_sha": code_sha,
        "repair_ts_utc": repair_ts.isoformat(),
        "database_fingerprint": database_fingerprint,
        "exchange_fingerprint": exchange_fingerprint,
        "action_fingerprint": action_fingerprint,
        "rollback_fingerprint": rollback_fingerprint,
        "action_count": len(actions),
    }
    plan_fingerprint = _fingerprint(plan_payload)
    confirmation_token = hashlib.sha256(
        f"historical-management-terminalization:{plan_fingerprint}".encode()
    ).hexdigest()[:16]
    return TerminalizationPlan(
        schema_version=1,
        mode="dry_run",
        database_path=str(resolved),
        snapshot_method="sqlite_mode_ro_query_only_transaction",
        target_batch_ids=TARGET_BATCH_IDS,
        code_sha=code_sha,
        repair_ts_utc=repair_ts.isoformat(),
        database_fingerprint=database_fingerprint,
        exchange_fingerprint=exchange_fingerprint,
        action_fingerprint=action_fingerprint,
        rollback_fingerprint=rollback_fingerprint,
        plan_fingerprint=plan_fingerprint,
        confirmation_token=confirmation_token,
        exchange_write_count=0,
        quick_check=quick_check,
        table_counts=table_counts,
        database_evidence=database_evidence,
        exchange_evidence=exchange_payload,
        actions=tuple(actions),
    )


def load_exchange_evidence_directory(
    directory: str | Path,
    *,
    expected_sibling_sha256: str,
    expected_base_hashes: Mapping[str, str] = _BASE_EVIDENCE_SHA256,
) -> dict[str, Any]:
    """Normalize the exact private evidence bundle without exchange access."""

    root = Path(directory).expanduser().resolve()
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise HistoricalManagementTerminalizationRefused("evidence_directory_not_private")
    expected_hashes = dict(expected_base_hashes)
    expected_hashes["batch-144-live-sibling.json"] = expected_sibling_sha256
    documents: dict[str, Any] = {}
    actual_hashes: dict[str, str] = {}
    for name, expected_hash in expected_hashes.items():
        if len(expected_hash) != 64:
            raise HistoricalManagementTerminalizationRefused("evidence_hash_invalid")
        path = root / name
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise HistoricalManagementTerminalizationRefused("evidence_file_not_private")
        content = path.read_bytes()
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != expected_hash:
            raise HistoricalManagementTerminalizationRefused("evidence_hash_mismatch")
        try:
            documents[name] = json.loads(content)
        except json.JSONDecodeError as exc:
            raise HistoricalManagementTerminalizationRefused("evidence_json_invalid") from exc
        actual_hashes[name] = actual_hash

    management = documents["management-batches.json"]
    if not isinstance(management, dict):
        raise HistoricalManagementTerminalizationRefused("management_snapshot_incomplete")
    _require_values(
        management,
        {
            "output_complete": True,
            "batches_returned": 146,
            "batches_truncated": False,
            "malformed_field_count": 0,
            "malformed_row_count": 0,
            "snapshot_status": "stable",
            "snapshot_validation": "ok",
            "all_history_legs_complete": True,
        },
        "management_snapshot_incomplete",
    )
    if management.get("counts", {}).get("recovery_required") != 6:
        raise HistoricalManagementTerminalizationRefused("management_target_count_changed")

    protection = documents["protection-incidents.json"]
    if (
        not isinstance(protection, dict)
        or protection.get("exchange_snapshot_complete") is not True
        or protection.get("counts", {}).get("current_risk") != 0
        or protection.get("snapshot_validation") != "ok"
    ):
        raise HistoricalManagementTerminalizationRefused("protection_snapshot_incomplete")

    ownership = documents["tpsl-ownership.json"]
    if (
        not isinstance(ownership, dict)
        or ownership.get("exchange_write_count") != 0
        or ownership.get("conflicts")
        or ownership.get("unowned_pending_order_ids")
        or ownership.get("owned_pending_count") != ownership.get("pending_tpsl_count")
    ):
        raise HistoricalManagementTerminalizationRefused("tpsl_ownership_conflict")

    local_chain = documents["six-batches-local-chain.json"]
    if (
        not isinstance(local_chain, list)
        or {row.get("batch", {}).get("id") for row in local_chain} != set(TARGET_BATCH_IDS)
    ):
        raise HistoricalManagementTerminalizationRefused("local_chain_target_set_changed")

    classification = documents["six-batches-classification.json"]
    if (
        not isinstance(classification, dict)
        or classification.get("exchange_snapshot_errors")
        or classification.get("position_history_errors")
    ):
        raise HistoricalManagementTerminalizationRefused("exchange_snapshot_incomplete")
    classification_rows = classification.get("batches")
    if not isinstance(classification_rows, list):
        raise HistoricalManagementTerminalizationRefused("exchange_classification_changed")
    by_batch = {row.get("batch_id"): row for row in classification_rows}
    if set(by_batch) != set(TARGET_BATCH_IDS) or len(classification_rows) != 6:
        raise HistoricalManagementTerminalizationRefused("exchange_target_set_changed")

    exchange_chain = documents["six-batches-exchange-chain.json"]
    if (
        not isinstance(exchange_chain, dict)
        or exchange_chain.get("snapshot_errors")
        or exchange_chain.get("position_history_errors")
    ):
        raise HistoricalManagementTerminalizationRefused("exchange_snapshot_incomplete")
    exchange_batches = exchange_chain.get("batches")
    if not isinstance(exchange_batches, dict) or set(exchange_batches) != {
        str(value) for value in TARGET_BATCH_IDS
    }:
        raise HistoricalManagementTerminalizationRefused("exchange_target_set_changed")

    normalized_batches: dict[str, Any] = {}
    for batch_id, target in _TARGETS.items():
        classified = by_batch[batch_id]
        local = classified.get("local", {})
        exchange_summary = classified.get("exchange", {})
        _require_values(
            local,
            {
                "batch_status": "recovery_required",
                "lifecycle_id": target["lifecycle"],
                "binding_id": target["binding"],
                "execution_leg_id": target["execution_leg"],
                "attribution_status": "verified",
                "pos_id": target["pos_id"],
                "unique_execution_leg_owner_count": 1,
                "unique_binding_owner_count": 1,
            },
            "local_chain_identity_changed",
        )
        if any(
            row.get("status") != "confirmed"
            for row in local.get("mutation_intent_states", [])
        ):
            raise HistoricalManagementTerminalizationRefused("mutation_intent_unconfirmed")
        _require_values(
            exchange_summary,
            {
                "live_position_count": 0,
                "open_order_count": 0,
                "pending_tpsl_count": 0,
                "position_history_count": 1,
            },
            "target_position_not_terminal",
        )
        chain = exchange_batches[str(batch_id)]
        chain_target = chain.get("target", {})
        _require_values(
            chain_target,
            {
                "id": batch_id,
                "instrument": target["instrument"],
                "binding_pos_id": target["pos_id"],
                "execution_order_leg_id": target["execution_leg"],
                "leg_pos_id": target["pos_id"],
            },
            "exchange_identity_changed",
        )
        matched = chain.get("matched", {})
        normalized_batches[str(batch_id)] = {
            "classification": classified.get("classification"),
            "position_history": chain.get("position_history"),
            "positions": matched.get("positions"),
            "open_orders": matched.get("open_orders"),
            "pending_trigger_orders": matched.get("pending_trigger_orders"),
            "position_history_error": chain.get("position_history_error"),
        }

    sibling = documents["batch-144-live-sibling.json"]
    normalized = {
        "snapshot_complete": True,
        "snapshot_errors": {},
        "exchange_write_count": 0,
        "tpsl_conflicts": ownership.get("conflicts"),
        "unowned_pending_order_ids": ownership.get("unowned_pending_order_ids"),
        "evidence_file_sha256": actual_hashes,
        "batches": normalized_batches,
        "sibling": sibling,
    }
    return _validate_exchange_evidence(normalized)


@dataclass(frozen=True, slots=True)
class TerminalizationMutationResult:
    mode: str
    status: str
    changed_row_count: int
    quick_check: str
    table_counts_before: Mapping[str, int]
    table_counts_after: Mapping[str, int]


def apply_terminalization_plan(
    database_path: str | Path,
    *,
    plan: TerminalizationPlan,
    expected_plan_fingerprint: str,
    expected_action_count: int,
    expected_repair_ts_utc: str,
    confirmation_token: str,
) -> TerminalizationMutationResult:
    """Apply one exact plan with all-row compare-and-set guards.

    This function has no exchange dependency.  Callers must supply the exact
    dry-run authorization values; a mismatch is rejected before a writable
    connection is opened.
    """

    _validate_plan_integrity(plan)
    if expected_plan_fingerprint != plan.plan_fingerprint:
        raise HistoricalManagementTerminalizationRefused("plan_fingerprint_mismatch")
    if expected_action_count != 45 or expected_action_count != plan.action_count:
        raise HistoricalManagementTerminalizationRefused("action_count_mismatch")
    if expected_repair_ts_utc != plan.repair_ts_utc:
        raise HistoricalManagementTerminalizationRefused("repair_timestamp_mismatch")
    if confirmation_token != plan.confirmation_token:
        raise HistoricalManagementTerminalizationRefused("confirmation_token_mismatch")
    return _mutate_plan(database_path, plan=plan, reverse=False)


def rollback_terminalization_plan(
    database_path: str | Path,
    *,
    plan: TerminalizationPlan,
    expected_rollback_fingerprint: str,
    expected_action_count: int,
    confirmation_token: str,
) -> TerminalizationMutationResult:
    """Reverse an exact plan only when every target row equals its new value."""

    _validate_plan_integrity(plan)
    if expected_rollback_fingerprint != plan.rollback_fingerprint:
        raise HistoricalManagementTerminalizationRefused("rollback_fingerprint_mismatch")
    if expected_action_count != 45 or expected_action_count != plan.action_count:
        raise HistoricalManagementTerminalizationRefused("action_count_mismatch")
    if confirmation_token != plan.confirmation_token:
        raise HistoricalManagementTerminalizationRefused("confirmation_token_mismatch")
    return _mutate_plan(database_path, plan=plan, reverse=True)


def write_terminalization_plan(
    path: str | Path, plan: TerminalizationPlan
) -> None:
    """Write canonical, credential-free plan JSON as a private file."""

    _validate_plan_integrity(plan)
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_canonical_json(plan.to_dict()))
            stream.write("\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(resolved, 0o600)


def load_terminalization_plan(path: str | Path) -> TerminalizationPlan:
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.pop("action_count", None) != 45:
        raise HistoricalManagementTerminalizationRefused("plan_json_invalid")
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list):
        raise HistoricalManagementTerminalizationRefused("plan_json_invalid")
    try:
        payload["actions"] = tuple(
            TerminalizationAction(
                table=item["table"], pk=item["pk"],
                before=item["before"], after=item["after"],
            )
            for item in raw_actions
        )
        payload["target_batch_ids"] = tuple(payload["target_batch_ids"])
        plan = TerminalizationPlan(**payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalManagementTerminalizationRefused("plan_json_invalid") from exc
    _validate_plan_integrity(plan)
    return plan


def render_rollback_sql(plan: TerminalizationPlan) -> str:
    """Render the reverse CAS transaction for review and emergency use."""

    _validate_plan_integrity(plan)
    lines = [
        "BEGIN IMMEDIATE;",
        f"-- rollback_fingerprint: {plan.rollback_fingerprint}",
        f"-- exact_action_count: {plan.action_count}",
        "CREATE TEMP TABLE _historical_repair_cas_guard (value INTEGER CHECK(value=1));",
    ]
    for action in reversed(plan.actions):
        before = action.after
        after = action.before
        changed_columns = [
            column
            for column in before
            if column != "id" and before[column] != after[column]
        ]
        assignments = ", ".join(
            f"{column}={_sql_literal(after[column])}" for column in changed_columns
        )
        predicates = " AND ".join(
            f"{column} IS {_sql_literal(value)}" for column, value in before.items()
        )
        lines.append(f"UPDATE {action.table} SET {assignments} WHERE {predicates};")
        lines.append("INSERT INTO _historical_repair_cas_guard VALUES(changes());")
        lines.append("DELETE FROM _historical_repair_cas_guard;")
    lines.append("DROP TABLE _historical_repair_cas_guard;")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _mutate_plan(
    database_path: str | Path,
    *,
    plan: TerminalizationPlan,
    reverse: bool,
) -> TerminalizationMutationResult:
    resolved = Path(database_path).expanduser().resolve()
    if not resolved.is_file():
        raise HistoricalManagementTerminalizationRefused("database_missing")
    if str(resolved) != plan.database_path:
        raise HistoricalManagementTerminalizationRefused("database_path_mismatch")
    connection = sqlite3.connect(resolved)
    connection.row_factory = sqlite3.Row
    changed = 0
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise HistoricalManagementTerminalizationRefused("quick_check_failed")
        counts_before = _table_counts(connection)
        if counts_before != dict(plan.table_counts):
            raise HistoricalManagementTerminalizationRefused("table_counts_changed")
        _validate_invariant_rows(connection, plan)

        ordered = tuple(reversed(plan.actions)) if reverse else plan.actions
        starts = [action.after if reverse else action.before for action in ordered]
        ends = [action.before if reverse else action.after for action in ordered]
        states = [
            _read_action_row(connection, action, columns=tuple(start))
            for action, start in zip(ordered, starts, strict=True)
        ]
        all_start = all(actual == dict(start) for actual, start in zip(states, starts, strict=True))
        all_end = all(actual == dict(end) for actual, end in zip(states, ends, strict=True))
        recovery_ids = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM strategy_management_batches "
                "WHERE status='recovery_required' ORDER BY id"
            )
        )
        unsafe_ids = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM strategy_management_batches WHERE status IN "
                "('executing','reserved','submitted','submit_unknown','reconciling') "
                "ORDER BY id"
            )
        )
        expected_start_recovery = () if reverse else TARGET_BATCH_IDS
        expected_end_recovery = TARGET_BATCH_IDS if reverse else ()
        if unsafe_ids:
            raise HistoricalManagementTerminalizationRefused("management_window_not_quiet")
        if all_end:
            if recovery_ids != expected_end_recovery:
                raise HistoricalManagementTerminalizationRefused("target_set_changed")
            connection.rollback()
            return TerminalizationMutationResult(
                mode="rollback" if reverse else "apply",
                status="already_rolled_back" if reverse else "already_applied",
                changed_row_count=0,
                quick_check=quick_check,
                table_counts_before=counts_before,
                table_counts_after=counts_before,
            )
        if not all_start:
            raise HistoricalManagementTerminalizationRefused("database_state_mixed")
        if recovery_ids != expected_start_recovery:
            raise HistoricalManagementTerminalizationRefused("target_set_changed")

        for action, start, end in zip(ordered, starts, ends, strict=True):
            _cas_update(connection, action=action, before=start, after=end)
            changed += 1
        for action, end in zip(ordered, ends, strict=True):
            if _read_action_row(connection, action, columns=tuple(end)) != dict(end):
                raise HistoricalManagementTerminalizationRefused("postcondition_failed")
        post_recovery_ids = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM strategy_management_batches "
                "WHERE status='recovery_required' ORDER BY id"
            )
        )
        if post_recovery_ids != expected_end_recovery:
            raise HistoricalManagementTerminalizationRefused("postcondition_failed")
        _validate_invariant_rows(connection, plan)
        counts_after = _table_counts(connection)
        if counts_after != counts_before:
            raise HistoricalManagementTerminalizationRefused("table_counts_changed")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise HistoricalManagementTerminalizationRefused("quick_check_failed")
        connection.commit()
        return TerminalizationMutationResult(
            mode="rollback" if reverse else "apply",
            status="rolled_back" if reverse else "applied",
            changed_row_count=changed,
            quick_check=quick_check,
            table_counts_before=counts_before,
            table_counts_after=counts_after,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _build_actions(
    connection: sqlite3.Connection, *, repair_db_value: str
) -> tuple[list[TerminalizationAction], dict[str, Any]]:
    rows_by_kind: dict[str, list[dict[str, Any]]] = {
        "batches": [], "management_legs": [], "components": [],
        "execution_legs": [], "bindings": [], "lifecycles": [],
        "mutation_intents": [],
        "source_messages": [],
    }
    actions: list[TerminalizationAction] = []
    target_binding_ids = tuple(target["binding"] for target in _TARGETS.values())

    for batch_id, target in _TARGETS.items():
        batch = _one(
            connection,
            "SELECT * FROM strategy_management_batches WHERE id=?",
            batch_id,
            "batch_missing",
        )
        expected_batch = {
            "status": "recovery_required",
            "reason_code": target["batch_reason"],
            "reconciled_at": None,
            "completed_at": None,
            "idempotency_fingerprint": target["batch_fp"],
            "management_contract_fingerprint": target["contract_fp"],
            "target_fingerprint": target["target_fp"],
            "target_lifecycle_id": target["lifecycle"],
            "execution_binding_id": target["binding"],
            "raw_message_id": target["raw"],
            "recognition_decision_id": target["decision"],
        }
        _require_values(batch, expected_batch, "batch_changed")
        rows_by_kind["batches"].append(batch)

        source_message = _one(
            connection,
            "SELECT id,message_id,text FROM raw_messages WHERE id=?",
            target["raw"],
            "source_message_missing",
        )
        source_evidence = {
            "id": source_message["id"],
            "message_id": source_message["message_id"],
            "text_sha256": hashlib.sha256(
                str(source_message["text"] or "").encode("utf-8")
            ).hexdigest(),
        }
        _require_values(
            source_evidence,
            {
                "message_id": target["source_message"],
                "text_sha256": target["source_text_fp"],
            },
            "source_message_changed",
        )
        rows_by_kind["source_messages"].append(source_evidence)

        management_leg = _one(
            connection,
            "SELECT * FROM strategy_management_legs WHERE id=?",
            target["management_leg"],
            "management_leg_missing",
        )
        _require_values(
            management_leg,
            {
                "management_batch_id": batch_id,
                "execution_order_leg_id": target["execution_leg"],
                "pos_id": target["pos_id"],
                "status": target["management_status"],
            },
            "management_leg_changed",
        )
        rows_by_kind["management_legs"].append(management_leg)

        for component_id, expected_status in target["components"]:
            component = _one(
                connection,
                "SELECT * FROM strategy_management_components WHERE id=?",
                component_id,
                "component_missing",
            )
            _require_values(
                component,
                {
                    "management_batch_id": batch_id,
                    "strategy_management_leg_id": target["management_leg"],
                    "status": expected_status,
                },
                "component_changed",
            )
            rows_by_kind["components"].append(component)

        execution_leg = _one(
            connection,
            "SELECT * FROM execution_order_legs WHERE id=?",
            target["execution_leg"],
            "execution_leg_missing",
        )
        _require_values(
            execution_leg,
            {
                "execution_binding_id": target["binding"],
                "strategy_instance_id": batch["strategy_instance_id"],
                "purpose": "entry",
                "status": target["execution_status"],
                "terminal_reason": None,
                "attribution_status": "verified",
                "pos_id": target["pos_id"],
            },
            "execution_leg_changed",
        )
        rows_by_kind["execution_legs"].append(execution_leg)
        exact_owner_rows = connection.execute(
            "SELECT id,execution_binding_id FROM execution_order_legs "
            "WHERE pos_id=? AND attribution_status='verified' ORDER BY id",
            (target["pos_id"],),
        ).fetchall()
        if [(row["id"], row["execution_binding_id"]) for row in exact_owner_rows] != [
            (target["execution_leg"], target["binding"])
        ]:
            raise HistoricalManagementTerminalizationRefused("position_owner_not_unique")

        binding = _one(
            connection,
            "SELECT * FROM execution_bindings WHERE id=?",
            target["binding"],
            "binding_missing",
        )
        _require_values(
            binding,
            {
                "strategy_instance_id": batch["strategy_instance_id"],
                "status": "active",
                "pos_id": target["pos_id"],
                "last_exchange_status": "position_attribution_evidence_unavailable",
            },
            "binding_changed",
        )
        rows_by_kind["bindings"].append(binding)

        lifecycle = _one(
            connection,
            "SELECT * FROM strategy_lifecycles WHERE id=?",
            target["lifecycle"],
            "lifecycle_missing",
        )
        _require_values(
            lifecycle,
            {
                "execution_binding_id": target["binding"],
                "lifecycle_status": "entered",
                "exit_reason": None,
                "exited_at": None,
                "trade_idea_id": None,
            },
            "lifecycle_changed",
        )
        rows_by_kind["lifecycles"].append(lifecycle)

    sibling = _one(
        connection,
        "SELECT * FROM execution_order_legs WHERE id=531",
        None,
        "batch_144_sibling_missing",
        parameterized=False,
    )
    _require_values(
        sibling,
        {
            "execution_binding_id": 307,
            "strategy_instance_id": rows_by_kind["batches"][4]["strategy_instance_id"],
            "purpose": "entry",
            "status": "active",
            "terminal_reason": None,
            "attribution_status": "verified",
            "pos_id": "1001124899621086",
        },
        "batch_144_sibling_changed",
    )
    rows_by_kind["execution_legs"].append({"batch_144_sibling_unchanged": sibling})
    sibling_owner_rows = connection.execute(
        "SELECT id,execution_binding_id FROM execution_order_legs "
        "WHERE pos_id='1001124899621086' AND attribution_status='verified' "
        "AND status='active' ORDER BY id"
    ).fetchall()
    if [(row["id"], row["execution_binding_id"]) for row in sibling_owner_rows] != [
        (531, 307)
    ]:
        raise HistoricalManagementTerminalizationRefused("batch_144_sibling_not_unique")

    placeholders = ",".join("?" for _ in target_binding_ids)
    mutation_rows = [
        dict(row) for row in connection.execute(
            "SELECT * FROM position_mutation_intents "
            f"WHERE execution_binding_id IN ({placeholders}) "
            "ORDER BY id",
            target_binding_ids,
        )
    ]
    if not mutation_rows or any(row["status"] != "confirmed" for row in mutation_rows):
        raise HistoricalManagementTerminalizationRefused("mutation_intent_unconfirmed")
    rows_by_kind["mutation_intents"] = mutation_rows

    for row in rows_by_kind["components"]:
        if row["status"] == "operator_required":
            continue
        after = dict(row)
        after.update(
            status="safely_skipped",
            reason_code=REPAIR_REASON,
            last_progress_at=repair_db_value,
            completed_at=repair_db_value,
            updated_at=repair_db_value,
        )
        actions.append(TerminalizationAction("strategy_management_components", row["id"], row, after))
    for row in rows_by_kind["management_legs"]:
        after = dict(row)
        after.update(status="failed", updated_at=repair_db_value)
        actions.append(TerminalizationAction("strategy_management_legs", row["id"], row, after))
    for row in rows_by_kind["batches"]:
        after = dict(row)
        after.update(
            status="resolved", reason_code=REPAIR_REASON,
            reconciled_at=repair_db_value, completed_at=repair_db_value,
            updated_at=repair_db_value,
        )
        actions.append(TerminalizationAction("strategy_management_batches", row["id"], row, after))
    for row in rows_by_kind["execution_legs"]:
        if "batch_144_sibling_unchanged" in row:
            continue
        after = dict(row)
        after.update(
            status="closed", terminal_reason=TERMINAL_LEG_REASON,
            last_verified_at=repair_db_value, updated_at=repair_db_value,
        )
        actions.append(TerminalizationAction("execution_order_legs", row["id"], row, after))
    for row in rows_by_kind["bindings"]:
        after = dict(row)
        if row["id"] == 307:
            after.update(
                status="active", pos_id="1001124899621086",
                last_exchange_status="position_ownership_verified",
                recovered_at=repair_db_value, updated_at=repair_db_value,
            )
        else:
            after.update(
                status="closed", pos_id=None,
                last_exchange_status="historical_cleanup_terminal",
                recovered_at=repair_db_value, updated_at=repair_db_value,
            )
        actions.append(TerminalizationAction("execution_bindings", row["id"], row, after))
    for row in rows_by_kind["lifecycles"]:
        if row["id"] == 910:
            continue
        after = dict(row)
        after.update(
            lifecycle_status="exited", exit_reason="exchange_closed",
            exited_at=repair_db_value, updated_at=repair_db_value,
        )
        actions.append(TerminalizationAction("strategy_lifecycles", row["id"], row, after))
    return actions, rows_by_kind


def _validate_exchange_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    payload = json.loads(_canonical_json(evidence))
    if payload.get("snapshot_complete") is not True or payload.get("snapshot_errors"):
        raise HistoricalManagementTerminalizationRefused("exchange_snapshot_incomplete")
    if payload.get("exchange_write_count") != 0:
        raise HistoricalManagementTerminalizationRefused("exchange_write_count_nonzero")
    if payload.get("tpsl_conflicts") or payload.get("unowned_pending_order_ids"):
        raise HistoricalManagementTerminalizationRefused("tpsl_ownership_conflict")
    batches = payload.get("batches")
    if not isinstance(batches, dict) or set(batches) != {str(v) for v in TARGET_BATCH_IDS}:
        raise HistoricalManagementTerminalizationRefused("exchange_target_set_changed")
    for batch_id, target in _TARGETS.items():
        row = batches[str(batch_id)]
        if not isinstance(row, dict) or row.get("classification") != "historical_terminal/informational":
            raise HistoricalManagementTerminalizationRefused("exchange_classification_changed")
        if row.get("position_history_error") is not None:
            raise HistoricalManagementTerminalizationRefused("position_history_incomplete")
        if row.get("positions") or row.get("open_orders") or row.get("pending_trigger_orders"):
            raise HistoricalManagementTerminalizationRefused("target_position_not_terminal")
        history = row.get("position_history")
        if not isinstance(history, list) or len(history) != 1:
            raise HistoricalManagementTerminalizationRefused("position_history_not_unique")
        history_row = history[0]
        if (
            not isinstance(history_row, dict)
            or str(history_row.get("posId") or "") != target["pos_id"]
            or history_row.get("instId") != target["instrument"]
            or history_row.get("posSide") != target["side"]
            or not _same_positive_number(history_row.get("pos"), target["size"])
            or not _same_positive_number(history_row.get("closePos"), target["size"])
        ):
            raise HistoricalManagementTerminalizationRefused("position_history_full_close_unproven")
    sibling = payload.get("sibling")
    expected_sibling = {
        "snapshot_complete": True,
        "binding_id": 307,
        "execution_order_leg_id": 531,
        "pos_id": "1001124899621086",
        "attribution_status": "verified",
        "leg_status": "active",
        "live_position_match_count": 1,
        "protection_complete": True,
        "ownership_conflicts": [],
    }
    if not isinstance(sibling, dict):
        raise HistoricalManagementTerminalizationRefused("batch_144_sibling_missing")
    _require_values(sibling, expected_sibling, "batch_144_sibling_unproven")
    return payload


def _same_positive_number(left: Any, right: Any) -> bool:
    from decimal import Decimal, InvalidOperation

    try:
        a, b = Decimal(str(left)), Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return a.is_finite() and b.is_finite() and a > 0 and a == b


def _one(
    connection: sqlite3.Connection,
    query: str,
    value: int | None,
    reason: str,
    *,
    parameterized: bool = True,
) -> dict[str, Any]:
    cursor = connection.execute(query, (value,)) if parameterized else connection.execute(query)
    row = cursor.fetchone()
    if row is None or cursor.fetchone() is not None:
        raise HistoricalManagementTerminalizationRefused(reason)
    return dict(row)


def _require_values(
    actual: Mapping[str, Any], expected: Mapping[str, Any], reason: str
) -> None:
    if any(actual.get(key) != value for key, value in expected.items()):
        raise HistoricalManagementTerminalizationRefused(reason)


def _database_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _action_payload(action: TerminalizationAction) -> dict[str, Any]:
    return {
        "table": action.table,
        "pk": action.pk,
        "before": dict(action.before),
        "after": dict(action.after),
    }


def _reverse_action_payload(action: TerminalizationAction) -> dict[str, Any]:
    return {
        "table": action.table,
        "pk": action.pk,
        "before": dict(action.after),
        "after": dict(action.before),
    }


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_ACTION_TABLES = {
    "strategy_management_components",
    "strategy_management_legs",
    "strategy_management_batches",
    "execution_order_legs",
    "execution_bindings",
    "strategy_lifecycles",
}

_EVIDENCE_TABLES = {
    "batches": "strategy_management_batches",
    "management_legs": "strategy_management_legs",
    "components": "strategy_management_components",
    "execution_legs": "execution_order_legs",
    "bindings": "execution_bindings",
    "lifecycles": "strategy_lifecycles",
    "mutation_intents": "position_mutation_intents",
    "source_messages": "raw_messages",
}


def _validate_plan_integrity(plan: TerminalizationPlan) -> None:
    if plan.schema_version != 1 or plan.mode != "dry_run":
        raise HistoricalManagementTerminalizationRefused("plan_schema_invalid")
    if (
        plan.snapshot_method != "sqlite_mode_ro_query_only_transaction"
        or tuple(plan.target_batch_ids) != TARGET_BATCH_IDS
    ):
        raise HistoricalManagementTerminalizationRefused("plan_target_contract_invalid")
    if plan.exchange_write_count != 0 or plan.action_count != 45:
        raise HistoricalManagementTerminalizationRefused("plan_action_contract_invalid")
    if set(plan.table_counts) != set(_COUNT_TABLES):
        raise HistoricalManagementTerminalizationRefused("plan_table_counts_invalid")
    expected_matrix = {
        "strategy_management_components": 16,
        "strategy_management_legs": 6,
        "strategy_management_batches": 6,
        "execution_order_legs": 6,
        "execution_bindings": 6,
        "strategy_lifecycles": 5,
    }
    actual_matrix = {
        table: sum(action.table == table for action in plan.actions)
        for table in _ACTION_TABLES
    }
    if actual_matrix != expected_matrix:
        raise HistoricalManagementTerminalizationRefused("plan_action_matrix_invalid")
    for action in plan.actions:
        if action.table not in _ACTION_TABLES:
            raise HistoricalManagementTerminalizationRefused("plan_table_invalid")
        if set(action.before) != set(action.after):
            raise HistoricalManagementTerminalizationRefused("plan_columns_invalid")
        if action.before.get("id") != action.pk or action.after.get("id") != action.pk:
            raise HistoricalManagementTerminalizationRefused("plan_primary_key_invalid")
    action_payload = [_action_payload(action) for action in plan.actions]
    rollback_payload = [_reverse_action_payload(action) for action in reversed(plan.actions)]
    if _fingerprint(plan.database_evidence) != plan.database_fingerprint:
        raise HistoricalManagementTerminalizationRefused("database_fingerprint_invalid")
    if _fingerprint(plan.exchange_evidence) != plan.exchange_fingerprint:
        raise HistoricalManagementTerminalizationRefused("exchange_fingerprint_invalid")
    if _fingerprint(action_payload) != plan.action_fingerprint:
        raise HistoricalManagementTerminalizationRefused("action_fingerprint_invalid")
    if _fingerprint(rollback_payload) != plan.rollback_fingerprint:
        raise HistoricalManagementTerminalizationRefused("rollback_fingerprint_invalid")
    plan_payload = {
        "schema_version": plan.schema_version,
        "code_sha": plan.code_sha,
        "repair_ts_utc": plan.repair_ts_utc,
        "database_fingerprint": plan.database_fingerprint,
        "exchange_fingerprint": plan.exchange_fingerprint,
        "action_fingerprint": plan.action_fingerprint,
        "rollback_fingerprint": plan.rollback_fingerprint,
        "action_count": plan.action_count,
    }
    if _fingerprint(plan_payload) != plan.plan_fingerprint:
        raise HistoricalManagementTerminalizationRefused("plan_fingerprint_invalid")
    token = hashlib.sha256(
        f"historical-management-terminalization:{plan.plan_fingerprint}".encode()
    ).hexdigest()[:16]
    if token != plan.confirmation_token:
        raise HistoricalManagementTerminalizationRefused("confirmation_token_invalid")


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _COUNT_TABLES
    }


def _validate_invariant_rows(
    connection: sqlite3.Connection, plan: TerminalizationPlan
) -> None:
    action_keys = {(action.table, action.pk) for action in plan.actions}
    for kind, table in _EVIDENCE_TABLES.items():
        rows = plan.database_evidence.get(kind)
        if not isinstance(rows, list):
            raise HistoricalManagementTerminalizationRefused("database_evidence_invalid")
        for stored in rows:
            if kind == "source_messages":
                row = connection.execute(
                    "SELECT id,message_id,text FROM raw_messages WHERE id=?",
                    (stored.get("id"),),
                ).fetchone()
                actual = None if row is None else {
                    "id": row["id"],
                    "message_id": row["message_id"],
                    "text_sha256": hashlib.sha256(
                        str(row["text"] or "").encode("utf-8")
                    ).hexdigest(),
                }
                if actual != stored:
                    raise HistoricalManagementTerminalizationRefused(
                        "database_invariant_changed"
                    )
                continue
            if kind == "execution_legs" and "batch_144_sibling_unchanged" in stored:
                stored = stored["batch_144_sibling_unchanged"]
            if not isinstance(stored, dict) or not isinstance(stored.get("id"), int):
                raise HistoricalManagementTerminalizationRefused("database_evidence_invalid")
            if (table, stored["id"]) in action_keys:
                continue
            columns = tuple(stored)
            query = f"SELECT {','.join(columns)} FROM {table} WHERE id=?"
            row = connection.execute(query, (stored["id"],)).fetchone()
            if row is None or dict(row) != stored:
                raise HistoricalManagementTerminalizationRefused("database_invariant_changed")


def _read_action_row(
    connection: sqlite3.Connection,
    action: TerminalizationAction,
    *,
    columns: tuple[str, ...],
) -> dict[str, Any] | None:
    if action.table not in _ACTION_TABLES or "id" not in columns:
        raise HistoricalManagementTerminalizationRefused("plan_table_invalid")
    row = connection.execute(
        f"SELECT {','.join(columns)} FROM {action.table} WHERE id=?",
        (action.pk,),
    ).fetchone()
    return None if row is None else dict(row)


def _cas_update(
    connection: sqlite3.Connection,
    *,
    action: TerminalizationAction,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    changed_columns = [
        column for column in before if column != "id" and before[column] != after[column]
    ]
    if not changed_columns:
        raise HistoricalManagementTerminalizationRefused("empty_action")
    predicate_columns = list(before)
    sql = (
        f"UPDATE {action.table} SET "
        + ",".join(f"{column}=?" for column in changed_columns)
        + " WHERE "
        + " AND ".join(f"{column} IS ?" for column in predicate_columns)
    )
    values = [after[column] for column in changed_columns]
    values.extend(before[column] for column in predicate_columns)
    cursor = connection.execute(sql, values)
    if cursor.rowcount != 1:
        raise HistoricalManagementTerminalizationRefused("compare_and_set_failed")


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _write_private_text(path: str | Path, content: str) -> None:
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(resolved, 0o600)


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalManagementTerminalizationRefused(
            "repair_timestamp_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise HistoricalManagementTerminalizationRefused("repair_timestamp_not_utc")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Supervised six-batch historical management terminalization"
    )
    parser.add_argument("--database-path", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    parser.add_argument("--evidence-directory")
    parser.add_argument("--expected-sibling-sha256")
    parser.add_argument("--repair-ts-utc")
    parser.add_argument("--code-sha")
    parser.add_argument("--output-plan")
    parser.add_argument("--rollback-sql-output")
    parser.add_argument("--plan-file")
    parser.add_argument("--expected-plan-fingerprint")
    parser.add_argument("--expected-rollback-fingerprint")
    parser.add_argument("--expected-action-count", type=int)
    parser.add_argument("--expected-repair-ts-utc")
    parser.add_argument("--confirmation-token")
    args = parser.parse_args(argv)

    try:
        if args.apply or args.rollback:
            if not args.plan_file or args.expected_action_count is None or not args.confirmation_token:
                raise HistoricalManagementTerminalizationRefused("mutation_arguments_missing")
            plan = load_terminalization_plan(args.plan_file)
            if args.apply:
                if not args.expected_plan_fingerprint or not args.expected_repair_ts_utc:
                    raise HistoricalManagementTerminalizationRefused("apply_arguments_missing")
                result = apply_terminalization_plan(
                    args.database_path,
                    plan=plan,
                    expected_plan_fingerprint=args.expected_plan_fingerprint,
                    expected_action_count=args.expected_action_count,
                    expected_repair_ts_utc=args.expected_repair_ts_utc,
                    confirmation_token=args.confirmation_token,
                )
            else:
                if not args.expected_rollback_fingerprint:
                    raise HistoricalManagementTerminalizationRefused("rollback_arguments_missing")
                result = rollback_terminalization_plan(
                    args.database_path,
                    plan=plan,
                    expected_rollback_fingerprint=args.expected_rollback_fingerprint,
                    expected_action_count=args.expected_action_count,
                    confirmation_token=args.confirmation_token,
                )
            print(_canonical_json(asdict(result)))
            return 0

        required = {
            "evidence_directory": args.evidence_directory,
            "expected_sibling_sha256": args.expected_sibling_sha256,
            "repair_ts_utc": args.repair_ts_utc,
            "code_sha": args.code_sha,
            "output_plan": args.output_plan,
            "rollback_sql_output": args.rollback_sql_output,
        }
        if any(value is None for value in required.values()):
            raise HistoricalManagementTerminalizationRefused("dry_run_arguments_missing")
        evidence = load_exchange_evidence_directory(
            args.evidence_directory,
            expected_sibling_sha256=args.expected_sibling_sha256,
        )
        plan = build_terminalization_plan(
            args.database_path,
            exchange_evidence=evidence,
            repair_ts=_parse_utc_timestamp(args.repair_ts_utc),
            code_sha=args.code_sha,
        )
        write_terminalization_plan(args.output_plan, plan)
        _write_private_text(args.rollback_sql_output, render_rollback_sql(plan))
        print(
            _canonical_json(
                {
                    "mode": "dry_run",
                    "status": "planned",
                    "action_count": plan.action_count,
                    "plan_fingerprint": plan.plan_fingerprint,
                    "rollback_fingerprint": plan.rollback_fingerprint,
                    "confirmation_token": plan.confirmation_token,
                    "exchange_write_count": plan.exchange_write_count,
                    "quick_check": plan.quick_check,
                }
            )
        )
        return 0
    except HistoricalManagementTerminalizationRefused as exc:
        print(_canonical_json({"mode": "refused", "reason": str(exc), "writes": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

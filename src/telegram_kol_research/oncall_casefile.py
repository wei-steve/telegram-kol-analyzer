"""The bounded, redacted evidence package one case is diagnosed from.

Phase 2 of the Codex on-call remediation program
(``docs/plans/2026-09-20-codex-oncall-phase2-spec.md``, section 5).

This module is the only thing that decides what leaves the production database
and reaches OpenAI. Everything it exports is:

**Bounded.** Every read is a primary-key point query or an index-seeking lookup
with a ``LIMIT`` -- the same discipline phase 1 adopted after the 2026-09-15
full-table scan froze the worker's event loop eight times. The finished package
is capped at :data:`CASEFILE_MAX_BYTES`; when it does not fit, it is trimmed in
one fixed order (spec 5: oldest execution events first, then the big JSON blobs)
and every cut is named in ``truncated``.

**Redacted.** ``oncall_codex.redact_structure`` runs over *every* string in the
package, keys included. The watcher's own bot token, anything spelled
``token=...``, and any long opaque base64/hex run are replaced before the file
is written, and the count lands in ``redactions``. The same function runs again
over the verdict that comes back (``oncall_codex.validate_verdict``), because a
diagnosis is also a string that ends up in a Telegram message. The patterns
live in ``oncall_codex`` rather than here only because that module is the one
the root-side runner is allowed to import.

**Marked as untrusted where it is untrusted.** KOL message text carries
``"trust": "untrusted_external_text"``. It is *data* in a JSON file -- it is
never interpolated into the prompt, which is a module constant in
``oncall_codex``.

Nothing here writes: the reader is ``oncall_detector.ProductionReader``
(``mode=ro`` plus ``PRAGMA query_only=ON``) and a test asserts zero writes
through an authorizer.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from telegram_kol_research.oncall_codex import redact_structure
from telegram_kol_research.oncall_detector import ProductionReader, as_utc
from telegram_kol_research.oncall_state import CaseRecord


logger = logging.getLogger(__name__)

#: The whole package, serialised, must fit in this. 64 KB is what spec 5 sets:
#: enough evidence to reason from, small enough that one pathological message
#: cannot turn into an expensive prompt.
CASEFILE_MAX_BYTES = 64 * 1024

SOURCE_TEXT_MAX_BYTES = 4096
ITEM_JSON_MAX_BYTES = 2048
SNAPSHOT_JSON_MAX_BYTES = 4096
RECENT_MESSAGE_MAX_CHARS = 500
RECENT_MESSAGE_COUNT = 5
EXECUTION_EVENT_LIMIT = 20
MUTATION_INTENT_LIMIT = 10
PROTECTION_LEDGER_LIMIT = 20
INCIDENT_LIMIT = 5
CANDIDATE_LIMIT = 10
INSTRUCTION_ITEM_LIMIT = 10
BATCH_LIMIT = 10
LEG_LIMIT = 10
COMPONENT_LIMIT = 20
JOURNAL_LINE_LIMIT = 200
JOURNAL_MAX_BYTES = 32 * 1024
STALLED_JOB_LIMIT = 20

CASEFILE_SCHEMA_VERSION = 1

UNTRUSTED = "untrusted_external_text"

#: Journal lines that are known to flood and carry no diagnostic value
#: (spec 5). Dropping them first is what keeps the 200-line budget useful.
JOURNAL_NOISE_FRAGMENTS = (
    "source_deletion",
    "runtime_incident_adapters",
    "recognition execution finding",
)

JOURNAL_UNITS = ("telegram-kol-worker.service",)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _clip_bytes(value: Any, limit: int) -> str | None:
    """Bound a string by *bytes*, cutting on a character boundary."""

    if value is None:
        return None
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "…"


def _clip_chars(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _iso(value: Any) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment is not None else None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _encoded_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


# --------------------------------------------------------------------------
# Column lists. Deliberately narrow: prompts, model replies and account
# identifiers beyond the ones spec 5 names are never selected at all.
# --------------------------------------------------------------------------

_RAW_MESSAGE_COLUMNS = (
    "id, chat_id, message_id, sender_name, posted_at, text, source_status, deleted_at"
)
_RECENT_MESSAGE_COLUMNS = "id, message_id, sender_name, posted_at, text"
_RECOGNITION_COLUMNS = (
    "id, raw_message_id, input_kind, authoritative_model, authoritative_status, "
    "agreement_status, automation_status, automation_reason, comparison_status, "
    "disagreement_severity, context_resolution_gate_json, created_at, updated_at"
)
_CANDIDATE_COLUMNS = (
    "id, raw_message_id, symbol, side, event_type, management_action, "
    "management_fraction, target_lifecycle_id, entry_text, stop_loss_text, "
    "stop_price_source, take_profit_text, confidence, created_at"
)
_ITEM_COLUMNS = (
    "id, raw_message_id, signal_candidate_id, sequence, instruction_kind, "
    "strategy_instance_id, status, result_json, error_json, retired_at, "
    "escalation_state, created_at, updated_at"
)
_BATCH_COLUMNS = (
    "id, raw_message_id, target_lifecycle_id, execution_binding_id, intent, "
    "effective_action, execution_mode, requested_fraction, effective_fraction, "
    "status, reason_code, partial_round_before, target_snapshot_json, "
    "planned_at, started_at, completed_at, updated_at"
)
_LEG_COLUMNS = (
    "id, management_batch_id, leg_index, status, preflight_size, "
    "planned_close_size, avg_entry_price, old_tpsl_json, planned_tpsl_json, "
    "last_error, updated_at"
)
_COMPONENT_COLUMNS = (
    "id, management_batch_id, strategy_management_leg_id, component_kind, "
    "sequence, status, reason_code, desired_json, attempt_count, updated_at"
)
_INTENT_COLUMNS = (
    "id, operation, execution_binding_id, status, error_json, reserved_at, "
    "submitted_at, confirmed_at"
)
_EVENT_COLUMNS = (
    "id, execution_binding_id, action, status, symbol, side, reason, "
    "exchange_event_time, created_at"
)
_LEDGER_COLUMNS = (
    "id, execution_binding_id, purpose, trigger_price, size_text, status, "
    "evidence_source, last_seen_at, last_verified_at"
)
_INCIDENT_COLUMNS = (
    "id, source_kind, source_record_id, incident_type, severity, status, "
    "repeat_count, redacted_summary, first_occurred_at, last_occurred_at"
)
_LIFECYCLE_COLUMNS = (
    "id, chat_id, message_id, symbol, side, lifecycle_status, exit_reason, "
    "entry_range_low, entry_range_high, stop_loss, take_profit, "
    "entry_price_actual, filled_tp_index, execution_binding_id, "
    "management_action, signal_at, entered_at, exited_at"
)
_BINDING_COLUMNS = (
    "id, strategy_instance_id, chat_id, symbol, side, venue, pos_id, status, "
    "last_exchange_status, recovered_at, created_at, updated_at"
)
_JOB_COLUMNS = "id, raw_message_id, chat_id, status, attempt_count, enqueued_at"


@dataclass(frozen=True, slots=True)
class CasefileConfig:
    max_bytes: int = CASEFILE_MAX_BYTES
    journal_window: timedelta = timedelta(minutes=10)
    journal_lines: int = JOURNAL_LINE_LIMIT
    journal_max_bytes: int = JOURNAL_MAX_BYTES


# --------------------------------------------------------------------------
# Section readers
# --------------------------------------------------------------------------


def _read_source_message(
    reader: ProductionReader, raw_message_id: int | None
) -> tuple[dict[str, Any] | None, Any]:
    row = reader.read_one("raw_messages", _RAW_MESSAGE_COLUMNS, raw_message_id)
    if row is None:
        return None, None
    payload = {
        "raw_message_id": int(row["id"]),
        "chat_id": _int_or_none(row["chat_id"]),
        "message_id": _int_or_none(row["message_id"]),
        "sender_name": row["sender_name"],
        "posted_at": _iso(row["posted_at"]),
        "source_status": row["source_status"],
        "deleted_at": _iso(row["deleted_at"]),
        "trust": UNTRUSTED,
        "text": _clip_bytes(row["text"] or "", SOURCE_TEXT_MAX_BYTES),
    }
    return payload, row


def _read_recognition(
    reader: ProductionReader, raw_message_id: int | None
) -> dict[str, Any] | None:
    if raw_message_id is None:
        return None
    rows = reader.query(
        f"SELECT {_RECOGNITION_COLUMNS} FROM recognition_decisions "
        "WHERE raw_message_id = ? ORDER BY id DESC LIMIT 1",
        (int(raw_message_id),),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "id": int(row["id"]),
        "input_kind": row["input_kind"],
        "authoritative_model": row["authoritative_model"],
        "authoritative_status": row["authoritative_status"],
        "agreement_status": row["agreement_status"],
        "automation_status": row["automation_status"],
        "automation_reason": _clip_bytes(row["automation_reason"], ITEM_JSON_MAX_BYTES),
        "comparison_status": row["comparison_status"],
        "disagreement_severity": row["disagreement_severity"],
        "context_resolution_gate_json": _clip_bytes(
            row["context_resolution_gate_json"], ITEM_JSON_MAX_BYTES
        ),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _read_candidates(
    reader: ProductionReader, raw_message_id: int | None
) -> list[dict[str, Any]]:
    if raw_message_id is None:
        return []
    rows = reader.query(
        f"SELECT {_CANDIDATE_COLUMNS} FROM signal_candidates "
        "WHERE raw_message_id = ? ORDER BY id LIMIT ?",
        (int(raw_message_id), CANDIDATE_LIMIT),
    )
    return [
        {
            "id": int(row["id"]),
            "symbol": row["symbol"],
            "side": row["side"],
            "event_type": row["event_type"],
            "management_action": row["management_action"],
            "management_fraction": row["management_fraction"],
            "target_lifecycle_id": _int_or_none(row["target_lifecycle_id"]),
            "entry_text": _clip_chars(row["entry_text"], 255),
            "stop_loss_text": _clip_chars(row["stop_loss_text"], 255),
            "stop_price_source": row["stop_price_source"],
            "take_profit_text": _clip_chars(row["take_profit_text"], 255),
            "confidence": row["confidence"],
            "created_at": _iso(row["created_at"]),
        }
        for row in rows
    ]


def _read_instruction_items(
    reader: ProductionReader, raw_message_id: int | None
) -> list[dict[str, Any]]:
    if raw_message_id is None:
        return []
    rows = reader.query(
        f"SELECT {_ITEM_COLUMNS} FROM message_instruction_items "
        "WHERE raw_message_id = ? ORDER BY id LIMIT ?",
        (int(raw_message_id), INSTRUCTION_ITEM_LIMIT),
    )
    return [
        {
            "id": int(row["id"]),
            "signal_candidate_id": _int_or_none(row["signal_candidate_id"]),
            "sequence": _int_or_none(row["sequence"]),
            "instruction_kind": row["instruction_kind"],
            "strategy_instance_id": row["strategy_instance_id"],
            "status": row["status"],
            "result_json": _clip_bytes(row["result_json"], ITEM_JSON_MAX_BYTES),
            "error_json": _clip_bytes(row["error_json"], ITEM_JSON_MAX_BYTES),
            "retired_at": _iso(row["retired_at"]),
            "escalation_state": row["escalation_state"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


def _read_batches(
    reader: ProductionReader, raw_message_id: int | None, batch_ids: Sequence[int]
) -> list[dict[str, Any]]:
    rows: list[Any] = []
    seen: set[int] = set()
    if raw_message_id is not None:
        rows.extend(
            reader.query(
                f"SELECT {_BATCH_COLUMNS} FROM strategy_management_batches "
                "WHERE raw_message_id = ? ORDER BY id LIMIT ?",
                (int(raw_message_id), BATCH_LIMIT),
            )
        )
        seen = {int(row["id"]) for row in rows}
    missing = [int(value) for value in batch_ids if int(value) not in seen]
    if missing:
        rows.extend(
            reader.read_by_ids(
                "strategy_management_batches", _BATCH_COLUMNS, missing[:BATCH_LIMIT]
            )
        )

    batches: list[dict[str, Any]] = []
    for row in rows[:BATCH_LIMIT]:
        batch_id = int(row["id"])
        batches.append(
            {
                "id": batch_id,
                "target_lifecycle_id": _int_or_none(row["target_lifecycle_id"]),
                "execution_binding_id": _int_or_none(row["execution_binding_id"]),
                "intent": row["intent"],
                "effective_action": row["effective_action"],
                "execution_mode": row["execution_mode"],
                "requested_fraction": row["requested_fraction"],
                "effective_fraction": row["effective_fraction"],
                "status": row["status"],
                "reason_code": row["reason_code"],
                "partial_round_before": _int_or_none(row["partial_round_before"]),
                "target_snapshot_json": _clip_bytes(
                    row["target_snapshot_json"], SNAPSHOT_JSON_MAX_BYTES
                ),
                "planned_at": _iso(row["planned_at"]),
                "started_at": _iso(row["started_at"]),
                "completed_at": _iso(row["completed_at"]),
                "updated_at": _iso(row["updated_at"]),
                "legs": _read_legs(reader, batch_id),
                "components": _read_components(reader, batch_id),
            }
        )
    return batches


def _read_legs(reader: ProductionReader, batch_id: int) -> list[dict[str, Any]]:
    rows = reader.query(
        f"SELECT {_LEG_COLUMNS} FROM strategy_management_legs "
        "WHERE management_batch_id = ? ORDER BY id LIMIT ?",
        (int(batch_id), LEG_LIMIT),
    )
    return [
        {
            "id": int(row["id"]),
            "leg_index": _int_or_none(row["leg_index"]),
            "status": row["status"],
            "preflight_size": row["preflight_size"],
            "planned_close_size": row["planned_close_size"],
            "avg_entry_price": row["avg_entry_price"],
            "old_tpsl_json": _clip_bytes(row["old_tpsl_json"], ITEM_JSON_MAX_BYTES),
            "planned_tpsl_json": _clip_bytes(
                row["planned_tpsl_json"], ITEM_JSON_MAX_BYTES
            ),
            "last_error": _clip_bytes(row["last_error"], ITEM_JSON_MAX_BYTES),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


def _read_components(reader: ProductionReader, batch_id: int) -> list[dict[str, Any]]:
    rows = reader.query(
        f"SELECT {_COMPONENT_COLUMNS} FROM strategy_management_components "
        "WHERE management_batch_id = ? ORDER BY id LIMIT ?",
        (int(batch_id), COMPONENT_LIMIT),
    )
    return [
        {
            "id": int(row["id"]),
            "strategy_management_leg_id": _int_or_none(
                row["strategy_management_leg_id"]
            ),
            "component_kind": row["component_kind"],
            "sequence": _int_or_none(row["sequence"]),
            "status": row["status"],
            "reason_code": row["reason_code"],
            "desired_json": _clip_bytes(row["desired_json"], ITEM_JSON_MAX_BYTES),
            "attempt_count": _int_or_none(row["attempt_count"]),
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]


def _read_mutation_intents(
    reader: ProductionReader, binding_id: int | None
) -> list[dict[str, Any]]:
    """Spec 5 asks for "the batch's" intents.

    ``position_mutation_intents`` carries no batch id; its indexed link to a
    management batch is the execution binding the batch targets, so that is
    what this follows.
    """

    if binding_id is None:
        return []
    rows = reader.query(
        f"SELECT {_INTENT_COLUMNS} FROM position_mutation_intents "
        "WHERE execution_binding_id = ? ORDER BY id DESC LIMIT ?",
        (int(binding_id), MUTATION_INTENT_LIMIT),
    )
    return [
        {
            "id": int(row["id"]),
            "operation": row["operation"],
            "status": row["status"],
            "error_json": _clip_bytes(row["error_json"], ITEM_JSON_MAX_BYTES),
            "reserved_at": _iso(row["reserved_at"]),
            "submitted_at": _iso(row["submitted_at"]),
            "confirmed_at": _iso(row["confirmed_at"]),
        }
        for row in rows
    ]


def _read_execution_events(
    reader: ProductionReader, *, binding_id: int | None, message_id: int | None
) -> list[dict[str, Any]]:
    if binding_id is not None:
        rows = reader.query(
            f"SELECT {_EVENT_COLUMNS} FROM execution_events "
            "WHERE execution_binding_id = ? ORDER BY id DESC LIMIT ?",
            (int(binding_id), EXECUTION_EVENT_LIMIT),
        )
    elif message_id is not None:
        rows = reader.query(
            f"SELECT {_EVENT_COLUMNS} FROM execution_events "
            "WHERE message_id = ? ORDER BY id DESC LIMIT ?",
            (int(message_id), EXECUTION_EVENT_LIMIT),
        )
    else:
        return []
    return [
        {
            "id": int(row["id"]),
            "action": row["action"],
            "status": row["status"],
            "symbol": row["symbol"],
            "side": row["side"],
            "reason": _clip_chars(row["reason"], 255),
            "exchange_event_time": _iso(row["exchange_event_time"]),
            "created_at": _iso(row["created_at"]),
        }
        for row in rows
    ]


def _read_protection_ledger(
    reader: ProductionReader, binding_id: int | None
) -> list[dict[str, Any]]:
    if binding_id is None:
        return []
    rows = reader.query(
        f"SELECT {_LEDGER_COLUMNS} FROM position_protection_ledger "
        "WHERE execution_binding_id = ? ORDER BY id DESC LIMIT ?",
        (int(binding_id), PROTECTION_LEDGER_LIMIT),
    )
    return [
        {
            "id": int(row["id"]),
            "purpose": row["purpose"],
            "trigger_price": row["trigger_price"],
            "size_text": row["size_text"],
            "status": row["status"],
            "evidence_source": row["evidence_source"],
            "last_seen_at": _iso(row["last_seen_at"]),
            "last_verified_at": _iso(row["last_verified_at"]),
        }
        for row in rows
    ]


def _read_related_incidents(
    reader: ProductionReader, references: Sequence[tuple[str, str]]
) -> list[dict[str, Any]]:
    """``runtime_incidents`` rows whose source record is one of this case's."""

    collected: list[dict[str, Any]] = []
    for source_kind, source_record_id in references:
        if len(collected) >= INCIDENT_LIMIT:
            break
        rows = reader.query(
            f"SELECT {_INCIDENT_COLUMNS} FROM runtime_incidents "
            "WHERE source_kind = ? AND source_record_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (str(source_kind), str(source_record_id), INCIDENT_LIMIT),
        )
        for row in rows:
            collected.append(
                {
                    "id": int(row["id"]),
                    "source_kind": row["source_kind"],
                    "source_record_id": row["source_record_id"],
                    "incident_type": row["incident_type"],
                    "severity": row["severity"],
                    "status": row["status"],
                    "repeat_count": _int_or_none(row["repeat_count"]),
                    "redacted_summary": _clip_bytes(
                        row["redacted_summary"], ITEM_JSON_MAX_BYTES
                    ),
                    "first_occurred_at": _iso(row["first_occurred_at"]),
                    "last_occurred_at": _iso(row["last_occurred_at"]),
                }
            )
    return collected[:INCIDENT_LIMIT]


def _read_recent_same_chat_messages(
    reader: ProductionReader, *, chat_id: int | None, before_id: int | None
) -> list[dict[str, Any]]:
    """The five messages before this one, in the same chat.

    A management instruction usually leans on what was said just before it
    ("那个止损再往上挪一点"), so without this the diagnosis has no referent.
    Still untrusted text, still bounded.
    """

    if chat_id is None or before_id is None:
        return []
    rows = reader.query(
        f"SELECT {_RECENT_MESSAGE_COLUMNS} FROM raw_messages "
        "WHERE chat_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
        (int(chat_id), int(before_id), RECENT_MESSAGE_COUNT),
    )
    return [
        {
            "raw_message_id": int(row["id"]),
            "message_id": _int_or_none(row["message_id"]),
            "sender_name": row["sender_name"],
            "posted_at": _iso(row["posted_at"]),
            "trust": UNTRUSTED,
            "text": _clip_chars(row["text"] or "", RECENT_MESSAGE_MAX_CHARS),
        }
        for row in reversed(rows)
    ]


def _read_lifecycle(
    reader: ProductionReader, lifecycle_id: int | None
) -> dict[str, Any] | None:
    row = reader.read_one("strategy_lifecycles", _LIFECYCLE_COLUMNS, lifecycle_id)
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "chat_id": _int_or_none(row["chat_id"]),
        "message_id": _int_or_none(row["message_id"]),
        "symbol": row["symbol"],
        "side": row["side"],
        "lifecycle_status": row["lifecycle_status"],
        "exit_reason": row["exit_reason"],
        "entry_range_low": row["entry_range_low"],
        "entry_range_high": row["entry_range_high"],
        "stop_loss": row["stop_loss"],
        "take_profit": _clip_chars(row["take_profit"], 255),
        "entry_price_actual": row["entry_price_actual"],
        "filled_tp_index": _int_or_none(row["filled_tp_index"]),
        "execution_binding_id": _int_or_none(row["execution_binding_id"]),
        "management_action": row["management_action"],
        "signal_at": _iso(row["signal_at"]),
        "entered_at": _iso(row["entered_at"]),
        "exited_at": _iso(row["exited_at"]),
    }


def _read_binding(
    reader: ProductionReader, binding_id: int | None
) -> dict[str, Any] | None:
    """The binding, minus the account identifiers.

    Spec 5: whether a ``pos_id`` exists is evidence; the ``pos_id`` itself is
    an account identifier and is not exported.
    """

    row = reader.read_one("execution_bindings", _BINDING_COLUMNS, binding_id)
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "chat_id": _int_or_none(row["chat_id"]),
        "symbol": row["symbol"],
        "side": row["side"],
        "venue": row["venue"],
        "has_pos_id": bool(str(row["pos_id"] or "").strip()),
        "status": row["status"],
        "last_exchange_status": row["last_exchange_status"],
        "recovered_at": _iso(row["recovered_at"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


# --------------------------------------------------------------------------
# Journal excerpt (health cases only)
# --------------------------------------------------------------------------


def read_journal_excerpt(
    *,
    since: datetime,
    units: Sequence[str] = JOURNAL_UNITS,
    line_limit: int = JOURNAL_LINE_LIMIT,
    max_bytes: int = JOURNAL_MAX_BYTES,
    runner: Callable[[Sequence[str]], str] | None = None,
) -> dict[str, Any]:
    """The worker's last few minutes of journal, minus the known flood.

    ``journalctl`` is read through a fixed argument list -- nothing from the
    case, and nothing from a message, is ever interpolated into it. Any
    failure (no binary, no permission, a timeout) is reported as
    ``available: false`` and never raises: a missing journal must not stop a
    case from being diagnosed.
    """

    command = [
        "journalctl",
        "--no-pager",
        "--output=short-iso",
        f"--since=@{int(since.timestamp())}",
        "--lines",
        str(int(line_limit) * 4),
    ]
    for unit in units:
        command += ["-u", str(unit)]

    try:
        text = (runner or _run_journalctl)(command)
    except Exception as exc:  # noqa: BLE001 - a journal is a nice-to-have
        logger.info("oncall journal excerpt unavailable: %s", type(exc).__name__)
        return {"available": False, "reason": type(exc).__name__, "lines": []}

    kept: list[str] = []
    dropped = 0
    for line in str(text).splitlines():
        if any(fragment in line for fragment in JOURNAL_NOISE_FRAGMENTS):
            dropped += 1
            continue
        kept.append(line)
    kept = kept[-int(line_limit) :]
    while kept and len("\n".join(kept).encode("utf-8")) > int(max_bytes):
        kept.pop(0)
    return {
        "available": True,
        "unit": list(units),
        "since": since.isoformat(),
        "dropped_noise_lines": dropped,
        "lines": kept,
    }


def _run_journalctl(command: Sequence[str]) -> str:
    if shutil.which(command[0]) is None:
        raise FileNotFoundError(command[0])
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        list(command),
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    return done.stdout or ""


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _case_section(case: CaseRecord) -> dict[str, Any]:
    evidence = case.evidence or {}
    return {
        "case_id": int(case.id),
        "case_key": case.case_key,
        "rule": case.rule,
        "severity": case.severity,
        "kind": "health" if case.case_key.startswith("health:") else "management",
        "opened_at": case.first_seen_at.isoformat() if case.first_seen_at else None,
        "last_seen_at": case.last_seen_at.isoformat() if case.last_seen_at else None,
        "reason_code": case.reason_code,
        "target_uncertain": bool(case.target_uncertain),
        "chat_id": case.chat_id,
        "group_name": evidence.get("group_name"),
        "action": evidence.get("action"),
        "position_state": evidence.get("position_state"),
        "minutes_since_message": evidence.get("minutes_since_message"),
    }


def _management_sections(
    reader: ProductionReader, case: CaseRecord, config: CasefileConfig
) -> dict[str, Any]:
    source_message, raw_row = _read_source_message(reader, case.raw_message_id)
    candidates = _read_candidates(reader, case.raw_message_id)
    items = _read_instruction_items(reader, case.raw_message_id)
    batches = _read_batches(reader, case.raw_message_id, case.batch_ids)

    lifecycle_id = next(
        (
            candidate["target_lifecycle_id"]
            for candidate in candidates
            if candidate.get("target_lifecycle_id")
        ),
        None,
    ) or next(
        (batch["target_lifecycle_id"] for batch in batches if batch.get("target_lifecycle_id")),
        None,
    )
    lifecycle = _read_lifecycle(reader, lifecycle_id)
    binding_id = (
        next(
            (
                batch["execution_binding_id"]
                for batch in batches
                if batch.get("execution_binding_id")
            ),
            None,
        )
        or (lifecycle or {}).get("execution_binding_id")
        or (case.evidence or {}).get("execution_binding_id")
    )
    binding_id = _int_or_none(binding_id)

    references: list[tuple[str, str]] = []
    if case.raw_message_id is not None:
        references.append(("raw_message", str(int(case.raw_message_id))))
    for item in items:
        references.append(("message_instruction_item", str(item["id"])))
    for batch in batches:
        references.append(("strategy_management_batch", str(batch["id"])))
    if binding_id is not None:
        references.append(("execution_binding", str(binding_id)))

    return {
        "source_message": source_message,
        "recognition": _read_recognition(reader, case.raw_message_id),
        "candidates": candidates,
        "instruction_items": items,
        "batches": batches,
        "position_mutation_intents": _read_mutation_intents(reader, binding_id),
        "execution_events": _read_execution_events(
            reader,
            binding_id=binding_id,
            message_id=_int_or_none(raw_row["message_id"]) if raw_row is not None else None,
        ),
        "lifecycle": lifecycle,
        "execution_binding": _read_binding(reader, binding_id),
        "protection_ledger": _read_protection_ledger(reader, binding_id),
        "related_incidents": _read_related_incidents(reader, references),
        "recent_same_chat_messages": _read_recent_same_chat_messages(
            reader,
            chat_id=case.chat_id,
            before_id=case.raw_message_id,
        ),
    }


def _health_sections(
    reader: ProductionReader,
    case: CaseRecord,
    *,
    now: datetime,
    config: CasefileConfig,
    stalled_job_ids: Sequence[int],
    journal_runner: Callable[[Sequence[str]], str] | None,
) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    if stalled_job_ids:
        rows = reader.read_by_ids(
            "message_processing_jobs",
            _JOB_COLUMNS,
            [int(value) for value in stalled_job_ids][:STALLED_JOB_LIMIT],
        )
        for row in rows:
            enqueued = as_utc(row["enqueued_at"])
            jobs.append(
                {
                    "id": int(row["id"]),
                    "raw_message_id": _int_or_none(row["raw_message_id"]),
                    "chat_id": _int_or_none(row["chat_id"]),
                    "status": row["status"],
                    "attempt_count": _int_or_none(row["attempt_count"]),
                    "enqueued_at": _iso(row["enqueued_at"]),
                    "waiting_minutes": (
                        max(0, int((now - enqueued).total_seconds() // 60))
                        if enqueued is not None
                        else None
                    ),
                }
            )
    rows = reader.query(
        f"SELECT {_INCIDENT_COLUMNS} FROM runtime_incidents ORDER BY id DESC LIMIT ?",
        (INCIDENT_LIMIT,),
    )
    incidents = [
        {
            "id": int(row["id"]),
            "source_kind": row["source_kind"],
            "source_record_id": row["source_record_id"],
            "incident_type": row["incident_type"],
            "severity": row["severity"],
            "status": row["status"],
            "repeat_count": _int_or_none(row["repeat_count"]),
            "redacted_summary": _clip_bytes(row["redacted_summary"], ITEM_JSON_MAX_BYTES),
            "first_occurred_at": _iso(row["first_occurred_at"]),
            "last_occurred_at": _iso(row["last_occurred_at"]),
        }
        for row in rows
    ]
    return {
        "health": {
            "stalled_jobs": jobs,
            "recent_incidents": incidents,
            "journal": read_journal_excerpt(
                since=now - config.journal_window,
                line_limit=config.journal_lines,
                max_bytes=config.journal_max_bytes,
                runner=journal_runner,
            ),
        }
    }


def build_case_file(
    reader: ProductionReader,
    *,
    case: CaseRecord,
    now: datetime,
    config: CasefileConfig | None = None,
    stalled_job_ids: Sequence[int] = (),
    journal_runner: Callable[[Sequence[str]], str] | None = None,
) -> dict[str, Any]:
    """Export one case as a bounded, redacted JSON-shaped dictionary."""

    settings = config or CasefileConfig()
    payload: dict[str, Any] = {
        "schema_version": CASEFILE_SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "case": _case_section(case),
    }
    if case.case_key.startswith("health:"):
        payload.update(
            _health_sections(
                reader,
                case,
                now=now,
                config=settings,
                stalled_job_ids=stalled_job_ids,
                journal_runner=journal_runner,
            )
        )
    else:
        payload.update(_management_sections(reader, case, settings))

    payload, redactions = redact_structure(payload)
    payload["redactions"] = int(redactions)
    payload["truncated"] = []
    return trim_to_budget(payload, max_bytes=settings.max_bytes)


# --------------------------------------------------------------------------
# Trimming (spec 5: one fixed order, and every cut is recorded)
# --------------------------------------------------------------------------

#: After the two steps spec 5 names, these lists are shed in this order. The
#: spec stops at "the big JSON fields"; a 64 KB guarantee needs a floor under
#: it, so the remaining bounded lists go next and the untrusted message text is
#: cut last, because it is the one thing the diagnosis cannot do without.
_OVERFLOW_LISTS = (
    "recent_same_chat_messages",
    "related_incidents",
    "protection_ledger",
    "position_mutation_intents",
    "candidates",
)

#: Big JSON blobs, most expendable first.
_BIG_JSON_FIELDS = (
    ("batches", "target_snapshot_json"),
    ("batches", "legs"),
    ("batches", "components"),
    ("instruction_items", "result_json"),
    ("instruction_items", "error_json"),
    ("recognition", "context_resolution_gate_json"),
    ("recognition", "automation_reason"),
)


def trim_to_budget(payload: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    """Shrink the package until it fits, recording every cut in ``truncated``."""

    truncated: list[str] = list(payload.get("truncated") or [])

    def fits() -> bool:
        return _encoded_size(payload) <= int(max_bytes)

    # 1. Oldest execution events first.
    events = payload.get("execution_events")
    if isinstance(events, list):
        dropped = 0
        while not fits() and events:
            events.pop()  # the list is newest-first, so the tail is the oldest
            dropped += 1
        if dropped:
            truncated.append(f"execution_events:-{dropped}")

    # 2. Then the big JSON fields.
    if not fits():
        for section, field in _BIG_JSON_FIELDS:
            if fits():
                break
            if _blank_field(payload.get(section), field):
                truncated.append(f"{section}.{field}")

    # 3. Then the remaining bounded lists, oldest evidence first.
    if not fits():
        for name in _OVERFLOW_LISTS:
            if fits():
                break
            value = payload.get(name)
            if isinstance(value, list) and value:
                payload[name] = []
                truncated.append(f"{name}:cleared")

    # 4. The journal, for a health case.
    health = payload.get("health")
    if not fits() and isinstance(health, dict) and health.get("journal"):
        health["journal"] = {"available": False, "reason": "trimmed", "lines": []}
        truncated.append("health.journal")

    # 5. Last: the untrusted message text itself.
    source = payload.get("source_message")
    if not fits() and isinstance(source, dict) and source.get("text"):
        overflow = _encoded_size(payload) - int(max_bytes)
        text_bytes = len(str(source["text"]).encode("utf-8"))
        source["text"] = _clip_bytes(source["text"], max(200, text_bytes - overflow - 64))
        truncated.append("source_message.text")

    payload["truncated"] = truncated
    return payload


def _blank_field(section: Any, field: str) -> bool:
    """Replace one field with a marker wherever it appears. True if it did."""

    if isinstance(section, dict):
        if section.get(field):
            section[field] = "<trimmed>"
            return True
        return False
    if isinstance(section, list):
        changed = False
        for entry in section:
            if isinstance(entry, dict) and entry.get(field):
                entry[field] = "<trimmed>"
                changed = True
        return changed
    return False


def serialise_case_file(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)


def collect_untrusted_texts(payload: Mapping[str, Any]) -> list[str]:
    """Every string the package marks as untrusted. Used by the tests."""

    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if node.get("trust") == UNTRUSTED and isinstance(node.get("text"), str):
                found.append(node["text"])
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    return found


def iter_strings(payload: Any) -> Iterable[str]:
    """Every string anywhere in the package, keys included."""

    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(key, str):
                yield key
            yield from iter_strings(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from iter_strings(item)

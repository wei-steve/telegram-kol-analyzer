"""Canonical completeness authority for bounded Deepcoin collection reads."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.deepcoin_execution_operations import (
    load_account_write_generation,
)


_DEFAULT_PAGE_LIMIT = 100
_MAX_ROW_DEPTH = 12
_MAX_ROW_NODES = 20_000
_MAX_ROW_STRING_LENGTH = 16_384


class DeepcoinSnapshotUnavailable(RuntimeError):
    """A bounded read did not prove an authoritative exchange collection."""


@dataclass(frozen=True, slots=True)
class ExchangeCollectionEvidence:
    endpoint: str
    available: bool
    schema_valid: bool
    complete: bool
    rows: tuple[Mapping[str, Any], ...] = field(repr=False)
    row_count: int
    page_count: int
    fingerprint: str | None
    reason_code: str | None
    expected_order_ids_visible: bool = False


@dataclass(frozen=True, slots=True)
class AccountSnapshotEvidence:
    uid_scope_hash: str
    start_write_generation: int
    end_write_generation: int
    collections: tuple[ExchangeCollectionEvidence, ...]
    complete: bool
    reason_code: str | None


def build_exchange_collection_evidence(
    *,
    endpoint: str,
    response: object,
    read_error: BaseException | None = None,
    expected_order_ids: Sequence[str] | set[str] | frozenset[str] = (),
    page_limit: int = _DEFAULT_PAGE_LIMIT,
) -> ExchangeCollectionEvidence:
    """Classify one list response without converting uncertainty into absence."""

    safe_endpoint = _safe_endpoint(endpoint)
    if read_error is not None:
        return _unavailable_collection(
            endpoint=safe_endpoint,
            available=False,
            reason_code="snapshot_read_unavailable",
        )
    if isinstance(response, list):
        raw_rows = response
        metadata: Mapping[str, Any] = {}
    elif isinstance(response, Mapping):
        raw_rows = response.get("data")
        metadata = response
    else:
        return _unavailable_collection(
            endpoint=safe_endpoint,
            available=True,
            reason_code="snapshot_schema_invalid",
        )
    if not isinstance(raw_rows, list) or not all(
        isinstance(row, Mapping) for row in raw_rows
    ):
        return _unavailable_collection(
            endpoint=safe_endpoint,
            available=True,
            reason_code="snapshot_schema_invalid",
        )
    limit = _positive_int(page_limit, fallback=_DEFAULT_PAGE_LIMIT)
    try:
        canonical_rows = tuple(_canonical_row(row) for row in raw_rows)
        fingerprint = _collection_fingerprint(canonical_rows)
        frozen_rows = tuple(_freeze_mapping(row) for row in raw_rows)
    except (RecursionError, TypeError, ValueError):
        return _unavailable_collection(
            endpoint=safe_endpoint,
            available=True,
            reason_code="snapshot_schema_invalid",
        )

    complete, reason_code = _pagination_completeness(
        metadata=metadata,
        row_count=len(raw_rows),
        page_limit=limit,
    )
    expected = {
        str(value).strip()
        for value in expected_order_ids
        if isinstance(value, str) and value.strip()
    }
    visible_order_ids = {
        order_id
        for row in raw_rows
        if (order_id := _order_id(row)) is not None
    }
    return ExchangeCollectionEvidence(
        endpoint=safe_endpoint,
        available=True,
        schema_valid=True,
        complete=complete,
        rows=frozen_rows,
        row_count=len(frozen_rows),
        page_count=1,
        fingerprint=fingerprint,
        reason_code=reason_code,
        expected_order_ids_visible=bool(complete and expected.issubset(visible_order_ids)),
    )


def capture_account_snapshot(
    session_factory: sessionmaker,
    *,
    uid_scope_hash: str,
    readers: Mapping[str, Callable[[], object]],
) -> AccountSnapshotEvidence:
    """Capture collections and invalidate all of them on local writer drift."""

    uid_hash = _fingerprint(uid_scope_hash)
    if not isinstance(readers, Mapping) or not readers:
        raise ValueError("snapshot_readers_required")
    start_generation = _current_generation(session_factory, uid_hash)
    collections: list[ExchangeCollectionEvidence] = []
    for endpoint, reader in sorted(readers.items(), key=lambda item: str(item[0])):
        safe_endpoint = _safe_endpoint(endpoint)
        if not callable(reader):
            collections.append(
                _unavailable_collection(
                    endpoint=safe_endpoint,
                    available=False,
                    reason_code="snapshot_reader_invalid",
                )
            )
            continue
        try:
            response = reader()
        except Exception as exc:
            collections.append(
                build_exchange_collection_evidence(
                    endpoint=safe_endpoint,
                    response=None,
                    read_error=exc,
                )
            )
            continue
        collections.append(
            build_exchange_collection_evidence(
                endpoint=safe_endpoint,
                response=response,
            )
        )
    end_generation = _current_generation(session_factory, uid_hash)
    generation_reason = None
    if start_generation != end_generation:
        generation_reason = "snapshot_write_generation_changed"
    elif start_generation % 2 != 0:
        generation_reason = "snapshot_write_in_progress"
    if generation_reason is not None:
        collections = [
            replace(
                collection,
                complete=False,
                reason_code=generation_reason,
                expected_order_ids_visible=False,
            )
            for collection in collections
        ]
        reason_code = generation_reason
        complete = False
    else:
        complete = all(collection.complete for collection in collections)
        reason_code = next(
            (
                collection.reason_code
                for collection in collections
                if not collection.complete and collection.reason_code
            ),
            None,
        )
    return AccountSnapshotEvidence(
        uid_scope_hash=uid_hash,
        start_write_generation=start_generation,
        end_write_generation=end_generation,
        collections=tuple(collections),
        complete=complete,
        reason_code=reason_code,
    )


def require_complete_collection(
    evidence: ExchangeCollectionEvidence,
) -> tuple[Mapping[str, Any], ...]:
    """Return rows only after an explicit complete proof."""

    if not isinstance(evidence, ExchangeCollectionEvidence) or not evidence.complete:
        reason = (
            evidence.reason_code
            if isinstance(evidence, ExchangeCollectionEvidence)
            else "snapshot_evidence_invalid"
        )
        raise DeepcoinSnapshotUnavailable(reason or "snapshot_incomplete")
    return evidence.rows


def _pagination_completeness(
    *,
    metadata: Mapping[str, Any],
    row_count: int,
    page_limit: int,
) -> tuple[bool, str | None]:
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(key).lower()): value
        for key, value in metadata.items()
    }
    pagination = {
        key
        for key in normalized
        if (
            "cursor" in key
            or key.startswith("page")
            or key.startswith("total")
            or key in {"hasmore", "morepages", "islastpage"}
        )
    }
    if pagination:
        has_more = normalized.get("hasmore", object())
        cursor_values = [
            normalized[key]
            for key in pagination
            if "cursor" in key
        ]
        supported_end = (
            "hasmore" in normalized
            and has_more is False
            and all(value in (None, "") for value in cursor_values)
            and not any(
                key.startswith("page") or key.startswith("total")
                for key in pagination
            )
        )
        if supported_end:
            return True, None
        return False, "snapshot_pagination_incomplete"
    if row_count >= page_limit:
        return False, "snapshot_page_limit_ambiguous"
    return True, None


def _unavailable_collection(
    *,
    endpoint: str,
    available: bool,
    reason_code: str,
) -> ExchangeCollectionEvidence:
    return ExchangeCollectionEvidence(
        endpoint=endpoint,
        available=available,
        schema_valid=False,
        complete=False,
        rows=(),
        row_count=0,
        page_count=0,
        fingerprint=None,
        reason_code=reason_code,
        expected_order_ids_visible=False,
    )


def _canonical_row(row: Mapping[str, Any]) -> str:
    node_count = 0

    def normalize(value: Any, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if depth > _MAX_ROW_DEPTH or node_count > _MAX_ROW_NODES:
            raise ValueError("snapshot_row_complexity_exceeded")
        if value is None or isinstance(value, bool) or isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("snapshot_row_number_invalid")
            return value
        if isinstance(value, str):
            if len(value) > _MAX_ROW_STRING_LENGTH:
                raise ValueError("snapshot_row_string_too_long")
            return value
        if isinstance(value, Mapping):
            normalized: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str) or not key or len(key) > 256:
                    raise ValueError("snapshot_row_key_invalid")
                normalized[key] = normalize(child, depth + 1)
            return normalized
        if isinstance(value, Sequence) and not isinstance(
            value, (bytes, bytearray, memoryview)
        ):
            return [normalize(child, depth + 1) for child in value]
        raise ValueError("snapshot_row_type_invalid")

    normalized = normalize(row, 0)
    return json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _collection_fingerprint(canonical_rows: tuple[str, ...]) -> str:
    canonical_collection = "[" + ",".join(sorted(canonical_rows)) + "]"
    return hashlib.sha256(canonical_collection.encode("utf-8")).hexdigest()


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any, depth: int = 0) -> Any:
        if depth > _MAX_ROW_DEPTH:
            raise ValueError("snapshot_row_complexity_exceeded")
        if isinstance(item, Mapping):
            return MappingProxyType(
                {str(key): freeze(child, depth + 1) for key, child in item.items()}
            )
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray, memoryview)
        ):
            return tuple(freeze(child, depth + 1) for child in item)
        return item

    frozen = freeze(value)
    if not isinstance(frozen, Mapping):
        raise ValueError("snapshot_row_invalid")
    return frozen


def _order_id(row: Mapping[str, Any]) -> str | None:
    for key in ("ordId", "orderId", "order_id", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip() or None
    return None


def _current_generation(session_factory: sessionmaker, uid_scope_hash: str) -> int:
    row = load_account_write_generation(
        session_factory,
        uid_scope_hash=uid_scope_hash,
    )
    return 0 if row is None else int(row.generation)


def _safe_endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("snapshot_endpoint_invalid")
    normalized = value.strip().lower()
    if (
        not normalized
        or len(normalized) > 128
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in normalized)
    ):
        raise ValueError("snapshot_endpoint_invalid")
    return normalized


def _fingerprint(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("uid_scope_hash_invalid")
    return value


def _positive_int(value: object, *, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value

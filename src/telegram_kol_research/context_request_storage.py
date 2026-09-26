"""Compact provenance and strict storage tags for context-resolution requests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from telegram_kol_research.context_resolution_prompt import (
    build_context_provider_messages,
)


REQUEST_STORAGE_CONTRACT = "context-resolution-request-storage-v1"
REQUEST_COMPONENTS = (
    "current_message",
    "saved_evidence",
    "message_context",
    "candidate_strategy_threads",
    "redacted_exchange_state",
    "mimo_first_pass",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: Exact field set of the marker ``db_retention`` leaves in place of an old
#: request (``docs/plans/2026-09-26-server-disk-usage-analysis.md`` section 5.3).
#: It keeps the one projection an online reader still derives from the request,
#: ``candidate_thread_ids`` (the worker's state fingerprint falls back to the
#: request when ``candidate_thread_ids_json`` is NULL, which it is for every row
#: written before R1), so replacing the request never changes a fingerprint.
RETENTION_STUB_FIELDS = frozenset(
    {
        "candidate_thread_ids",
        "contract",
        "original_bytes",
        "original_sha256",
        "storage",
        "stubbed_at",
    }
)


class ContextRequestStorageError(ValueError):
    """The stored request is malformed or unavailable in the live database."""


@dataclass(frozen=True, slots=True)
class ParsedContextRequestStorage:
    storage: Literal["legacy-full", "reference-only", "archived", "retention-stub"]
    request_payload: dict[str, Any] | None
    archive_artifact_sha256: str | None = None
    record_sha256: str | None = None
    #: Only for ``retention-stub``: the exact ``collect_candidate_thread_ids``
    #: projection of the request that was removed.
    candidate_thread_ids: tuple[int, ...] | None = None

    def require_legacy_full(self) -> dict[str, Any]:
        if self.storage == "archived":
            raise ContextRequestStorageError("request payload is archived")
        if self.storage == "retention-stub":
            raise ContextRequestStorageError(
                "request payload was removed by retention"
            )
        if self.storage == "reference-only":
            raise ContextRequestStorageError("request payload is reference-only")
        if self.request_payload is None:
            raise ContextRequestStorageError("legacy request payload is missing")
        return self.request_payload


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_canonical(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def parse_context_request_storage(
    value: str | Mapping[str, Any],
) -> ParsedContextRequestStorage:
    """Parse an untagged legacy request or one exact v1 storage marker."""

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ContextRequestStorageError("request storage is malformed JSON") from exc
    elif isinstance(value, Mapping):
        parsed = dict(value)
    else:
        raise ContextRequestStorageError("request storage must be a JSON object")
    if not isinstance(parsed, dict):
        raise ContextRequestStorageError("request storage must be a JSON object")

    tagged = "contract" in parsed or "storage" in parsed
    if not tagged:
        return ParsedContextRequestStorage(
            storage="legacy-full",
            request_payload=parsed,
        )
    if parsed.get("contract") != REQUEST_STORAGE_CONTRACT:
        raise ContextRequestStorageError("unknown request storage contract")

    storage = parsed.get("storage")
    if storage == "reference_only":
        if set(parsed) != {"contract", "storage"}:
            raise ContextRequestStorageError(
                "reference-only marker must have exact fields"
            )
        return ParsedContextRequestStorage(
            storage="reference-only",
            request_payload=None,
        )
    if storage == "archive":
        if set(parsed) != {
            "archive_artifact_sha256",
            "contract",
            "record_sha256",
            "storage",
        }:
            raise ContextRequestStorageError("archived marker must have exact fields")
        archive_sha = parsed.get("archive_artifact_sha256")
        record_sha = parsed.get("record_sha256")
        if not isinstance(archive_sha, str) or not _SHA256_RE.fullmatch(archive_sha):
            raise ContextRequestStorageError(
                "archived marker has invalid archive artifact SHA-256"
            )
        if not isinstance(record_sha, str) or not _SHA256_RE.fullmatch(record_sha):
            raise ContextRequestStorageError(
                "archived marker has invalid record SHA-256"
            )
        return ParsedContextRequestStorage(
            storage="archived",
            request_payload=None,
            archive_artifact_sha256=archive_sha,
            record_sha256=record_sha,
        )
    if storage == "retention_stub":
        if set(parsed) != RETENTION_STUB_FIELDS:
            raise ContextRequestStorageError(
                "retention stub marker must have exact fields"
            )
        original_sha = parsed.get("original_sha256")
        if not isinstance(original_sha, str) or not _SHA256_RE.fullmatch(original_sha):
            raise ContextRequestStorageError(
                "retention stub has invalid original SHA-256"
            )
        original_bytes = parsed.get("original_bytes")
        if (
            isinstance(original_bytes, bool)
            or not isinstance(original_bytes, int)
            or original_bytes < 0
        ):
            raise ContextRequestStorageError(
                "retention stub has invalid original byte count"
            )
        if not isinstance(parsed.get("stubbed_at"), str):
            raise ContextRequestStorageError("retention stub has invalid stubbed_at")
        thread_ids = parsed.get("candidate_thread_ids")
        if (
            not isinstance(thread_ids, list)
            or any(isinstance(item, bool) or not isinstance(item, int) for item in thread_ids)
            or thread_ids != sorted(set(thread_ids))
        ):
            raise ContextRequestStorageError(
                "retention stub candidate thread IDs must be sorted unique integers"
            )
        return ParsedContextRequestStorage(
            storage="retention-stub",
            request_payload=None,
            record_sha256=original_sha,
            candidate_thread_ids=tuple(thread_ids),
        )
    raise ContextRequestStorageError("unknown storage state")


def build_retention_stub(stored: str, *, stubbed_at: str) -> str | None:
    """Return the marker that replaces a legacy full request, or ``None``.

    ``None`` means "leave this value alone": it is already a tagged marker
    (reference-only, archived, or an earlier stub -- which is what makes the
    retention job idempotent), or it is not parseable at all, in which case
    replacing it would destroy the only evidence of what was wrong with it.
    """

    try:
        parsed = parse_context_request_storage(stored)
    except ContextRequestStorageError:
        return None
    if parsed.storage != "legacy-full" or parsed.request_payload is None:
        return None
    encoded = stored.encode("utf-8")
    return _canonical_json(
        {
            "candidate_thread_ids": collect_candidate_thread_ids(
                parsed.request_payload
            ),
            "contract": REQUEST_STORAGE_CONTRACT,
            "original_bytes": len(encoded),
            "original_sha256": hashlib.sha256(encoded).hexdigest(),
            "storage": "retention_stub",
            "stubbed_at": str(stubbed_at),
        }
    )


def collect_candidate_thread_ids(value: Any) -> list[int]:
    """Return the exact recursive thread-ID projection in canonical order."""

    found: set[int] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in {"thread_id", "strategy_thread_id"} and item is not None:
                try:
                    found.add(int(item))
                except (TypeError, ValueError):
                    pass
            found.update(collect_candidate_thread_ids(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(collect_candidate_thread_ids(item))
    return sorted(found)


def parse_candidate_thread_ids(value: str) -> set[int]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContextRequestStorageError(
            "candidate thread-ID projection is malformed JSON"
        ) from exc
    if not isinstance(parsed, list):
        raise ContextRequestStorageError(
            "candidate thread-ID projection must be a list"
        )
    if any(isinstance(item, bool) or not isinstance(item, int) for item in parsed):
        raise ContextRequestStorageError(
            "candidate thread-ID projection must contain integers"
        )
    if parsed != sorted(set(parsed)):
        raise ContextRequestStorageError(
            "candidate thread-ID projection must be sorted and unique"
        )
    return set(parsed)


def _message_ref(value: Any) -> list[int | None]:
    source = value if isinstance(value, Mapping) else {}

    def optional_int(key: str) -> int | None:
        raw = source.get(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ContextRequestStorageError(
                f"context message reference has invalid {key}"
            ) from exc

    return [
        optional_int("raw_message_id"),
        optional_int("message_id"),
        optional_int("evidence_version_id"),
    ]


def build_context_message_refs(
    request_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Project ordered message/evidence identities without copying message text."""

    current_message = request_payload.get("current_message")
    current = current_message if isinstance(current_message, Mapping) else {}
    context_value = request_payload.get("message_context")
    context = context_value if isinstance(context_value, Mapping) else {}
    context_current = context.get("current")
    merged_current = dict(current)
    if isinstance(context_current, Mapping):
        merged_current.update(
            {key: value for key, value in context_current.items() if value is not None}
        )
    messages = context.get("messages")
    reply_chain = context.get("reply_chain")
    return {
        "chat_id": (
            int(current["chat_id"])
            if current.get("chat_id") is not None
            else None
        ),
        "current": _message_ref(merged_current),
        "messages": [
            _message_ref(item)
            for item in (messages if isinstance(messages, (list, tuple)) else ())
        ],
        "reply_chain": [
            _message_ref(item)
            for item in (
                reply_chain if isinstance(reply_chain, (list, tuple)) else ()
            )
        ],
    }


def build_request_component_sha256(
    request_payload: Mapping[str, Any],
) -> dict[str, str]:
    return {
        key: _sha256_canonical(request_payload.get(key))
        for key in REQUEST_COMPONENTS
    }


def rendered_prompt_sha256(
    *,
    system_prompt: str,
    request_payload: dict[str, Any],
) -> str:
    return _sha256_canonical(
        build_context_provider_messages(system_prompt, request_payload)
    )

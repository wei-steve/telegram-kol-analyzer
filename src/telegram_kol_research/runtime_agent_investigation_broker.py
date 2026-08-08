"""Audited declarative access to broad, structurally read-only evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit


BROAD_READ_ONLY_EVIDENCE_KINDS = frozenset(
    {
        "message_evidence",
        "database_projection",
        "processing_timeline",
        "journal_summary",
        "deployed_code",
        "configuration_state",
        "exchange_snapshot",
        "telegram_evidence",
        "prior_incidents",
    }
)
_SAFE_QUERY_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_SENSITIVE_MARKER = re.compile(
    r"(?:secret|token|credential|cookie|session|api.?key|password|passphrase|"
    r"authorization|private.?key|\.env(?:\b|$))",
    re.IGNORECASE,
)
_SQL_WRITE = re.compile(
    r"\b(?:insert|update|delete|replace|create|alter|drop|attach|detach|"
    r"vacuum|reindex|analyze|pragma|begin|commit|rollback|savepoint|release)\b",
    re.IGNORECASE,
)
_PROXY_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "proxy-authorization",
    }
)


class InvestigationDenied(ValueError):
    """A request or result violated the investigation authority boundary."""

    def __init__(self, denial_code: str):
        self.denial_code = denial_code
        super().__init__(denial_code)


@dataclass(frozen=True, slots=True)
class InvestigationRequest:
    incident_id: int
    evidence_kind: str
    object_ids: tuple[str, ...] = ()
    query: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    maximum_bytes: int = 8192


@dataclass(frozen=True, slots=True)
class InvestigationAuditRecord:
    incident_id: int
    evidence_kind: str
    arguments_fingerprint: str
    result_status: str
    evidence_reference: str | None
    result_bytes: int
    duration_ms: int
    denial_code: str | None
    created_at: datetime


def _canonical_arguments(request: InvestigationRequest) -> bytes:
    return json.dumps(
        {
            "evidence_kind": request.evidence_kind,
            "incident_id": request.incident_id,
            "maximum_bytes": request.maximum_bytes,
            "object_ids": list(request.object_ids),
            "query": request.query,
            "since": request.since.isoformat() if request.since else None,
            "until": request.until.isoformat() if request.until else None,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _contains_sensitive_material(value: Any, *, depth: int = 0) -> bool:
    if depth > 8:
        return True
    if isinstance(value, Mapping):
        return len(value) > 64 or any(
            _SENSITIVE_MARKER.search(str(key))
            or _contains_sensitive_material(nested, depth=depth + 1)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return len(value) > 100 or any(
            _contains_sensitive_material(item, depth=depth + 1) for item in value
        )
    if isinstance(value, str):
        return len(value) > 2048 or bool(_SENSITIVE_MARKER.search(value))
    return value is not None and not isinstance(value, (bool, int, float))


def _audited_evidence_kind(value: Any) -> str:
    normalized = str(value)
    return (
        normalized
        if normalized in BROAD_READ_ONLY_EVIDENCE_KINDS
        else "invalid"
    )


class InvestigationBroker:
    """Validate one closed evidence request, execute it, and audit the result."""

    def __init__(
        self,
        *,
        providers: Mapping[
            str, Callable[[InvestigationRequest], Mapping[str, Any]]
        ],
        incident_exists: Callable[[int], bool],
        audit_recorder: Callable[[InvestigationAuditRecord], None],
        clock: Callable[[], datetime],
    ) -> None:
        unknown = set(providers) - BROAD_READ_ONLY_EVIDENCE_KINDS
        if unknown:
            raise InvestigationDenied("evidence_kind_denied")
        self._providers = dict(providers)
        self._incident_exists = incident_exists
        self._audit_recorder = audit_recorder
        self._clock = clock

    def _validate(self, request: InvestigationRequest) -> None:
        if (
            isinstance(request.incident_id, bool)
            or not isinstance(request.incident_id, int)
            or request.incident_id < 1
        ):
            raise InvestigationDenied("incident_not_found")
        if not self._incident_exists(request.incident_id):
            raise InvestigationDenied("incident_not_found")
        if request.evidence_kind not in BROAD_READ_ONLY_EVIDENCE_KINDS:
            raise InvestigationDenied("evidence_kind_denied")
        if not 256 <= request.maximum_bytes <= 32_768:
            raise InvestigationDenied("bounds_invalid")
        if len(request.object_ids) > 32:
            raise InvestigationDenied("bounds_invalid")
        if any(
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or ".." in value
            or "/" in value
            or "\\" in value
            or _SENSITIVE_MARKER.search(value)
            for value in request.object_ids
        ):
            raise InvestigationDenied("sensitive_argument")
        if request.query is not None and not _SAFE_QUERY_NAME.fullmatch(
            request.query
        ):
            raise InvestigationDenied("query_denied")
        if (request.since is None) != (request.until is None):
            raise InvestigationDenied("bounds_invalid")
        if request.since is not None and request.until is not None:
            if (
                request.until < request.since
                or request.until - request.since > timedelta(days=31)
            ):
                raise InvestigationDenied("bounds_invalid")

    def execute(self, request: InvestigationRequest) -> dict[str, Any]:
        started = time.monotonic()
        fingerprint = hashlib.sha256(_canonical_arguments(request)).hexdigest()
        try:
            self._validate(request)
            provider = self._providers.get(request.evidence_kind)
            if provider is None:
                raise InvestigationDenied("provider_unavailable")
            raw = provider(request)
            if not isinstance(raw, Mapping) or set(raw) != {
                "data",
                "evidence_refs",
            }:
                raise InvestigationDenied("result_contract_invalid")
            references = raw["evidence_refs"]
            if (
                not isinstance(references, (list, tuple))
                or not 1 <= len(references) <= 32
                or any(
                    not isinstance(reference, str)
                    or not 1 <= len(reference) <= 255
                    or _SENSITIVE_MARKER.search(reference)
                    for reference in references
                )
            ):
                raise InvestigationDenied(
                    "sensitive_result"
                    if isinstance(references, (list, tuple))
                    and any(
                        isinstance(reference, str)
                        and _SENSITIVE_MARKER.search(reference)
                        for reference in references
                    )
                    else "result_contract_invalid"
                )
            if _contains_sensitive_material(raw["data"]):
                raise InvestigationDenied("sensitive_result")
            payload = {
                "data": raw["data"],
                "evidence_refs": list(references),
            }
            encoded = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > request.maximum_bytes:
                raise InvestigationDenied("result_too_large")
        except InvestigationDenied as exc:
            self._audit_recorder(
                InvestigationAuditRecord(
                    incident_id=(
                        request.incident_id
                        if isinstance(request.incident_id, int)
                        and not isinstance(request.incident_id, bool)
                        else 0
                    ),
                    evidence_kind=_audited_evidence_kind(request.evidence_kind),
                    arguments_fingerprint=fingerprint,
                    result_status="denied",
                    evidence_reference=None,
                    result_bytes=0,
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    denial_code=exc.denial_code,
                    created_at=self._clock(),
                )
            )
            raise
        except Exception:
            self._audit_recorder(
                InvestigationAuditRecord(
                    incident_id=(
                        request.incident_id
                        if isinstance(request.incident_id, int)
                        and not isinstance(request.incident_id, bool)
                        else 0
                    ),
                    evidence_kind=_audited_evidence_kind(request.evidence_kind),
                    arguments_fingerprint=fingerprint,
                    result_status="error",
                    evidence_reference=None,
                    result_bytes=0,
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    denial_code="provider_error",
                    created_at=self._clock(),
                )
            )
            raise InvestigationDenied("provider_error") from None
        self._audit_recorder(
            InvestigationAuditRecord(
                incident_id=request.incident_id,
                evidence_kind=request.evidence_kind,
                arguments_fingerprint=fingerprint,
                result_status="allowed",
                evidence_reference=str(references[0]),
                result_bytes=len(encoded),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                denial_code=None,
                created_at=self._clock(),
            )
        )
        return payload


def build_sqlalchemy_audit_recorder(session_factory):
    """Return an append-only recorder for bounded broker audit metadata."""

    from telegram_kol_research.models import RuntimeAgentInvestigationAudit

    def record(value: InvestigationAuditRecord) -> None:
        row = RuntimeAgentInvestigationAudit(
            runtime_incident_id=max(0, int(value.incident_id)),
            evidence_kind=str(value.evidence_kind)[:64] or "invalid",
            arguments_fingerprint=value.arguments_fingerprint,
            result_status=value.result_status,
            evidence_reference=(
                str(value.evidence_reference)[:255]
                if value.evidence_reference is not None
                else None
            ),
            result_bytes=max(0, min(int(value.result_bytes), 32_768)),
            duration_ms=max(0, min(int(value.duration_ms), 86_400_000)),
            denial_code=(
                str(value.denial_code)[:64]
                if value.denial_code is not None
                else None
            ),
            created_at=value.created_at,
        )
        with session_factory() as session:
            session.add(row)
            session.commit()

    return record


class SqliteReadOnlyEvidenceStore:
    """Execute bounded SELECT statements on an immutable query-only connection."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).resolve()

    @staticmethod
    def _authorizer(action, _arg1, _arg2, _database, _trigger):
        denied = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_PRAGMA,
        }
        return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK

    def select(self, statement: str, *, maximum_rows: int) -> list[dict[str, Any]]:
        normalized = statement.strip()
        if (
            not isinstance(statement, str)
            or not normalized
            or len(normalized) > 4096
            or ";" in normalized.rstrip(";")
            or _SQL_WRITE.search(normalized)
            or not re.match(r"^(?:select|with)\b", normalized, re.IGNORECASE)
            or not 1 <= int(maximum_rows) <= 100
        ):
            raise InvestigationDenied("database_write_denied")
        uri = f"file:{quote(str(self.database_path))}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.set_authorizer(self._authorizer)
            cursor = connection.execute(normalized)
            rows = cursor.fetchmany(int(maximum_rows) + 1)
            if len(rows) > int(maximum_rows):
                raise InvestigationDenied("result_too_large")
            return [dict(row) for row in rows]
        except InvestigationDenied:
            raise
        except sqlite3.Error as exc:
            raise InvestigationDenied("database_query_denied") from exc
        finally:
            if "connection" in locals():
                connection.close()


class ReadOnlyFileEvidenceReader:
    """Bounded reader for reviewed roots; it intentionally exposes no writes."""

    def __init__(
        self,
        *,
        allowed_roots: Sequence[str | Path],
        scratch_root: str | Path,
        maximum_bytes: int = 8192,
    ) -> None:
        self.allowed_roots = tuple(Path(root).resolve() for root in allowed_roots)
        self.scratch_root = Path(scratch_root).resolve()
        self.maximum_bytes = max(256, min(int(maximum_bytes), 32_768))

    def read_text(self, path: str | Path) -> str:
        target = Path(path).resolve()
        if not any(target.is_relative_to(root) for root in self.allowed_roots):
            raise InvestigationDenied("path_denied")
        if _SENSITIVE_MARKER.search(target.name):
            raise InvestigationDenied("credential_path_denied")
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise InvestigationDenied("path_unavailable") from exc
        if len(payload) > self.maximum_bytes:
            raise InvestigationDenied("result_too_large")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvestigationDenied("binary_path_denied") from exc


@dataclass(frozen=True, slots=True)
class NetworkReadPolicy:
    allowed_hosts: frozenset[str]
    deepcoin_read_paths: frozenset[str]

    def authorize(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> None:
        if str(method).upper() != "GET":
            raise InvestigationDenied("method_denied")
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise InvestigationDenied("scheme_denied")
        try:
            port = parsed.port
        except ValueError as exc:
            raise InvestigationDenied("port_denied") from exc
        if port not in (None, 443):
            raise InvestigationDenied("port_denied")
        if parsed.username or parsed.password:
            raise InvestigationDenied("credential_url_denied")
        host = (parsed.hostname or "").lower()
        if host == "api.telegram.org":
            raise InvestigationDenied("telegram_direct_access_denied")
        if host not in self.allowed_hosts:
            raise InvestigationDenied("host_denied")
        if any(str(key).lower() in _PROXY_HEADERS for key in headers):
            raise InvestigationDenied("proxy_denied")
        if host == "api.deepcoin.com" and parsed.path not in self.deepcoin_read_paths:
            raise InvestigationDenied("exchange_endpoint_denied")

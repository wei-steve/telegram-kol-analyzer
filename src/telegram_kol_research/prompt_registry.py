"""Versioned persistence for Web-managed AI prompts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, text
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    AiPromptDefinition,
    AiPromptInvocation,
    AiPromptTestRun,
    AiPromptVersion,
    utc_now,
)


class PromptRegistryError(RuntimeError):
    """Base error for prompt registry operations."""


class PromptRegistryNotFound(PromptRegistryError):
    """Raised when a definition or version does not exist."""


class PromptRegistryConflict(PromptRegistryError):
    """Raised when optimistic version checks fail."""


class PromptRegistryValidationError(PromptRegistryError):
    """Raised when prompt content or metadata is invalid."""


@dataclass(frozen=True)
class PromptSeed:
    prompt_key: str
    display_name: str
    description: str
    category: str
    consumers: tuple[str, ...]
    required_variables: tuple[str, ...]
    validation_profile: str
    content: str
    scope_chat_id: int | None = None
    enabled: bool = True


@dataclass(frozen=True)
class PromptVersionView:
    id: int
    version_number: int
    content: str
    status: str
    change_note: str | None
    source_version_id: int | None
    validated_at: object | None
    validation_result: dict | None
    created_at: object
    updated_at: object
    published_at: object | None


@dataclass(frozen=True)
class PromptDetail:
    definition_id: int
    prompt_key: str
    display_name: str
    description: str
    category: str
    scope_key: str
    scope_chat_id: int | None
    consumers: tuple[str, ...]
    required_variables: tuple[str, ...]
    validation_profile: str
    enabled: bool
    active_version: PromptVersionView
    draft_version: PromptVersionView | None
    history: tuple[PromptVersionView, ...]


@dataclass(frozen=True)
class ResolvedPrompt:
    definition_id: int
    prompt_key: str
    version_id: int
    version_number: int
    content: str
    scope_chat_id: int | None
    required_variables: tuple[str, ...]
    validation_profile: str


@dataclass(frozen=True)
class PromptInvocationRecord:
    feature: str
    correlation_key: str
    model: str
    prompt_versions: dict[str, int]
    status: str
    raw_message_id: int | None = None
    chat_id: int | None = None
    error_message: str | None = None


def _scope_key(chat_id: int | None) -> str:
    return f"chat:{chat_id}" if chat_id is not None else "global"


def _json_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False, sort_keys=True)


def _decode_tuple(value: str) -> tuple[str, ...]:
    decoded = json.loads(value or "[]")
    return tuple(str(item) for item in decoded)


def _version_view(row: AiPromptVersion) -> PromptVersionView:
    return PromptVersionView(
        id=row.id,
        version_number=row.version_number,
        content=row.content,
        status=row.status,
        change_note=row.change_note,
        source_version_id=row.source_version_id,
        validated_at=row.validated_at,
        validation_result=(
            json.loads(row.validation_result_json)
            if row.validation_result_json
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        published_at=row.published_at,
    )


def _load_detail(session, definition: AiPromptDefinition) -> PromptDetail:
    versions = (
        session.query(AiPromptVersion)
        .filter(AiPromptVersion.prompt_definition_id == definition.id)
        .order_by(AiPromptVersion.version_number.desc())
        .all()
    )
    by_id = {row.id: row for row in versions}
    active = by_id.get(definition.active_version_id)
    if active is None:
        raise PromptRegistryValidationError(
            f"prompt {definition.prompt_key} has no active version"
        )
    draft = next((row for row in versions if row.status == "draft"), None)
    return PromptDetail(
        definition_id=definition.id,
        prompt_key=definition.prompt_key,
        display_name=definition.display_name,
        description=definition.description,
        category=definition.category,
        scope_key=definition.scope_key,
        scope_chat_id=definition.scope_chat_id,
        consumers=_decode_tuple(definition.consumers_json),
        required_variables=_decode_tuple(definition.required_variables_json),
        validation_profile=definition.validation_profile,
        enabled=definition.enabled,
        active_version=_version_view(active),
        draft_version=_version_view(draft) if draft is not None else None,
        history=tuple(_version_view(row) for row in versions),
    )


def _get_definition(session, prompt_key: str, chat_id: int | None) -> AiPromptDefinition:
    row = (
        session.query(AiPromptDefinition)
        .filter(AiPromptDefinition.prompt_key == prompt_key)
        .filter(AiPromptDefinition.scope_key == _scope_key(chat_id))
        .one_or_none()
    )
    if row is None:
        raise PromptRegistryNotFound(
            f"prompt definition not found: {prompt_key} ({_scope_key(chat_id)})"
        )
    return row


def seed_prompt_definition(
    session_factory: sessionmaker,
    seed: PromptSeed,
) -> PromptDetail:
    content = seed.content.strip()
    if not content:
        raise PromptRegistryValidationError("prompt content is required")
    now = utc_now()
    with session_factory() as session:
        existing = (
            session.query(AiPromptDefinition)
            .filter(AiPromptDefinition.prompt_key == seed.prompt_key)
            .filter(AiPromptDefinition.scope_key == _scope_key(seed.scope_chat_id))
            .one_or_none()
        )
        if existing is not None:
            return _load_detail(session, existing)
        definition = AiPromptDefinition(
            prompt_key=seed.prompt_key,
            display_name=seed.display_name,
            description=seed.description,
            category=seed.category,
            scope_key=_scope_key(seed.scope_chat_id),
            scope_chat_id=seed.scope_chat_id,
            consumers_json=_json_list(seed.consumers),
            required_variables_json=_json_list(seed.required_variables),
            validation_profile=seed.validation_profile,
            enabled=seed.enabled,
            created_at=now,
            updated_at=now,
        )
        session.add(definition)
        session.flush()
        version = AiPromptVersion(
            prompt_definition_id=definition.id,
            version_number=1,
            content=content,
            status="published",
            change_note="Initial registry seed",
            created_at=now,
            updated_at=now,
            published_at=now,
        )
        session.add(version)
        session.flush()
        definition.active_version_id = version.id
        session.commit()
        return _load_detail(session, definition)


def upgrade_seeded_prompt_definition(
    session_factory: sessionmaker,
    seed: PromptSeed,
    *,
    previous_content: str,
    change_note: str,
) -> PromptDetail:
    """Publish a new default only while the exact prior seed is still active."""

    normalized = seed.content.strip()
    previous = previous_content.strip()
    if not normalized or not previous or normalized == previous:
        raise PromptRegistryValidationError("prompt seed upgrade is invalid")
    current = get_prompt_detail(
        session_factory,
        seed.prompt_key,
        chat_id=seed.scope_chat_id,
    )
    if (
        current.active_version.content.strip() == normalized
        or current.active_version.content.strip() != previous
        or current.draft_version is not None
    ):
        return current
    now = utc_now()
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        definition = _get_definition(session, seed.prompt_key, seed.scope_chat_id)
        active = session.get(AiPromptVersion, definition.active_version_id)
        if active is None:
            raise PromptRegistryValidationError(
                f"prompt {definition.prompt_key} has no active version"
            )
        if active.content.strip() == normalized:
            session.commit()
            return _load_detail(session, definition)
        draft_exists = (
            session.query(AiPromptVersion.id)
            .filter(AiPromptVersion.prompt_definition_id == definition.id)
            .filter(AiPromptVersion.status == "draft")
            .first()
            is not None
        )
        if active.content.strip() != previous or draft_exists:
            session.commit()
            return _load_detail(session, definition)

        from telegram_kol_research.prompt_composition import (
            validate_prompt_content,
        )

        validation = validate_prompt_content(
            definition.prompt_key,
            normalized,
            validation_profile=definition.validation_profile,
            required_variables=_decode_tuple(definition.required_variables_json),
        )
        if not validation.success:
            raise PromptRegistryValidationError(
                "seeded prompt upgrade failed validation: "
                + "; ".join(validation.errors)
            )
        max_version = (
            session.query(func.max(AiPromptVersion.version_number))
            .filter(AiPromptVersion.prompt_definition_id == definition.id)
            .scalar()
            or 0
        )
        active.status = "superseded"
        active.updated_at = now
        published = AiPromptVersion(
            prompt_definition_id=definition.id,
            version_number=max_version + 1,
            content=normalized,
            status="published",
            change_note=change_note.strip(),
            source_version_id=active.id,
            validated_at=now,
            validation_result_json=json.dumps(
                {"success": True, "errors": []},
                ensure_ascii=False,
                sort_keys=True,
            ),
            created_at=now,
            updated_at=now,
            published_at=now,
        )
        session.add(published)
        session.flush()
        definition.active_version_id = published.id
        definition.updated_at = now
        session.commit()
        return _load_detail(session, definition)


def list_prompt_definitions(
    session_factory: sessionmaker,
    *,
    chat_id: int | None = None,
) -> list[PromptDetail]:
    with session_factory() as session:
        query = session.query(AiPromptDefinition)
        if chat_id is not None:
            query = query.filter(
                AiPromptDefinition.scope_key.in_(["global", _scope_key(chat_id)])
            )
        definitions = query.order_by(
            AiPromptDefinition.category,
            AiPromptDefinition.prompt_key,
            AiPromptDefinition.scope_key,
        ).all()
        return [_load_detail(session, definition) for definition in definitions]


def get_prompt_detail(
    session_factory: sessionmaker,
    prompt_key: str,
    *,
    chat_id: int | None = None,
) -> PromptDetail:
    with session_factory() as session:
        return _load_detail(session, _get_definition(session, prompt_key, chat_id))


def resolve_active_prompt(
    session_factory: sessionmaker,
    prompt_key: str,
    *,
    chat_id: int | None = None,
) -> ResolvedPrompt:
    detail = get_prompt_detail(session_factory, prompt_key, chat_id=chat_id)
    return ResolvedPrompt(
        definition_id=detail.definition_id,
        prompt_key=detail.prompt_key,
        version_id=detail.active_version.id,
        version_number=detail.active_version.version_number,
        content=detail.active_version.content,
        scope_chat_id=detail.scope_chat_id,
        required_variables=detail.required_variables,
        validation_profile=detail.validation_profile,
    )


def save_prompt_draft(
    session_factory: sessionmaker,
    prompt_key: str,
    *,
    content: str,
    change_note: str,
    chat_id: int | None = None,
    expected_active_version_id: int | None = None,
    expected_draft_updated_at: datetime | None = None,
) -> PromptDetail:
    normalized = content.strip()
    if not normalized:
        raise PromptRegistryValidationError("prompt content is required")
    now = utc_now()
    with session_factory() as session:
        definition = _get_definition(session, prompt_key, chat_id)
        if (
            expected_active_version_id is not None
            and definition.active_version_id != expected_active_version_id
        ):
            raise PromptRegistryConflict("active version changed")
        draft = (
            session.query(AiPromptVersion)
            .filter(AiPromptVersion.prompt_definition_id == definition.id)
            .filter(AiPromptVersion.status == "draft")
            .one_or_none()
        )
        if draft is None:
            if expected_draft_updated_at is not None:
                raise PromptRegistryConflict("draft version changed")
            max_version = (
                session.query(func.max(AiPromptVersion.version_number))
                .filter(AiPromptVersion.prompt_definition_id == definition.id)
                .scalar()
                or 0
            )
            draft = AiPromptVersion(
                prompt_definition_id=definition.id,
                version_number=max_version + 1,
                content=normalized,
                status="draft",
                change_note=change_note.strip() or None,
                created_at=now,
                updated_at=now,
            )
            session.add(draft)
        else:
            if (
                expected_draft_updated_at is None
                or draft.updated_at != expected_draft_updated_at
            ):
                raise PromptRegistryConflict("draft version changed")
            content_changed = draft.content != normalized
            draft.content = normalized
            draft.change_note = change_note.strip() or None
            draft.updated_at = now
            if content_changed:
                draft.validated_at = None
                draft.validation_result_json = None
                session.query(AiPromptTestRun).filter(
                    AiPromptTestRun.draft_version_id == draft.id
                ).delete(synchronize_session=False)
        definition.updated_at = now
        session.commit()
        return _load_detail(session, definition)


def publish_prompt_draft(
    session_factory: sessionmaker,
    prompt_key: str,
    *,
    expected_draft_version_id: int,
    expected_active_version_id: int,
    chat_id: int | None = None,
) -> PromptDetail:
    now = utc_now()
    with session_factory() as session:
        definition = _get_definition(session, prompt_key, chat_id)
        if definition.active_version_id != expected_active_version_id:
            raise PromptRegistryConflict("active version changed")
        draft = (
            session.query(AiPromptVersion)
            .filter(AiPromptVersion.prompt_definition_id == definition.id)
            .filter(AiPromptVersion.status == "draft")
            .one_or_none()
        )
        if draft is None or draft.id != expected_draft_version_id:
            raise PromptRegistryConflict("draft version changed")
        if not (draft.change_note or "").strip():
            raise PromptRegistryValidationError("change note is required")
        validation_result = (
            json.loads(draft.validation_result_json)
            if draft.validation_result_json
            else None
        )
        if draft.validated_at is None or not validation_result or not validation_result.get("success"):
            raise PromptRegistryConflict("draft requires successful validation")
        if definition.validation_profile == "mimo_v2_authoritative":
            from telegram_kol_research.prompt_composition import (
                validate_prompt_content,
            )

            validation = validate_prompt_content(
                definition.prompt_key,
                draft.content,
                validation_profile=definition.validation_profile,
                required_variables=_decode_tuple(definition.required_variables_json),
            )
            if not validation.success:
                raise PromptRegistryValidationError(
                    "MiMo v2 draft failed closed-contract validation: "
                    + "; ".join(validation.errors)
                )
        active = session.get(AiPromptVersion, definition.active_version_id)
        if active is not None:
            active.status = "superseded"
            active.updated_at = now
        draft.status = "published"
        draft.published_at = now
        draft.updated_at = now
        definition.active_version_id = draft.id
        definition.updated_at = now
        session.commit()
        return _load_detail(session, definition)


def record_prompt_validation(
    session_factory: sessionmaker,
    prompt_key: str,
    *,
    expected_draft_version_id: int,
    success: bool,
    errors: tuple[str, ...],
    chat_id: int | None = None,
) -> PromptDetail:
    now = utc_now()
    with session_factory() as session:
        definition = _get_definition(session, prompt_key, chat_id)
        draft = (
            session.query(AiPromptVersion)
            .filter(AiPromptVersion.prompt_definition_id == definition.id)
            .filter(AiPromptVersion.status == "draft")
            .one_or_none()
        )
        if draft is None or draft.id != expected_draft_version_id:
            raise PromptRegistryConflict("draft version changed")
        draft.validated_at = now
        draft.validation_result_json = json.dumps(
            {"success": success, "errors": list(errors)},
            ensure_ascii=False,
            sort_keys=True,
        )
        draft.updated_at = now
        definition.updated_at = now
        session.commit()
        return _load_detail(session, definition)


def rollback_prompt(
    session_factory: sessionmaker,
    prompt_key: str,
    *,
    source_version_id: int,
    change_note: str,
    expected_active_version_id: int,
    chat_id: int | None = None,
) -> PromptDetail:
    if not change_note.strip():
        raise PromptRegistryValidationError("change note is required")
    now = utc_now()
    with session_factory() as session:
        definition = _get_definition(session, prompt_key, chat_id)
        if definition.active_version_id != expected_active_version_id:
            raise PromptRegistryConflict("active version changed")
        source = session.get(AiPromptVersion, source_version_id)
        if source is None or source.prompt_definition_id != definition.id:
            raise PromptRegistryNotFound("rollback source version not found")
        if source.status not in {"published", "superseded"}:
            raise PromptRegistryValidationError("rollback source must be published")
        max_version = (
            session.query(func.max(AiPromptVersion.version_number))
            .filter(AiPromptVersion.prompt_definition_id == definition.id)
            .scalar()
            or 0
        )
        active = session.get(AiPromptVersion, definition.active_version_id)
        if active is not None:
            active.status = "superseded"
            active.updated_at = now
        restored = AiPromptVersion(
            prompt_definition_id=definition.id,
            version_number=max_version + 1,
            content=source.content,
            status="published",
            change_note=change_note.strip(),
            source_version_id=source.id,
            created_at=now,
            updated_at=now,
            published_at=now,
        )
        session.add(restored)
        session.flush()
        definition.active_version_id = restored.id
        definition.updated_at = now
        session.commit()
        return _load_detail(session, definition)


def record_prompt_invocation(
    session_factory: sessionmaker,
    record: PromptInvocationRecord,
) -> None:
    with session_factory() as session:
        session.add(
            AiPromptInvocation(
                feature=record.feature,
                correlation_key=record.correlation_key,
                raw_message_id=record.raw_message_id,
                chat_id=record.chat_id,
                model=record.model,
                prompt_versions_json=json.dumps(
                    record.prompt_versions,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                status=record.status,
                error_message=record.error_message,
            )
        )
        session.commit()

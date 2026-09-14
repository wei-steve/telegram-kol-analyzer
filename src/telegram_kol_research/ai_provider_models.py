"""Ask a provider which models its key can call.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §9.2.

The preset catalogue says what models.dev knew on the day it was generated;
this says what *this account* can reach right now, which is the only answer
that matters when someone is about to bind a model to a production stage.

``GET {base}/models`` is the OpenAI-compatible listing. The Anthropic
compatibility layer wants ``x-api-key`` and ``anthropic-version`` instead of a
bearer token, so both sets of headers are sent every time: a provider that
does not know them ignores them, and sending both is cheaper than keeping a
table of which provider needs which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from telegram_kol_research.ai_endpoints import models_url, provider_append_v1


#: Long enough for a cold aggregator, short enough that a person clicking a
#: button does not think the page has hung.
MODEL_LIST_TIMEOUT_SECONDS = 15.0

#: The Anthropic compatibility layer pins this; other providers ignore it.
ANTHROPIC_VERSION = "2023-06-01"

#: A listing this long is an aggregator dumping its whole catalogue; the page
#: has to stay usable, and nobody scrolls past this many anyway.
MAX_LISTED_MODELS = 500


@dataclass(frozen=True, slots=True)
class ProviderModelListing:
    """What one provider answered, or why it did not."""

    models: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    failure_class: str | None = None
    kind: str | None = None
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _rows(payload: Any) -> list[dict[str, Any]]:
    """The model rows, from either OpenAI's ``{data: [...]}`` or a bare list."""

    if isinstance(payload, dict):
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    listed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            model_id, owned_by = row, ""
        elif isinstance(row, dict):
            model_id = str(row.get("id") or row.get("name") or "")
            owned_by = str(row.get("owned_by") or row.get("owner") or "")
        else:
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        listed.append({"id": model_id, "owned_by": owned_by})
        if len(listed) >= MAX_LISTED_MODELS:
            break
    listed.sort(key=lambda item: item["id"])
    return listed


def list_provider_models(
    provider: Any,
    *,
    client_factory: Callable[..., Any] = httpx.Client,
    timeout_seconds: float = MODEL_LIST_TIMEOUT_SECONDS,
) -> ProviderModelListing:
    """One ``GET {base}/models``, classified the way every other call is.

    ``provider`` is an :class:`ai_stage_catalog.AiProvider`. Failures are named
    by :func:`mimo_provider_health.classify_provider_failure`, so "your key is
    wrong" and "this host does not answer" stay different sentences on the
    page, exactly as they are in the outage alerts.
    """

    from telegram_kol_research.mimo_provider_health import (
        RESPONSE_INVALID,
        classify_provider_failure,
    )

    url = models_url(getattr(provider, "base_url", ""), provider_append_v1(provider))
    if not url:
        return ProviderModelListing(
            error="provider has no base_url", failure_class="not_configured"
        )
    api_key = str(getattr(provider, "api_key", "") or "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    http_status: int | None = None
    try:
        with client_factory(timeout=timeout_seconds) as client:
            response = client.get(url, headers=headers)
            http_status = int(response.status_code)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - every failure is reportable
        failure = classify_provider_failure(exc)
        failure_class = failure.failure_class if failure is not None else None
        if failure_class is None and isinstance(exc, ValueError):
            failure_class = RESPONSE_INVALID
        return ProviderModelListing(
            error=str(exc) or type(exc).__name__,
            failure_class=failure_class,
            kind=failure.kind if failure is not None else None,
            http_status=(
                failure.http_status
                if failure is not None and failure.http_status is not None
                else http_status
            ),
        )
    models = _rows(payload)
    if not models:
        return ProviderModelListing(
            error="provider listed no models",
            failure_class=RESPONSE_INVALID,
            http_status=http_status,
        )
    return ProviderModelListing(models=models, http_status=http_status)

"""One rule for turning a provider's base URL into an endpoint.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §9.3.

Seven call sites used to join this URL themselves, under three different
rules, and none of them worked for the providers phase 6 adds:

* ``message_recognition`` / ``context_resolution`` /
  ``semantic_disagreement_review``: "append ``/chat/completions`` when the base
  ends in ``/v1``, else ``/v1/chat/completions``".
* ``strategy_alerts`` / ``llm_chat``: always ``/v1/chat/completions``.
* The MiMo direct call and the daily probe: always ``/chat/completions``.

Every one of those gets Gemini (``/v1beta/openai``), Groq (``/openai/v1``),
Bailian (``/compatible-mode/v1``), Volcengine (``/api/v3``) or Zhipu
(``/api/paas/v4``) wrong -- by inserting a second ``/v1`` that is not there, or
by dropping the one that is.

**The rule**: a base URL whose path is empty or bare ``/`` gets
``/v1/chat/completions``; anything else already names its API root, so it only
gets ``/chat/completions``. A base that already ends in ``/chat/completions``
is left alone.

That keeps every URL this project sends today byte-identical --
``https://api.deepseek.com`` → ``/v1/chat/completions``,
``https://api.xiaomimimo.com/v1`` → ``/v1/chat/completions``,
``http://127.0.0.1:8317`` → ``/v1/chat/completions`` -- and gets the new ones
right. A test pins every base URL in the §9.1 table.
"""

from __future__ import annotations

from urllib.parse import urlsplit


CHAT_COMPLETIONS_PATH = "chat/completions"
MODELS_PATH = "models"


def _join(base_url: str, suffix: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith(f"/{suffix}"):
        return normalized
    path = urlsplit(normalized).path
    # No path at all means the host is the whole base URL, so it cannot be an
    # API root: the version segment has to be supplied.
    if path in ("", "/"):
        return f"{normalized}/v1/{suffix}"
    return f"{normalized}/{suffix}"


def chat_completions_url(base_url: str) -> str:
    """The OpenAI-compatible chat completions endpoint for one provider."""

    return _join(base_url, CHAT_COMPLETIONS_PATH)


def models_url(base_url: str) -> str:
    """The OpenAI-compatible model listing endpoint for one provider."""

    return _join(base_url, MODELS_PATH)

"""One rule for turning a provider's base URL into an endpoint.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §9.3.

Seven call sites used to join this URL themselves, under three different
rules, and none of them worked for the providers phase 6 adds:

* ``message_recognition`` / ``context_resolution``: "append
  ``/chat/completions`` when the base ends in ``/v1``, else
  ``/v1/chat/completions``".
* ``strategy_alerts`` / ``llm_chat``: always ``/v1/chat/completions``.
* The MiMo direct call and the daily probe: always ``/chat/completions``.

Every one of those gets Gemini (``/v1beta/openai``), Groq (``/openai/v1``),
Bailian (``/compatible-mode/v1``), Volcengine (``/api/v3``) or Zhipu
(``/api/paas/v4``) wrong -- by inserting a second ``/v1`` that is not there, or
by dropping the one that is.

**The rule**: a provider says whether its base URL needs a ``/v1`` appended
(``append_v1``, the switch OpenMinis calls ``appendV1Suffix``). ``True`` means
"I only filled in the host"; ``False`` means "this is already the API root".
A base that already ends in ``/v1`` is not given a second one, and a base that
already ends in ``/chat/completions`` is left alone entirely.

``append_v1=None`` -- an older configuration, or a caller that has no provider
record -- falls back to :func:`infer_append_v1`, which is the rule this module
shipped with: a base URL with no path is a bare host and gets its ``/v1``.
So every existing file keeps producing the URL it produced before anybody saw
the switch, and saving writes the inferred value down so it stops being a
guess.

Either way every URL this project sends today is byte-identical --
``https://api.deepseek.com`` → ``/v1/chat/completions``,
``https://api.xiaomimimo.com/v1`` → ``/v1/chat/completions``,
``http://127.0.0.1:8317`` → ``/v1/chat/completions`` -- and the providers whose
root is not ``/v1`` are right. A test pins every base URL in the §9.1 table.
"""

from __future__ import annotations

from urllib.parse import urlsplit


CHAT_COMPLETIONS_PATH = "chat/completions"
MODELS_PATH = "models"


def infer_append_v1(base_url: str) -> bool:
    """What the switch would have been before anyone set it.

    The rule this module shipped with, now expressed as a default rather than
    a law: a base URL with no path is a bare host and needs its version
    segment; anything else already names its API root.
    """

    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return False
    return urlsplit(normalized).path in ("", "/")


def _join(base_url: str, suffix: str, append_v1: bool | None) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith(f"/{suffix}"):
        return normalized
    if append_v1 is None:
        append_v1 = infer_append_v1(normalized)
    if append_v1 and not urlsplit(normalized).path.rstrip("/").endswith("/v1"):
        normalized = f"{normalized}/v1"
    return f"{normalized}/{suffix}"


def provider_append_v1(provider: object) -> bool | None:
    """One provider's switch, or ``None`` when it does not carry one.

    Several call sites are handed a provider-shaped object rather than an
    :class:`ai_stage_catalog.AiProvider` -- a stand-in built in a test, a
    record from code that predates the switch. ``None`` means "nobody said",
    which :func:`chat_completions_url` resolves to the inferred rule, so those
    callers keep sending exactly the URL they always sent.
    """

    value = getattr(provider, "append_v1", None)
    return None if value is None else bool(value)


def chat_completions_url(base_url: str, append_v1: bool | None = None) -> str:
    """The OpenAI-compatible chat completions endpoint for one provider."""

    return _join(base_url, CHAT_COMPLETIONS_PATH, append_v1)


def models_url(base_url: str, append_v1: bool | None = None) -> str:
    """The OpenAI-compatible model listing endpoint for one provider."""

    return _join(base_url, MODELS_PATH, append_v1)

"""One URL rule, pinned against every provider phase 6 can configure.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §9.3.

Half of these base URLs name their API root with something other than ``/v1``
-- ``/v1beta/openai``, ``/openai/v1``, ``/compatible-mode/v1``, ``/api/v3``,
``/api/paas/v4`` -- and each of the three rules this module replaced got some
of them wrong. The other half are the URLs this project already sends, and
those must come out byte-identical.
"""

from __future__ import annotations

import inspect

import pytest

from telegram_kol_research import (
    context_resolution,
    llm_chat,
    message_recognition,
    mimo_provider_probe,
    recognition_experiments,
    semantic_disagreement_review,
    strategy_alerts,
)
from telegram_kol_research.ai_endpoints import chat_completions_url, models_url


# The §9.1 preset table, plus the three base URLs already in production.
BASE_URLS = {
    # Already in use: these three must not move.
    "https://api.deepseek.com": "https://api.deepseek.com/v1",
    "https://api.xiaomimimo.com/v1": "https://api.xiaomimimo.com/v1",
    "http://127.0.0.1:8317": "http://127.0.0.1:8317/v1",
    # 国内
    "https://open.bigmodel.cn/api/paas/v4": "https://open.bigmodel.cn/api/paas/v4",
    "https://dashscope.aliyuncs.com/compatible-mode/v1": (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ),
    "https://api.moonshot.cn/v1": "https://api.moonshot.cn/v1",
    "https://api.siliconflow.cn/v1": "https://api.siliconflow.cn/v1",
    "https://api.stepfun.com/v1": "https://api.stepfun.com/v1",
    "https://api.minimaxi.com/v1": "https://api.minimaxi.com/v1",
    "https://ark.cn-beijing.volces.com/api/v3": (
        "https://ark.cn-beijing.volces.com/api/v3"
    ),
    # 国际
    "https://api.openai.com/v1": "https://api.openai.com/v1",
    "https://api.anthropic.com/v1": "https://api.anthropic.com/v1",
    "https://generativelanguage.googleapis.com/v1beta/openai": (
        "https://generativelanguage.googleapis.com/v1beta/openai"
    ),
    "https://api.x.ai/v1": "https://api.x.ai/v1",
    "https://openrouter.ai/api/v1": "https://openrouter.ai/api/v1",
    "https://api.groq.com/openai/v1": "https://api.groq.com/openai/v1",
    "https://api.mistral.ai/v1": "https://api.mistral.ai/v1",
    # 本地
    "http://127.0.0.1:11434/v1": "http://127.0.0.1:11434/v1",
    "http://127.0.0.1:1234/v1": "http://127.0.0.1:1234/v1",
}


@pytest.mark.parametrize("base_url,root", sorted(BASE_URLS.items()))
def test_every_preset_base_url_keeps_the_api_root_it_declares(base_url, root):
    assert chat_completions_url(base_url) == f"{root}/chat/completions"
    assert models_url(base_url) == f"{root}/models"


@pytest.mark.parametrize(
    "base_url,expected",
    [
        # The three URLs production sends today, spelled out rather than
        # derived, so a change to the rule cannot quietly move them.
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
        (
            "https://api.xiaomimimo.com/v1",
            "https://api.xiaomimimo.com/v1/chat/completions",
        ),
        ("http://127.0.0.1:8317", "http://127.0.0.1:8317/v1/chat/completions"),
    ],
)
def test_the_urls_already_in_production_are_unchanged(base_url, expected):
    assert chat_completions_url(base_url) == expected


def test_a_trailing_slash_does_not_change_the_answer():
    assert chat_completions_url("https://api.deepseek.com/") == chat_completions_url(
        "https://api.deepseek.com"
    )
    assert chat_completions_url("https://api.groq.com/openai/v1/") == (
        "https://api.groq.com/openai/v1/chat/completions"
    )


def test_a_base_that_already_names_the_endpoint_is_left_alone():
    full = "https://api.example.com/v1/chat/completions"

    assert chat_completions_url(full) == full
    assert models_url("https://api.example.com/v1/models") == (
        "https://api.example.com/v1/models"
    )


def test_an_empty_base_url_produces_nothing_to_call():
    assert chat_completions_url("") == ""
    assert chat_completions_url("   ") == ""
    assert models_url("") == ""


def test_a_port_alone_is_not_read_as_a_path():
    """``http://host:8317`` has no path, so it still needs its ``/v1``."""

    assert chat_completions_url("http://localhost:1234") == (
        "http://localhost:1234/v1/chat/completions"
    )


# ---------------------------------------------------------------------------
# Every call site goes through the rule
# ---------------------------------------------------------------------------


def test_no_call_site_still_joins_this_url_by_hand():
    modules = (
        context_resolution,
        llm_chat,
        message_recognition,
        mimo_provider_probe,
        recognition_experiments,
        semantic_disagreement_review,
        strategy_alerts,
    )
    offenders = []
    for module in modules:
        source = inspect.getsource(module)
        for fragment in ("/v1/chat/completions", "/chat/completions"):
            # The rule itself lives in ai_endpoints; nobody else builds it.
            if f'{fragment}"' in source.replace(
                'endswith("/chat/completions")', ""
            ):
                offenders.append((module.__name__, fragment))
    assert offenders == []


@pytest.mark.parametrize(
    "helper",
    [
        message_recognition._chat_completions_url,
        semantic_disagreement_review._chat_completions_url,
        context_resolution._completion_url,
    ],
)
def test_the_three_surviving_helpers_are_the_shared_rule(helper):
    for base_url in BASE_URLS:
        assert helper(base_url) == chat_completions_url(base_url)

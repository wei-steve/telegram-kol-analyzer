import httpx
import pytest

from telegram_kol_research.llm_chat import (
    LLMProxyConfig,
    build_proxy_chat_payload,
    request_grounded_chat_answer,
)


def test_build_proxy_chat_payload_matches_openai_compatible_shape():
    payload = build_proxy_chat_payload(
        question="Summarize this group",
        scope_context="message context",
        model="gpt-test",
        group_prompt="Prioritize recent changes",
    )

    assert payload["model"] == "gpt-test"
    assert payload["messages"][0]["role"] == "system"
    assert (
        payload["messages"][1]["content"] == "Group prompt:\nPrioritize recent changes"
    )
    assert payload["messages"][-1]["content"] == "Summarize this group"


def test_request_grounded_chat_answer_reads_openai_compatible_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://proxy.test/v1/chat/completions")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Grounded answer [1]",
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    answer = request_grounded_chat_answer(
        config=LLMProxyConfig(
            base_url="http://proxy.test",
            api_key="secret",
            model="gpt-test",
            timeout_seconds=5,
        ),
        question="Summarize this group",
        scope_context="message context",
        client=httpx.Client(transport=transport),
    )

    assert answer == "Grounded answer [1]"


def test_request_grounded_chat_answer_raises_for_image_input_error_text_in_success_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://proxy.test/v1/chat/completions")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": 'ERROR: Cannot read "image.png" (this model does not support image input). Inform the user.',
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)

    with pytest.raises(httpx.HTTPError) as exc_info:
        request_grounded_chat_answer(
            config=LLMProxyConfig(
                base_url="http://proxy.test",
                api_key="secret",
                model="gpt-test",
                timeout_seconds=5,
            ),
            question="Summarize this group",
            scope_context="message context",
            client=httpx.Client(transport=transport),
        )

    assert "does not support image input" in str(exc_info.value).lower()

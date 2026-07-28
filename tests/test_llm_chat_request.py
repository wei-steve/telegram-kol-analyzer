import httpx
import pytest

from telegram_kol_research.llm_chat import (
    LLMProxyConfig,
    build_proxy_chat_payload,
    load_llm_proxy_config,
    request_grounded_chat_answer,
    request_structured_chat_turn,
)


def test_build_proxy_chat_payload_matches_openai_compatible_shape():
    payload = build_proxy_chat_payload(
        question="Summarize this group",
        scope_context="message context",
        model="gpt-test",
        system_prompt="Published research system",
        group_prompt="Prioritize recent changes",
    )

    assert payload["model"] == "gpt-test"
    assert payload["messages"][0] == {
        "role": "system",
        "content": "Published research system",
    }
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
        system_prompt="Published research system",
        client=httpx.Client(transport=transport),
    )

    assert answer == "Grounded answer [1]"


def test_request_structured_chat_turn_normalizes_one_tool_call():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        assert '"tool_choice":"auto"' in payload
        assert '"parallel_tool_calls":false' in payload
        assert '"name":"get_incident_summary"' in payload
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_incident_summary",
                                        "arguments": '{"incident_id":17}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    turn = request_structured_chat_turn(
        config=LLMProxyConfig(
            base_url="http://proxy.test",
            api_key="secret",
            model="gpt-test",
            timeout_seconds=5,
        ),
        messages=[{"role": "system", "content": "Read-only diagnosis."}],
        tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": "get_incident_summary",
                    "parameters": {"type": "object"},
                },
            }
        ],
        timeout_seconds=3,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert turn == {
        "tool_call": {
            "id": "call-1",
            "name": "get_incident_summary",
            "arguments": {"incident_id": 17},
        }
    }


def test_request_structured_chat_turn_normalizes_closed_final_json():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        assert '"tools"' not in payload
        assert '"tool_choice"' not in payload
        assert '"parallel_tool_calls"' not in payload
        assert '"response_format":{"type":"json_object"}' in payload
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"incident_id":17,"confidence":"low"}'
                        }
                    }
                ]
            },
        )

    turn = request_structured_chat_turn(
        config=LLMProxyConfig(
            base_url="http://proxy.test",
            api_key="",
            model="gpt-test",
            timeout_seconds=5,
        ),
        messages=[{"role": "system", "content": "Read-only diagnosis."}],
        tool_schemas=[],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert turn == {"final": {"incident_id": 17, "confidence": "low"}}


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
            system_prompt="Published research system",
            client=httpx.Client(transport=transport),
        )

    assert "does not support image input" in str(exc_info.value).lower()


def test_load_llm_proxy_config_reads_local_env_file(tmp_path):
    env_file = tmp_path / "llm.env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_KOL_LLM_BASE_URL=http://proxy.local:9000",
                "TELEGRAM_KOL_LLM_API_KEY=test-key",
                "TELEGRAM_KOL_LLM_MODEL=gpt-test",
                "TELEGRAM_KOL_LLM_TIMEOUT_SECONDS=12",
            ]
        ),
        encoding="utf-8",
    )

    config = load_llm_proxy_config(environ={}, env_file_paths=[env_file])

    assert config.base_url == "http://proxy.local:9000"
    assert config.api_key == "test-key"
    assert config.model == "gpt-test"
    assert config.timeout_seconds == 12.0


def test_request_grounded_chat_answer_retries_with_supported_proxy_model():
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("http://proxy.test/v1/chat/completions"):
            payload = request.read().decode("utf-8")
            requests.append(("chat", payload))
            if '"model":"gpt-4.1-mini"' in payload:
                return httpx.Response(
                    502,
                    request=request,
                    json={
                        "error": {
                            "message": "unknown provider for model gpt-4.1-mini"
                        }
                    },
                )
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "Fallback answer [1]",
                            }
                        }
                    ]
                },
            )

        if request.url == httpx.URL("http://proxy.test/v1/models"):
            requests.append(("models", ""))
            return httpx.Response(
                200,
                request=request,
                json={
                    "data": [
                        {"id": "gpt-5.4-mini"},
                        {"id": "gpt-5.4"},
                    ]
                },
            )

        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    config = LLMProxyConfig(
        base_url="http://proxy.test",
        api_key="secret",
        model="gpt-4.1-mini",
        timeout_seconds=5,
    )

    answer = request_grounded_chat_answer(
        config=config,
        question="Summarize this group",
        scope_context="message context",
        system_prompt="Published research system",
        client=httpx.Client(transport=transport),
    )

    assert answer == "Fallback answer [1]"
    assert config.model == "gpt-5.4-mini"
    assert requests == [
        ("chat", '{"model":"gpt-4.1-mini","messages":[{"role":"system","content":"Published research system"},{"role":"user","content":"Source context:\\nmessage context"},{"role":"user","content":"Summarize this group"}]}'),
        ("models", ""),
        ("chat", '{"model":"gpt-5.4-mini","messages":[{"role":"system","content":"Published research system"},{"role":"user","content":"Source context:\\nmessage context"},{"role":"user","content":"Summarize this group"}]}'),
    ]

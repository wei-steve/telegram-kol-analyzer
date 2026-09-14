import httpx
import pytest
import telegram_kol_research.llm_chat as llm_chat_module

from telegram_kol_research.llm_chat import (
    LLMProxyConfig,
    RuntimeAgentLLMConfigError,
    load_runtime_agent_llm_config,
    request_structured_chat_turn,
)
from telegram_kol_research.runtime_agent_contracts import (
    RuntimeAgentFinalResponseError,
)


def test_runtime_agent_llm_config_is_isolated():
    config = load_runtime_agent_llm_config(
        environ={
            "TELEGRAM_KOL_LLM_BASE_URL": "https://shared.invalid/v1",
            "TELEGRAM_KOL_LLM_API_KEY": "shared-key",
            "TELEGRAM_KOL_LLM_MODEL": "shared-model",
            "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL": (
                "https://api.xiaomimimo.com/v1"
            ),
            "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY": "agent-key",
            "TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL": "mimo-v2.5",
            "TELEGRAM_KOL_RUNTIME_AGENT_LLM_TIMEOUT_SECONDS": "999",
        },
        env_file_paths=[],
    )

    assert config == LLMProxyConfig(
        base_url="https://api.xiaomimimo.com",
        api_key="agent-key",
        model="mimo-v2.5",
        timeout_seconds=120.0,
        egress_socket_path="/run/telegram-kol-agent-model-egress.sock",
    )


def test_runtime_agent_http_client_uses_only_reviewed_unix_egress_socket():
    client = llm_chat_module._build_runtime_agent_http_client(
        LLMProxyConfig(
            base_url="https://api.xiaomimimo.com",
            api_key="agent-key",
            model="mimo-v2.5",
            timeout_seconds=30,
            egress_socket_path="/run/telegram-kol-agent-model-egress.sock",
        ),
        timeout_seconds=12,
    )
    try:
        assert client._transport._pool._uds == (
            "/run/telegram-kol-agent-model-egress.sock"
        )
        assert client.timeout.connect == 12
    finally:
        client.close()


def test_runtime_agent_llm_config_rejects_unreviewed_egress_socket():
    with pytest.raises(RuntimeAgentLLMConfigError):
        load_runtime_agent_llm_config(
            environ={
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL": (
                    "https://api.xiaomimimo.com/v1"
                ),
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY": "agent-key",
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL": "mimo-v2.5",
                "TELEGRAM_KOL_RUNTIME_AGENT_MODEL_EGRESS_SOCKET": (
                    "/tmp/unreviewed.sock"
                ),
            },
            env_file_paths=[],
        )


def test_runtime_agent_llm_config_never_falls_back_to_shared_key():
    shared_key = "shared-key-must-not-leak"

    with pytest.raises(RuntimeAgentLLMConfigError) as exc_info:
        load_runtime_agent_llm_config(
            environ={
                "TELEGRAM_KOL_LLM_BASE_URL": "https://shared.invalid/v1",
                "TELEGRAM_KOL_LLM_API_KEY": shared_key,
                "TELEGRAM_KOL_LLM_MODEL": "shared-model",
            },
            env_file_paths=[],
        )

    assert str(exc_info.value) == (
        "dedicated Runtime Agent provider configuration is invalid"
    )
    assert shared_key not in str(exc_info.value)


def test_runtime_agent_llm_config_rejects_non_finite_timeout():
    with pytest.raises(RuntimeAgentLLMConfigError):
        load_runtime_agent_llm_config(
            environ={
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL": (
                    "https://api.xiaomimimo.com/v1"
                ),
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY": "agent-key",
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL": "mimo-v2.5",
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_TIMEOUT_SECONDS": "inf",
            },
            env_file_paths=[],
        )


@pytest.mark.parametrize(
    ("base_url", "model"),
    [
        ("https://other-provider.invalid/v1", "mimo-v2.5"),
        ("https://api.xiaomimimo.com/v1", "other-model"),
    ],
)
def test_runtime_agent_llm_config_requires_reviewed_mimo_provider(
    base_url,
    model,
):
    with pytest.raises(RuntimeAgentLLMConfigError):
        load_runtime_agent_llm_config(
            environ={
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL": base_url,
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY": "agent-key",
                "TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL": model,
            },
            env_file_paths=[],
        )


def test_runtime_agent_llm_config_uses_systemd_environment_when_secret_file_is_unreadable(
    tmp_path,
    monkeypatch,
):
    secret_file = tmp_path / "runtime_incident_agent.env"
    secret_file.write_text("unreadable=true\n", encoding="utf-8")
    original_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(secret_file):
            raise PermissionError("root-owned secret")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    config = load_runtime_agent_llm_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL": (
                "https://api.xiaomimimo.com/v1"
            ),
            "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY": "agent-key",
            "TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL": "mimo-v2.5",
        },
        env_file_paths=[secret_file],
    )

    assert config.model == "mimo-v2.5"


def test_runtime_agent_llm_config_does_not_open_defaults_when_systemd_environment_is_complete(
    tmp_path,
    monkeypatch,
):
    config_file = tmp_path / "llm.env"
    config_file.write_text("unreadable=true\n", encoding="utf-8")
    original_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(config_file):
            raise PermissionError("root-owned unrelated config")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL",
        "https://api.xiaomimimo.com/v1",
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY",
        "agent-key",
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL",
        "mimo-v2.5",
    )

    config = load_runtime_agent_llm_config(
        env_file_paths=[config_file],
    )

    assert config.model == "mimo-v2.5"


def test_request_structured_chat_turn_normalizes_one_tool_call():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        assert '"tool_choice":"auto"' in payload
        assert '"parallel_tool_calls":false' in payload
        assert '"max_tokens"' not in payload
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
    usages = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        assert '"response_format":{"type":"json_object"}' in payload
        assert '"tools"' not in payload
        assert '"tool_choice"' not in payload
        return httpx.Response(
            200,
            request=request,
            json={
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                },
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"incident_id":17,'
                                '"confidence":"low"}'
                            )
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
        max_completion_tokens=512,
        usage_callback=lambda **value: usages.append(value),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert turn == {"final": {"incident_id": 17, "confidence": "low"}}
    assert usages == [
        {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    ]


@pytest.mark.parametrize("content", ["not-json", "[]", None])
def test_request_structured_chat_turn_marks_malformed_final_as_correctable(
    content,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": content}}]},
        )

    with pytest.raises(RuntimeAgentFinalResponseError):
        request_structured_chat_turn(
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


def test_request_structured_chat_turn_marks_malformed_final_tool_as_correctable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "final-1",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_runtime_diagnosis",
                                        "arguments": "not-json",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    with pytest.raises(RuntimeAgentFinalResponseError):
        request_structured_chat_turn(
            config=LLMProxyConfig(
                base_url="http://proxy.test",
                api_key="",
                model="gpt-test",
                timeout_seconds=5,
            ),
            messages=[{"role": "system", "content": "Read-only diagnosis."}],
            tool_schemas=[{"type": "function", "function": {"name": "read"}}],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_request_structured_chat_turn_serializes_provider_parallel_calls():
    def handler(request: httpx.Request) -> httpx.Response:
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
                                },
                                {
                                    "id": "call-2",
                                    "type": "function",
                                    "function": {
                                        "name": "get_worker_state",
                                        "arguments": '{"incident_id":17}',
                                    },
                                },
                            ]
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
        tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": "get_incident_summary",
                    "parameters": {"type": "object"},
                },
            }
        ],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert turn["tool_call"]["name"] == "get_incident_summary"

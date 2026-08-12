from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "local_vlm_smoke_test.py"


def _load_module():
    assert SCRIPT_PATH.exists(), "local VLM smoke-test helper is missing"
    spec = importlib.util.spec_from_file_location("local_vlm_smoke_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_to_data_url_uses_detected_png_mime_type(tmp_path):
    module = _load_module()
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nchart")

    result = module.image_to_data_url(image_path, max_image_bytes=1024)

    assert result.startswith("data:image/png;base64,")
    assert "chart" not in result


def test_image_to_data_url_rejects_oversized_input(tmp_path):
    module = _load_module()
    image_path = tmp_path / "large.jpg"
    image_path.write_bytes(b"x" * 11)

    with pytest.raises(ValueError, match="exceeds 10 bytes"):
        module.image_to_data_url(image_path, max_image_bytes=10)


def test_build_payload_preserves_all_images_in_openai_content_order(tmp_path):
    module = _load_module()
    first = tmp_path / "first.png"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    second.write_bytes(b"\xff\xd8\xffsecond")

    payload = module.build_payload(
        model="test-vlm",
        prompt="read both",
        image_paths=[first, second],
        max_tokens=123,
        max_image_bytes=1024,
    )

    content = payload["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "read both"}
    assert [item["type"] for item in content] == [
        "text",
        "image_url",
        "image_url",
    ]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert payload["max_tokens"] == 123


def test_send_request_reports_summary_without_raw_base64(tmp_path):
    module = _load_module()
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsecret-image-content")
    captured_request = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_request["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "test-vlm",
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"total_tokens": 7},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = module.send_request(
            client=client,
            base_url="http://127.0.0.1:18080/v1",
            model="test-vlm",
            prompt="read image",
            image_paths=[image_path],
            max_tokens=64,
            max_image_bytes=1024,
        )

    assert captured_request["json"]["messages"][1]["content"][1]["type"] == "image_url"
    assert result["status_code"] == 200
    assert result["image_count"] == 1
    assert result["content"] == '{"ok":true}'
    assert "base64" not in json.dumps(result)
    assert "secret-image-content" not in json.dumps(result)


@pytest.mark.parametrize(
    ("base_url", "expected_path"),
    [
        ("http://127.0.0.1:18080", "/v1/chat/completions"),
        ("http://127.0.0.1:18080/v1", "/v1/chat/completions"),
    ],
)
def test_send_request_accepts_root_or_v1_base_url(tmp_path, base_url, expected_path):
    module = _load_module()
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nchart")
    observed_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        module.send_request(
            client=client,
            base_url=base_url,
            model="test-vlm",
            prompt="read image",
            image_paths=[image_path],
            max_tokens=64,
            max_image_bytes=1024,
        )

    assert observed_paths == [expected_path]


def test_send_request_reports_non_json_http_failure_without_traceback(tmp_path):
    module = _load_module()
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nchart")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model unavailable")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = module.send_request(
            client=client,
            base_url="http://127.0.0.1:18080/v1",
            model="test-vlm",
            prompt="read image",
            image_paths=[image_path],
            max_tokens=64,
            max_image_bytes=1024,
        )

    assert result["status_code"] == 503
    assert result["ok"] is False
    assert result["error"] == "model unavailable"
    assert isinstance(result["elapsed_seconds"], float)


def test_build_payload_rejects_excessive_aggregate_image_bytes(tmp_path):
    module = _load_module()
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 8)
    second.write_bytes(b"\x89PNG\r\n\x1a\n" + b"b" * 8)

    with pytest.raises(ValueError, match="combined images exceed 20 bytes"):
        module.build_payload(
            model="test-vlm",
            prompt="read both",
            image_paths=[first, second],
            max_tokens=64,
            max_image_bytes=1024,
            max_total_image_bytes=20,
        )


@pytest.mark.parametrize(
    "response_body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
    ],
)
def test_send_request_rejects_malformed_success_response(tmp_path, response_body):
    module = _load_module()
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nchart")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = module.send_request(
            client=client,
            base_url="http://127.0.0.1:18080/v1",
            model="test-vlm",
            prompt="read image",
            image_paths=[image_path],
            max_tokens=64,
            max_image_bytes=1024,
        )

    assert result["status_code"] == 200
    assert result["ok"] is False
    assert result["error"] == "malformed response: missing assistant content"

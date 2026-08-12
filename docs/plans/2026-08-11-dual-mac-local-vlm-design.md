# Dual Mac Local VLM Testbed Design

**Goal:** Run a local vision-language model on the dedicated M4/16GB Mac mini and let the work M4/16GB Mac mini test it safely over the existing Wi-Fi network, without touching production Telegram or trading services.

## Scope

The first phase is an isolated inference testbed. The compute Mac owns only the model runtime and downloaded model weights. The work Mac owns test prompts, test images, and benchmark results. No Telegram session, production database, Deepcoin credential, trading authority, or live message processing moves in this phase.

## Selected approach

Use MLX-VLM with `mlx-community/Qwen3-VL-4B-Instruct-4bit` on the compute Mac. The model is small enough to leave useful headroom on a 16GB M4 while supporting Chinese text and images. MLX-VLM exposes an OpenAI-compatible `/v1/chat/completions` endpoint whose text-and-`image_url` request shape matches the project's existing recognition client.

The model server binds only to `127.0.0.1:8080` on the compute Mac. The work Mac reaches it through an authenticated SSH tunnel:

```text
Work Mac localhost:18080
        |
        | encrypted SSH over the shared Wi-Fi/router
        v
Compute Mac localhost:8080 -> MLX-VLM -> Metal GPU/unified memory
```

This avoids exposing an unauthenticated inference API to every device on the Wi-Fi. Bonjour hostname discovery (`<computer-name>.local`) is preferred; a router DHCP reservation is the fallback if name discovery is unreliable.

## Alternatives considered

- `llama.cpp`: mature and efficient, but the initial target is MLX-native and MLX-VLM has a closer match to the project's multi-image OpenAI request shape.
- Ollama: easiest interactive installation, but adds an API/model packaging layer that is unnecessary for this controlled compatibility test.
- AirLLM: rejected for the real-time path because layer-by-layer SSD loading trades capacity for latency.

## Components

### Compute Mac

- macOS Remote Login restricted to the work Mac's user/account access.
- Python 3.12 virtual environment managed by `uv`.
- A pinned MLX-VLM installation after the first successful compatibility test.
- `mlx-community/Qwen3-VL-4B-Instruct-4bit` downloaded into the normal Hugging Face cache.
- MLX-VLM bound to loopback only.
- A user LaunchAgent added only after manual text and vision tests pass.
- Sleep disabled while serving; display sleep may remain enabled.

### Work Mac

- SSH key dedicated to the compute Mac connection.
- SSH host alias and local port forward from `127.0.0.1:18080` to the compute Mac's `127.0.0.1:8080`.
- Read-only smoke and benchmark scripts that send text, one-image, and multi-image requests.
- No changes to production AI configuration during this phase.

## Data flow

1. The operator starts MLX-VLM on the compute Mac.
2. The work Mac opens the SSH tunnel.
3. A test client posts an OpenAI-style request to `http://127.0.0.1:18080/v1/chat/completions`.
4. SSH forwards it to the compute Mac's loopback API.
5. MLX-VLM loads the image, runs inference through MLX/Metal, and returns JSON.
6. The work Mac records timing and validates the returned structure.

No request is automatically connected to message ingestion or execution.

## Capacity policy

Start with the 4-bit 4B model and one inference request at a time. Limit initial context/KV cache to 4096 tokens and output to 512 tokens. Do not test an 8B/9B model until the 4B baseline completes without memory pressure or swap growth. On a 16GB Mac, sustained yellow/red memory pressure, rapidly increasing swap, or repeated model-process termination is a failed capacity result.

## Security and failure behavior

- The inference server must remain bound to `127.0.0.1`; never use `0.0.0.0` for this phase.
- Only SSH port 22 crosses Wi-Fi, protected by macOS authentication and the dedicated key.
- Password SSH login can be disabled after key login is verified, but the operator must retain local console access.
- A failed tunnel, failed health check, timeout, malformed JSON, or unreadable image fails the test request; it never falls back into production execution.
- Model requests and screenshots may contain sensitive material, so benchmark artifacts must not be committed unless sanitized.

## Acceptance criteria

- Work Mac can resolve or reach the compute Mac and establish key-based SSH.
- Compute Mac exposes MLX-VLM only on loopback port 8080.
- Work Mac receives HTTP 200 from `/health` through local port 18080.
- Chinese text, one image, and multiple images each receive a valid response.
- Ten sequential mixed requests finish without a server crash or unrecovered timeout.
- Median request duration is recorded; a practical initial target is under 30 seconds per normal message, with every request below the project's current 60-second recognition timeout.
- Memory pressure stays green for ordinary tests and the process is not killed by macOS.
- Disconnecting the SSH tunnel makes the API unreachable from the work Mac, proving there is no direct LAN exposure.

## Rollback

Stop and unload the LaunchAgent if installed, close the SSH tunnel, remove the dedicated SSH authorization line, delete the local AI virtual environment and Hugging Face model cache only after resolving their exact paths, and restore any sleep setting changed for the test. No production rollback is necessary because production configuration is not changed.

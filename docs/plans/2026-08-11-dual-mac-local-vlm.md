# Dual Mac Local VLM Testbed Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy a loopback-only MLX-VLM service on the dedicated M4/16GB Mac mini and test text and vision inference from the work Mac through an SSH tunnel over the shared Wi-Fi.

**Architecture:** The compute Mac runs a 4-bit Qwen3-VL 4B model through MLX-VLM on `127.0.0.1:8080`. The work Mac forwards `127.0.0.1:18080` to that loopback endpoint over SSH, so the model API is never exposed directly to the Wi-Fi LAN. Production Telegram, database, and trading paths remain unchanged.

**Tech Stack:** macOS Tahoe, Wi-Fi/Bonjour, OpenSSH, `uv`, Python 3.12, MLX-VLM, Qwen3-VL 4B Instruct 4-bit, OpenAI-compatible Chat Completions API, launchd.

---

## Before starting

Use these names consistently:

- **Work Mac:** the Mac containing this repository.
- **Compute Mac:** the second M4/16GB Mac that will run the model.
- **Compute account:** the normal non-administrator-or-administrator user used interactively on the compute Mac; do not use `root`.

Have the compute Mac's keyboard/display available until SSH key login works. Keep both Macs on AC power and the same trusted Wi-Fi. Do not make any production configuration change during this plan.

### Task 1: Name and identify the compute Mac

**Files:** None.

**Step 1: Set a recognizable computer name**

On the compute Mac, open **System Settings → General → About → Name** and set:

```text
ai-mac-mini
```

The expected Bonjour hostname is `ai-mac-mini.local`.

**Step 2: Record the compute username**

On the compute Mac Terminal, run:

```bash
whoami
```

Record the exact result as `COMPUTE_USER`. In later commands, replace `<compute-user>` with this value; do not type the angle brackets.

**Step 3: Verify hardware and free disk**

Run on the compute Mac:

```bash
system_profiler SPHardwareDataType
df -h /System/Volumes/Data
```

Expected: Apple M4, 16GB memory, and at least 15GB free disk. The model itself is about 3.1GB, while caches, environments, and upgrade headroom require more.

**Step 4: Record the LAN address**

Run:

```bash
ipconfig getifaddr en0 || ipconfig getifaddr en1
```

Record the returned private address, normally `192.168.x.x` or `10.x.x.x`, as a fallback. Do not expose or forward a router internet/WAN port.

### Task 2: Prove basic Wi-Fi connectivity

**Files:** None.

**Step 1: Test Bonjour from the work Mac**

Run on the work Mac:

```bash
ping -c 3 ai-mac-mini.local
```

Expected: three replies. If this fails, test the private address recorded in Task 1:

```bash
ping -c 3 <compute-private-ip>
```

**Step 2: Stabilize addressing if Bonjour is unreliable**

Open the router's management page, find **DHCP reservation**, **address reservation**, or **static lease**, select the compute Mac, and reserve its current private IP. Do not configure port forwarding, DMZ, public exposure, or a public static IP.

**Step 3: Stop on network isolation**

If neither Bonjour nor the private IP responds, confirm both Macs are on the same normal Wi-Fi rather than a guest network. Guest networks often block device-to-device traffic. Do not weaken the router firewall to work around guest isolation; move both devices to the trusted LAN.

### Task 3: Enable restricted SSH access

**Files:**
- Work Mac: `~/.ssh/id_ed25519_ai_mac`
- Work Mac: `~/.ssh/id_ed25519_ai_mac.pub`
- Work Mac: `~/.ssh/config`
- Compute Mac: `~/.ssh/authorized_keys`

**Step 1: Enable Remote Login on the compute Mac**

Open **System Settings → General → Sharing → Remote Login**. Turn it on and choose **Only these users**, adding only the compute account.

**Step 2: Create a dedicated key on the work Mac**

Run on the work Mac:

```bash
ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519_ai_mac" -C "work-mac-to-ai-mac"
```

Set a passphrase. This creates only a new dedicated key and does not replace existing SSH keys.

**Step 3: Install the public key**

Run, replacing the username:

```bash
ssh-copy-id -i "$HOME/.ssh/id_ed25519_ai_mac.pub" <compute-user>@ai-mac-mini.local
```

If macOS does not have `ssh-copy-id`, display the public key:

```bash
pbcopy < "$HOME/.ssh/id_ed25519_ai_mac.pub"
```

Then, on the compute Mac, open `~/.ssh/authorized_keys`, add that single copied line, and run:

```bash
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh/authorized_keys"
```

**Step 4: Add a work-Mac SSH alias**

Add this block to the work Mac's `~/.ssh/config`, replacing the username:

```sshconfig
Host ai-compute
    HostName ai-mac-mini.local
    User <compute-user>
    IdentityFile ~/.ssh/id_ed25519_ai_mac
    IdentitiesOnly yes
    ServerAliveInterval 15
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
```

If Bonjour failed but the reserved IP worked, use that private IP for `HostName`.

**Step 5: Test key login**

Run on the work Mac:

```bash
ssh ai-compute 'hostname; uname -m'
```

Expected: the compute hostname followed by `arm64`. A key passphrase prompt is normal; a compute-account password prompt means key authentication is not configured correctly.

### Task 4: Prepare the compute Mac runtime

**Files:**
- Create directory: `~/local-ai/`
- Create virtual environment: `~/local-ai/.venv/`

**Step 1: Install Apple command-line tools if absent**

Run on the compute Mac:

```bash
xcode-select -p
```

If it reports that no developer directory exists, run:

```bash
xcode-select --install
```

Complete the graphical installer, then rerun `xcode-select -p`.

**Step 2: Install `uv`**

If Homebrew is already installed, use:

```bash
brew install uv
```

Otherwise install `uv` from its official installer:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new Terminal and verify:

```bash
uv --version
```

**Step 3: Create an isolated environment**

Run:

```bash
mkdir -p "$HOME/local-ai"
uv venv --python 3.12 "$HOME/local-ai/.venv"
```

Expected: a Python virtual environment at `~/local-ai/.venv`.

**Step 4: Install MLX-VLM**

Run:

```bash
uv pip install --python "$HOME/local-ai/.venv/bin/python" --upgrade mlx-vlm
```

Verify without loading a model:

```bash
"$HOME/local-ai/.venv/bin/python" -c 'import mlx, mlx_vlm; print("mlx-vlm import ok")'
```

Expected: `mlx-vlm import ok`.

**Step 5: Capture reproducible versions**

Run:

```bash
"$HOME/local-ai/.venv/bin/python" -m pip freeze | grep -E '^(mlx|mlx-vlm|transformers|huggingface-hub)=='
```

Record this output in a local test note. Do not blindly upgrade after compatibility is established.

### Task 5: Run the model manually on loopback

**Files:** Hugging Face cache files under the compute account.

**Step 1: Prevent system sleep for the first test**

Keep this command running in a separate compute Mac Terminal:

```bash
caffeinate -dimsu
```

This is temporary; stopping it restores the previous behavior.

**Step 2: Start MLX-VLM**

Run in another compute Mac Terminal:

```bash
"$HOME/local-ai/.venv/bin/mlx_vlm.server" \
  --model mlx-community/Qwen3-VL-4B-Instruct-4bit \
  --host 127.0.0.1 \
  --port 8080 \
  --max-kv-size 4096 \
  --max-tokens 512
```

The first run downloads roughly 3.1GB and can take several minutes. Expected final state: the process remains running and listens on port 8080.

**Step 3: Verify the bind address on the compute Mac**

Run in a third compute Mac Terminal:

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
```

Expected: a listener on `127.0.0.1:8080`. Stop immediately if it shows `*:8080` or `0.0.0.0:8080`.

**Step 4: Test local health**

Run:

```bash
curl --fail --silent --show-error http://127.0.0.1:8080/health
```

Expected: HTTP success with health/model information.

### Task 6: Connect the Macs with an SSH tunnel

**Files:** None.

**Step 1: Start the tunnel on the work Mac**

Run and leave it open:

```bash
ssh -N -L 127.0.0.1:18080:127.0.0.1:8080 ai-compute
```

The terminal normally shows no output while the tunnel is healthy.

**Step 2: Verify tunnel health from another work Mac Terminal**

Run:

```bash
curl --fail --silent --show-error http://127.0.0.1:18080/health
```

Expected: the same healthy response seen locally on the compute Mac.

**Step 3: Prove the API is not directly exposed**

Run on the work Mac, substituting the private IP:

```bash
curl --connect-timeout 3 http://<compute-private-ip>:8080/health
```

Expected: connection failure. Success is a security failure: recheck that MLX-VLM uses `--host 127.0.0.1`.

### Task 7: Run the text compatibility test

**Files:** None.

**Step 1: Send a Chinese structured-output prompt from the work Mac**

Run:

```bash
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:18080/v1/chat/completions \
  -d '{
    "model":"mlx-community/Qwen3-VL-4B-Instruct-4bit",
    "messages":[
      {"role":"system","content":"只返回JSON对象，不要Markdown。字段为symbol、side、entry、stop_loss、take_profit。无法确定的字段填null。"},
      {"role":"user","content":"BTC现价附近做多，止损112000，止盈118000。"}
    ],
    "temperature":0,
    "max_tokens":256
  }'
```

Expected: HTTP 200 and a response whose assistant content is valid JSON or a JSON string that can be parsed. Exact trading values are evaluated manually; this request has no execution authority.

**Step 2: Record latency**

Repeat with curl timing:

```bash
curl --output /dev/null --silent \
  --write-out 'connect=%{time_connect} first_byte=%{time_starttransfer} total=%{time_total}\n' \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:18080/v1/chat/completions \
  -d '{"model":"mlx-community/Qwen3-VL-4B-Instruct-4bit","messages":[{"role":"user","content":"只回复：测试成功"}],"temperature":0,"max_tokens":32}'
```

Expected: total duration below 60 seconds after the model is warm.

### Task 8: Add repeatable vision smoke tests

**Files:**
- Create: `scripts/local_vlm_smoke_test.py`
- Test input outside git: a sanitized screenshot selected by the operator

**Step 1: Write the failing unit tests**

Create tests for a helper that:

- accepts `--base-url`, `--model`, and one or more `--image` paths;
- converts each image to a correctly typed base64 data URL;
- sends the same OpenAI content-block shape used by `message_recognition.py`;
- rejects files above a conservative limit before transmission;
- reports HTTP status, total duration, response text, and image count;
- never prints an API key or raw base64 data.

**Step 2: Run tests and verify they fail before implementation**

Run on the work Mac from the repository:

```bash
uv run pytest tests/test_local_vlm_smoke_test.py -v
```

Expected: failure because the helper does not yet exist.

**Step 3: Implement the minimal smoke-test helper**

Use Python `base64`, `mimetypes`, `time.monotonic`, and `httpx`. Default the base URL to `http://127.0.0.1:18080/v1`, model to `mlx-community/Qwen3-VL-4B-Instruct-4bit`, timeout to 60 seconds, and output limit to 512 tokens. Build content blocks in this order:

```json
[
  {"type": "text", "text": "..."},
  {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
]
```

**Step 4: Run tests and verify they pass**

Run:

```bash
uv run pytest tests/test_local_vlm_smoke_test.py -v
```

Expected: all smoke-helper unit tests pass without making a network call.

**Step 5: Test one sanitized trading screenshot**

Run:

```bash
uv run python scripts/local_vlm_smoke_test.py \
  --image /absolute/path/to/sanitized-trading-screenshot.png \
  --prompt '读取图片中可见的交易品种、方向、入场价、止损和止盈。只返回JSON；看不清的字段填null。'
```

Expected: HTTP 200 within 60 seconds, no invented value for unreadable content, and a manually reviewable result.

**Step 6: Test two distinct images**

Run with two sanitized images:

```bash
uv run python scripts/local_vlm_smoke_test.py \
  --image /absolute/path/to/first.png \
  --image /absolute/path/to/second.png \
  --prompt '分别列出第一张和第二张图中的显著文字，不要合并。只返回JSON。'
```

Expected: the answer contains distinct observations from both images. If it only describes the last image, record the MLX-VLM version and treat multi-image compatibility as failed rather than modifying production code.

**Step 7: Commit only reviewed, non-sensitive helper code**

Do not add test screenshots or captured raw responses. Commit the helper and unit tests only after review:

```bash
git add scripts/local_vlm_smoke_test.py tests/test_local_vlm_smoke_test.py
git commit -m "test: add local vision model smoke client"
```

### Task 9: Benchmark capacity and stability

**Files:**
- Create locally but do not commit raw data: `.local-vlm-results/`

**Step 1: Establish an idle baseline on the compute Mac**

Open **Activity Monitor → Memory** and record memory pressure and swap used before starting the model.

**Step 2: Run ten sequential mixed requests**

Use five representative sanitized text messages and five representative sanitized screenshots. Run them one at a time, not concurrently. Record:

- cold-start duration;
- warm total duration;
- success/failure;
- JSON validity;
- visually important field accuracy;
- compute Mac memory pressure and swap delta.

**Step 3: Apply the first-stage gate**

Pass only if:

- 10/10 requests return without server crash;
- every normal request completes below 60 seconds;
- median warm request is preferably below 30 seconds;
- no malformed response is silently treated as success;
- memory pressure normally remains green;
- swap does not grow continuously across the ten-request run.

Accuracy is recorded separately. Hardware capacity can pass even if the 4B model is not accurate enough for later trading use.

**Step 4: Do not upgrade model size automatically**

Only after the baseline passes should an 8B 4-bit VLM be tested. Test it as a separate run with the same cases. Do not replace the known-working 4B baseline or connect either model to production.

### Task 10: Install optional background startup after manual tests pass

**Files:**
- Compute Mac create: `~/Library/LaunchAgents/local.mlx-vlm.server.plist`
- Compute Mac create: `~/local-ai/run-server.sh`

**Step 1: Create a fixed launcher**

Create `~/local-ai/run-server.sh` containing an `exec` call to the exact tested `mlx_vlm.server` binary with the exact tested model, loopback host, port, KV limit, and output limit. Do not use an unpinned shell activation or depend on interactive shell startup files.

**Step 2: Create the LaunchAgent**

Configure:

- label `local.mlx-vlm.server`;
- `RunAtLoad` true;
- `KeepAlive` only for process failure, with throttling;
- stdout and stderr under `~/Library/Logs/local-mlx-vlm/`;
- the compute account, never root;
- no `NetworkState` requirement;
- no public bind address.

**Step 3: Validate the plist before loading**

Run:

```bash
plutil -lint "$HOME/Library/LaunchAgents/local.mlx-vlm.server.plist"
```

Expected: `OK`.

**Step 4: Load and verify**

Run:

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.mlx-vlm.server.plist"
launchctl print "gui/$(id -u)/local.mlx-vlm.server"
curl --fail --silent --show-error http://127.0.0.1:8080/health
```

Expected: service state is running and health succeeds after model load completes.

**Step 5: Configure power behavior explicitly**

In **System Settings → Energy**, enable automatic startup after power failure if desired and prevent automatic sleep while the display is off. Do not disable the screen lock. Confirm the service remains healthy after display sleep.

### Task 11: Final security and rollback drill

**Files:** Previously created testbed files only.

**Step 1: Close the SSH tunnel**

Press `Ctrl-C` in the work Mac tunnel terminal. Then run:

```bash
curl --connect-timeout 3 http://127.0.0.1:18080/health
```

Expected: connection failure.

**Step 2: Reopen the tunnel and recheck**

Run:

```bash
ssh -N -L 127.0.0.1:18080:127.0.0.1:8080 ai-compute
```

In another terminal, health must succeed again.

**Step 3: Prove service rollback**

If the LaunchAgent was installed, run on the compute Mac:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.mlx-vlm.server.plist"
```

Expected: local health fails and no listener remains on port 8080. Bootstrap it again only if continued testing is desired.

**Step 4: Preserve production separation**

Verify the repository's production AI configuration and service definitions are unchanged. Do not deploy, restart `telegram-kol.service`, change Deepcoin allowlists, or push a production configuration as part of this testbed.

## Troubleshooting map

- `ai-mac-mini.local` does not resolve: use the router-reserved private IP in `~/.ssh/config`.
- SSH works but tunnel health fails: test compute-local `/health`, then check `lsof` and MLX-VLM logs.
- `Address already in use`: find the exact owner with `lsof -nP -iTCP:8080 -sTCP:LISTEN`; do not kill unrelated processes blindly.
- Model download stalls: verify disk space and internet access on the compute Mac; rerun the same command to resume cache downloads.
- Process is killed or memory pressure stays red: stop the server, keep the 4B model, lower `--max-kv-size` to 2048, and retest.
- Image ignored: confirm a `data:image/...;base64,` URL and the OpenAI list-of-content-blocks shape; record exact package/model versions.
- Only the final image is recognized: treat multi-image support as failed for the installed version and test a current reviewed release before considering an application workaround.
- Request exceeds 60 seconds: capture cold versus warm timings; capacity fails if normal warm requests still exceed 60 seconds.

## Completion record

At the end, record only non-sensitive facts:

- compute Mac model and memory;
- macOS version;
- MLX/MLX-VLM version;
- exact model ID;
- text, single-image, and multi-image pass/fail;
- median and maximum warm latency;
- peak memory pressure and swap delta;
- whether LaunchAgent startup and rollback succeeded.

Do not commit private screenshots, Telegram message contents, SSH private keys, local IP inventories, credentials, or raw production prompts.

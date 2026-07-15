# Deepcoin Position-History Rate-Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pace exact Deepcoin position-history reads at the endpoint's stricter published one-request-per-second limit so the production attribution audit can complete without weakening fail-closed behavior.

**Architecture:** Add a per-`DeepcoinRestClient` monotonic pace gate invoked only by `list_position_history`. Inject the clock and sleeper for deterministic unit tests, leave all request signing and response validation unchanged, and keep every HTTP/schema/evidence failure blocking. After local review, deploy normally and run only a fresh production dry run without `--apply`.

**Tech Stack:** Python 3.12+, `httpx`, `time.monotonic`, `time.sleep`, pytest, Typer CLI, SQLite read-only verification, systemd.

## Global Constraints

- Default minimum interval for `/deepcoin/account/positions-history`: exactly `1.05` seconds between request start times.
- The first position-history request on a client runs immediately.
- Pacing is per client instance and affects no other Deepcoin endpoint.
- Do not retry `401` or any other HTTP error automatically.
- Preserve the signed request path, exact query parameters, strict list schema, evidence fingerprints, and all current fail-closed rules.
- Automatic trading remains disabled throughout production verification.
- Never run `repair-position-attribution` with `--apply` in this plan.
- Never modify the production database or submit/cancel any exchange order.
- Preserve the existing production backup `data/research.db.20260715-123243.historical-cleanup.bak`, size `22528000`, SHA256 `c6dfa9eba14628a0ac8d1de3453b09f0d3d34913a02cead640c316a5e56c3c6f`.

## File map

- Modify `src/telegram_kol_research/deepcoin_client.py`: own the endpoint-specific pace state and invoke it immediately before exact history GET requests.
- Modify `tests/test_deepcoin_client.py`: provide deterministic fake time and cover first-call, remaining-delay, elapsed-delay, endpoint-isolation, and unchanged request/schema behavior.
- Modify `docs/runbook.md`: tell operators that exact-history dry runs are intentionally paced and that interruption or source errors remain blockers.
- Modify `docs/server-deployment.md`: add the same production verification expectation next to the attribution repair procedure.
- Do not modify `historical_attribution_cleanup.py`, `position_attribution_repair.py`, database models, migrations, or exchange-write modules.

---

### Task 1: Add the endpoint-specific monotonic pace gate

**Files:**
- Modify: `tests/test_deepcoin_client.py:15-210`
- Modify: `src/telegram_kol_research/deepcoin_client.py:5-16`
- Modify: `src/telegram_kol_research/deepcoin_client.py:144-208`

**Interfaces:**
- Consumes: existing `DeepcoinRestClient.list_position_history(*, inst_id: str, pos_id: str) -> list[dict[str, Any]]`.
- Produces: constructor injection points `monotonic_factory: Callable[[], float] | None`, `sleep_fn: Callable[[float], None] | None`, and `position_history_min_interval_seconds: float = 1.05`; private method `_pace_position_history_request() -> None`.
- Preserves: all existing public method return values, paths, signatures, HTTP errors, schema errors, and authentication headers.

- [ ] **Step 1: Add deterministic fake time and the failing remaining-interval test**

Add this helper after `_CapturingHttpClient` in `tests/test_deepcoin_client.py`:

```python
class _FakeMonotonicClock:
    def __init__(self, current: float = 100.0):
        self.current = current
        self.sleeps = []

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += seconds

    def advance(self, seconds):
        self.current += seconds
```

Add this test after `test_list_position_history_queries_exact_split_position`:

```python
def test_list_position_history_waits_only_for_remaining_endpoint_interval():
    clock = _FakeMonotonicClock()
    http_client = _CapturingHttpClient(
        {"code": "0", "data": [{"posId": "position-1"}]}
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        position_history_min_interval_seconds=1.05,
    )

    client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-1",
    )
    assert clock.sleeps == []

    clock.advance(0.25)
    http_client.payload = {"code": "0", "data": [{"posId": "position-2"}]}
    client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-2",
    )

    assert clock.sleeps == pytest.approx([0.80])
    assert len(http_client.requests) == 2
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest \
  tests/test_deepcoin_client.py::test_list_position_history_waits_only_for_remaining_endpoint_interval \
  -q
```

Expected: FAIL before any production-code edit because `DeepcoinRestClient.__init__()` does not accept `monotonic_factory`.

- [ ] **Step 3: Implement the minimal per-client pace gate**

In `src/telegram_kol_research/deepcoin_client.py`, add the imports:

```python
import time
from collections.abc import Callable
```

Extend the constructor and state exactly as follows:

```python
    def __init__(
        self,
        credentials: DeepcoinCredentials,
        *,
        http_client: httpx.Client | None = None,
        timestamp_factory=None,
        monotonic_factory: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        position_history_min_interval_seconds: float = 1.05,
    ) -> None:
        self._credentials = credentials
        self._http_client = http_client
        self._timestamp_factory = timestamp_factory or _utc_timestamp_ms
        self._monotonic_factory = monotonic_factory or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._position_history_min_interval_seconds = max(
            0.0,
            float(position_history_min_interval_seconds),
        )
        self._last_position_history_request_started_at: float | None = None
```

Call the pace gate as the first statement inside `list_position_history`:

```python
        self._pace_position_history_request()
```

Add this private method immediately after `list_position_history`:

```python
    def _pace_position_history_request(self) -> None:
        now = self._monotonic_factory()
        previous = self._last_position_history_request_started_at
        if previous is not None:
            remaining = self._position_history_min_interval_seconds - (now - previous)
            if remaining > 0:
                self._sleep_fn(remaining)
                now = self._monotonic_factory()
        self._last_position_history_request_started_at = now
```

Do not modify `_request`, `_path_with_query`, `build_deepcoin_auth_headers`, or exception handling.

- [ ] **Step 4: Run the new test and verify GREEN**

Run the Step 2 command again.

Expected: `1 passed`; the recorded sleep is approximately `0.80` seconds and no wall-clock sleep occurs.

- [ ] **Step 5: Add elapsed-time and endpoint-isolation regressions**

Add these tests:

```python
def test_list_position_history_does_not_sleep_after_full_interval_elapsed():
    clock = _FakeMonotonicClock()
    http_client = _CapturingHttpClient(
        {"code": "0", "data": [{"posId": "position-1"}]}
    )
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        position_history_min_interval_seconds=1.05,
    )

    client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-1",
    )
    clock.advance(1.10)
    client.list_position_history(
        inst_id="BTC-USDT-SWAP",
        pos_id="position-1",
    )

    assert clock.sleeps == []


def test_position_history_pacing_does_not_delay_other_endpoints():
    clock = _FakeMonotonicClock()
    http_client = _CapturingHttpClient({"code": "0", "data": []})
    client = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        monotonic_factory=clock,
        sleep_fn=clock.sleep,
        position_history_min_interval_seconds=1.05,
    )

    client.list_positions()
    client.list_positions()

    assert clock.sleeps == []
```

In the existing `test_list_position_history_queries_exact_split_position`, add
`position_history_min_interval_seconds=0.0` to its client constructor. That test
calls the endpoint twice to check malformed schema and must not incur a real
sleep.

- [ ] **Step 6: Run the focused client tests**

Run:

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest tests/test_deepcoin_client.py -q
```

Expected: all tests pass with no real-time one-second delay between test calls.

- [ ] **Step 7: Run attribution regression tests**

Run:

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest \
  tests/test_historical_attribution_cleanup.py \
  tests/test_position_attribution_repair.py \
  tests/test_cli_smoke.py \
  -q
```

Expected: all tests pass; no fingerprint, action, conflict, or CLI JSON assertion changes.

- [ ] **Step 8: Commit the paced client and tests**

```bash
git add src/telegram_kol_research/deepcoin_client.py tests/test_deepcoin_client.py
git diff --cached --check
git commit -m "fix: pace Deepcoin position history reads"
```

---

### Task 2: Document the paced audit and complete local verification

**Files:**
- Modify: `docs/runbook.md:248-280`
- Modify: `docs/server-deployment.md:93-138`

**Interfaces:**
- Consumes: the `1.05`-second `list_position_history` pace gate from Task 1.
- Produces: an operator contract explaining expected audit duration and unchanged fail-closed handling.

- [ ] **Step 1: Add the operator-facing pacing note**

Add this paragraph after the dry-run command in both documentation files:

```markdown
Exact position-history evidence is intentionally paced at no more than one
request per 1.05 seconds, following the stricter Deepcoin endpoint limit. A
history-heavy dry run can therefore take roughly one second per candidate; do
not interrupt it merely because output is delayed. HTTP or schema errors still
block every action and must not be treated as empty history or retried with
`--apply`.
```

- [ ] **Step 2: Verify documentation scope and formatting**

Run:

```bash
git diff --check
git diff -- docs/runbook.md docs/server-deployment.md
```

Expected: only the approved pacing/fail-closed note is added; no apply procedure or automatic-trading instruction is relaxed.

- [ ] **Step 3: Run the full local suite**

Run:

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest -q
```

Expected: all tests pass; only the seven existing YAML prompt deprecation warnings may remain.

- [ ] **Step 4: Run compilation and diff gates**

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m compileall -q src tests
git diff --check
git status --short
```

Expected: compilation and diff checks exit `0`; only intended docs changes and untracked controller artifacts may appear.

- [ ] **Step 5: Commit the runbook update**

```bash
git add docs/runbook.md docs/server-deployment.md
git diff --cached --check
git commit -m "docs: document paced position history audits"
```

---

### Task 3: Review, integrate, deploy, and rerun the production audit read-only

**Files:**
- Review only: all commits after design commit `dd82906`
- Production read only: `/opt/telegram-kol-analyzer/data/research.db`
- Preserve: `/opt/telegram-kol-analyzer/data/research.db.20260715-123243.historical-cleanup.bak`

**Interfaces:**
- Consumes: reviewed Task 1 and Task 2 commits with a passing full suite.
- Produces: one captured production dry-run JSON and an evidence-backed audit report; produces no database or exchange mutation.

- [ ] **Step 1: Run independent task reviews and a final whole-branch review**

Generate review packages from each task's recorded base commit, then a final package from `229ef211a8e3943c5962ff81f38a28087c50aa01` to the final HEAD. Review specifically for:

- default `1.05` seconds and first-call immediacy;
- monotonic rather than wall-clock time;
- exact remaining-delay calculation;
- history-only scope;
- no generic HTTP retry;
- unchanged signature/path/schema behavior; and
- unchanged repair fail-closed behavior.

Expected: no unresolved P0-P2 findings before integration.

- [ ] **Step 2: Run fresh controller-side verification**

```bash
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m pytest -q
PYTHONPATH="$PWD/src" /Users/steven/Documents/telegram获取消息/.venv/bin/python \
  -m compileall -q src tests
git diff --check 229ef21..HEAD
```

Expected: full suite passes, compilation exits `0`, and the diff check is clean.

- [ ] **Step 3: Fast-forward the approved target branch and retest**

From `/Users/steven/Documents/telegram获取消息`, verify the checkout is clean and still points to `229ef21`, then:

```bash
git merge --ff-only codex/historical-attribution-cleanup
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q
git push origin codex/deepcoin-auto-trading-v1
```

Expected: fast-forward merge, full suite pass on the merged checkout, and successful non-force push.

- [ ] **Step 4: Recheck production safety invariants before deployment**

Using read-only SSH commands, verify:

- production HEAD is the prior deployed commit before pulling;
- `telegram-kol.service` is active;
- `auto_trade_enabled` is false;
- `position_attribution_audits` has zero `historical_cleanup` rows; and
- the backup size and SHA256 exactly match the Global Constraints.

Expected: every invariant matches. Stop on any mismatch.

- [ ] **Step 5: Deploy with the standard helper and verify independently**

```bash
cd /Users/steven/Documents/telegram获取消息
./scripts/server_git_update.sh
```

Independently verify server HEAD, active service PID/start time, application startup logs, automatic trading false, zero historical cleanup audits, and unchanged backup hash.

- [ ] **Step 6: Run one fresh production dry run without apply**

On the server:

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research repair-position-attribution \
  --database-path data/research.db \
  > /tmp/historical-attribution-paced-dry-run-20260715.json
```

Expected: first line `DRY RUN`; runtime is roughly candidate count times one second; no `--apply` appears in the command.

- [ ] **Step 7: Parse and audit the dry-run JSON**

Report exactly:

- current `live_position_ids`;
- `actions` count and action types;
- `historical_actions` count and action types;
- `unresolved_conflicts` count and reasons;
- `evidence_source_errors` count;
- intersection between live position IDs and every historical action identifier;
- whether `install_position_ownership_unique_index` is present and last;
- database, exchange-evidence, and plan fingerprints; and
- post-run automatic-trading value and historical-cleanup audit count.

Expected safety conditions: no source errors, no current actions, no live ID in historical cleanup, automatic trading false, and zero committed cleanup audits. Any violation is a stop condition, not permission to apply.

- [ ] **Step 8: Stop and hand off the exact read-only result**

Do not run `--apply`, do not re-enable automatic trading, and do not modify the database even if conflicts are zero and the historical plan is internally consistent. Preserve the captured JSON for the operator's next decision.

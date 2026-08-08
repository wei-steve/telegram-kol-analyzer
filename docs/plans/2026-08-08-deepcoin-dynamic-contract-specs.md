# Deepcoin Dynamic Contract Specifications Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Deepcoin new-entry eligibility equal the intersection of the global symbol allowlist, Deepcoin's current live USDT perpetual instruments, and fresh validated contract specifications, while preserving risk-reducing management for existing positions.

**Architecture:** Add an authoritative Deepcoin product-info reader, validate and atomically cache complete instrument snapshots, and expose a refreshable provider plus a venue-capability gate. Roll it out dormant, then shadow, then live for future entries only; persist the exact spec used in every draft/binding and retain the current fail-closed behavior whenever platform support or fresh specifications cannot be proven.

**Tech Stack:** Python 3.12, Decimal, dataclasses, pathlib/JSON, FastAPI, vanilla JavaScript, pytest, Typer, systemd deployment helpers.

---

### Task 1: Parse authoritative Deepcoin product information

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Test: `tests/test_deepcoin_client.py`

**Step 1: Write failing client tests**

Add tests proving `list_swap_instruments()` calls
`/deepcoin/market/instruments?instType=SWAP` and returns the untouched fields needed by the validator:

```python
assert instruments == [{
    "instType": "SWAP",
    "instId": "SOL-USDT-SWAP",
    "ctVal": "1",
    "lotSz": "1",
    "minSz": "1",
    "tickSz": "0.001",
    "state": "live",
}]
```

Also cover non-list response data and Deepcoin error codes.

**Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_deepcoin_client.py -k 'swap_instruments' -v`

Expected: FAIL because the protocol and client method do not exist.

**Step 3: Implement the product-info method**

Add `DEEPCOIN_MARKET_INSTRUMENTS_PATH`, add `list_swap_instruments()` to the protocol, and implement one public GET request. Do not perform contract validation or caching in the HTTP client.

**Step 4: Run focused and adjacent tests**

Run: `pytest tests/test_deepcoin_client.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_client.py tests/test_deepcoin_client.py
git commit -m "feat: read Deepcoin swap instrument specs"
```

### Task 2: Validate a complete instrument snapshot

**Files:**
- Create: `src/telegram_kol_research/deepcoin_contract_spec_cache.py`
- Create: `tests/test_deepcoin_contract_spec_cache.py`
- Modify: `src/telegram_kol_research/deepcoin_contract_specs.py`

**Step 1: Write failing validator tests**

Test a valid BTC/ETH/SOL response and assert exact `DeepcoinContractSpec` conversion:

```python
snapshot = validate_deepcoin_instrument_snapshot(rows, fetched_at=NOW, ttl=timedelta(hours=24))
assert snapshot.specs_by_instrument_id["SOL-USDT-SWAP"] == DeepcoinContractSpec(
    instrument_id="SOL-USDT-SWAP",
    contract_value=1,
    quantity_step=1,
    min_quantity=1,
    price_tick=0.001,
)
```

Parameterize refusals for missing fields, booleans, NaN/infinity, zero/negative values, duplicate IDs, conflicting duplicates, non-SWAP products, malformed IDs, and `minSz` incompatible with `lotSz`. Test that non-live valid instruments remain in capability status but are excluded from the live spec map.

**Step 2: Verify the tests fail**

Run: `pytest tests/test_deepcoin_contract_spec_cache.py -k 'validate' -v`

Expected: FAIL because the snapshot model and validator do not exist.

**Step 3: Implement immutable snapshot models and validation**

Use `Decimal` throughout validation and convert to the existing spec type only after all rows pass. Include `schema_version`, source path, fetched/expiry timestamps, normalized SHA-256 digest, live specs, and non-live capability states. Reject the entire candidate snapshot on structural ambiguity.

**Step 4: Run the focused tests**

Run: `pytest tests/test_deepcoin_contract_spec_cache.py -k 'validate' -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_contract_specs.py src/telegram_kol_research/deepcoin_contract_spec_cache.py tests/test_deepcoin_contract_spec_cache.py
git commit -m "feat: validate Deepcoin instrument snapshots"
```

### Task 3: Publish and load the cache atomically

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_contract_spec_cache.py`
- Modify: `tests/test_deepcoin_contract_spec_cache.py`

**Step 1: Write failing cache tests**

Cover atomic publish/read, deterministic digest verification, `fsync` before replacement, an injected write failure preserving the prior cache, corrupt JSON, unsupported schema version, digest mismatch, future `fetched_at`, expiry at the exact boundary, and a path whose parent does not yet exist.

**Step 2: Verify failure**

Run: `pytest tests/test_deepcoin_contract_spec_cache.py -k 'cache or publish or stale' -v`

Expected: FAIL because persistence is not implemented.

**Step 3: Implement cache persistence**

Write UTF-8 JSON to a same-directory temporary file with restrictive normal file permissions, flush and `os.fsync`, reload and validate the temporary snapshot, then use `os.replace`. Never overwrite a valid cache when refresh or candidate validation fails.

**Step 4: Run all cache tests**

Run: `pytest tests/test_deepcoin_contract_spec_cache.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_contract_spec_cache.py tests/test_deepcoin_contract_spec_cache.py
git commit -m "feat: atomically cache Deepcoin contract specs"
```

### Task 4: Add the refreshable provider and venue-capability decision

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_contract_spec_cache.py`
- Modify: `src/telegram_kol_research/deepcoin_contract_specs.py`
- Create: `src/telegram_kol_research/deepcoin_symbol_capability.py`
- Create: `tests/test_deepcoin_symbol_capability.py`
- Modify: `tests/test_deepcoin_contract_specs.py`

**Step 1: Write failing provider and intersection tests**

Create a matrix covering:

```python
assert decide("BTC", global_allowed={"BTC"}, snapshot=live_btc).reason == "tradable"
assert decide("BTC", global_allowed=set(), snapshot=live_btc).reason == "global_not_allowed"
assert decide("ABC", global_allowed={"ABC"}, snapshot=live_btc).reason == "venue_instrument_unsupported"
assert decide("SOL", global_allowed={"SOL"}, snapshot=suspended_sol).reason == "venue_instrument_not_live"
assert decide("SOL", global_allowed={"SOL"}, snapshot=stale_sol).reason == "contract_spec_stale"
```

Also prove case normalization, exact `-USDT-SWAP` mapping, reload after atomic replacement, fail-closed corrupt cache behavior, and bounded refresh locking.

**Step 2: Verify failure**

Run: `pytest tests/test_deepcoin_symbol_capability.py tests/test_deepcoin_contract_specs.py -v`

Expected: FAIL because the refreshable provider and decision service do not exist.

**Step 3: Implement the provider and decision service**

Keep `get_contract_spec()` compatible with existing call sites. Add explicit status lookup so callers never infer “unsupported” from a bare `None`. Implement one in-process refresh lock and expose last-success, expiry, and bounded last-error metadata.

**Step 4: Run focused tests**

Run: `pytest tests/test_deepcoin_symbol_capability.py tests/test_deepcoin_contract_specs.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_contract_spec_cache.py src/telegram_kol_research/deepcoin_contract_specs.py src/telegram_kol_research/deepcoin_symbol_capability.py tests/test_deepcoin_symbol_capability.py tests/test_deepcoin_contract_specs.py
git commit -m "feat: gate symbols by Deepcoin capabilities"
```

### Task 5: Add explicit rollout settings and startup wiring

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_trading_settings.py`
- Modify: `tests/test_web_cli.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write failing configuration tests**

Add `deepcoin_contract_specs_mode` with `static`, `shadow`, and `live`, defaulting to `static`; add cache path and positive TTL options at process composition rather than accepting arbitrary web payload paths. Prove malformed rollout modes fail closed and the default preserves current production behavior.

**Step 2: Verify failure**

Run: `pytest tests/test_trading_settings.py tests/test_web_cli.py -k 'contract_spec' -v`

Expected: FAIL because the rollout setting and composition do not exist.

**Step 3: Wire the provider without changing execution authority**

Construct one refreshable provider in the CLI composition root and inject the same instance into web/listener/workers. In `static` mode, preserve the YAML provider. In `shadow`, refresh and compare but return the static provider result to execution. In `live`, return only fresh authoritative cache results.

**Step 4: Run focused tests**

Run: `pytest tests/test_trading_settings.py tests/test_web_cli.py tests/test_web_app.py -k 'contract_spec or trading_settings' -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/trading_settings.py src/telegram_kol_research/cli.py src/telegram_kol_research/web_app.py tests/test_trading_settings.py tests/test_web_cli.py tests/test_web_app.py
git commit -m "feat: wire dynamic contract spec rollout"
```

### Task 6: Refresh on startup, settings access, and a bounded schedule

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/deepcoin_contract_spec_cache.py`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_deepcoin_contract_spec_cache.py`

**Step 1: Write failing lifecycle tests**

Prove startup refresh is non-blocking, concurrent refreshes coalesce, the settings-symbol endpoint attempts refresh, settings save persists the requested global list even when one symbol is unsupported, an offline refresh uses a still-fresh cache, and no cache/expired cache reports fail-closed status without changing settings.

**Step 2: Verify failure**

Run: `pytest tests/test_web_app.py tests/test_deepcoin_contract_spec_cache.py -k 'refresh or unsupported or stale' -v`

Expected: FAIL because refresh orchestration is absent.

**Step 3: Implement bounded refresh orchestration**

Use the existing application lifecycle/task pattern. Refresh at startup and half-TTL intervals, with bounded timeout and error text. Do not introduce network I/O inside `trading_settings_from_payload()` or database transactions.

**Step 4: Run focused tests**

Run: `pytest tests/test_web_app.py tests/test_deepcoin_contract_spec_cache.py -k 'refresh or unsupported or stale' -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py src/telegram_kol_research/deepcoin_contract_spec_cache.py tests/test_web_app.py tests/test_deepcoin_contract_spec_cache.py
git commit -m "feat: refresh Deepcoin specs safely"
```

### Task 7: Enforce the capability gate before enqueue

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py`
- Modify: `src/telegram_kol_research/recovery_order_confirmation.py`
- Modify: `src/telegram_kol_research/recovery_live_submit_gate.py`
- Modify: `src/telegram_kol_research/recovery_live_submit.py`
- Modify: `tests/test_auto_trade_execution.py`
- Modify: `tests/test_recovery_order_confirmation.py`
- Modify: `tests/test_recovery_live_submit_gate.py`
- Modify: `tests/test_recovery_live_submit.py`

**Step 1: Write failing pre-enqueue safety tests**

For each reason (`venue_instrument_unsupported`, `venue_instrument_not_live`, `contract_spec_missing`, `contract_spec_invalid`, `contract_spec_stale`, `contract_spec_sync_unavailable`), assert:

```python
assert result["status"] in {"skipped", "blocked"}
assert count_trade_signals(session_factory) == 0
assert count_execution_bindings(session_factory) == 0
assert fake_client.write_calls == []
```

Prove global-disallowed remains `symbol_not_allowed` and is checked before venue capability. Prove a fresh live SOL spec reaches a correctly sized executable draft.

**Step 2: Verify failure**

Run: `pytest tests/test_auto_trade_execution.py tests/test_recovery_order_confirmation.py tests/test_recovery_live_submit_gate.py tests/test_recovery_live_submit.py -k 'unsupported or stale or dynamic_spec or SOL' -v`

Expected: FAIL because the explicit gate is not enforced.

**Step 3: Apply one shared gate**

Call the capability decision before draft confirmation and before any queue/binding/exchange write. Preserve the existing allowlist check. Attach the exact validated spec snapshot and digest to every accepted draft.

**Step 4: Run focused execution tests**

Run: `pytest tests/test_auto_trade_execution.py tests/test_recovery_order_confirmation.py tests/test_recovery_live_submit_gate.py tests/test_recovery_live_submit.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/auto_trade_execution.py src/telegram_kol_research/recovery_order_confirmation.py src/telegram_kol_research/recovery_live_submit_gate.py src/telegram_kol_research/recovery_live_submit.py tests/test_auto_trade_execution.py tests/test_recovery_order_confirmation.py tests/test_recovery_live_submit_gate.py tests/test_recovery_live_submit.py
git commit -m "fix: require Deepcoin symbol capability before enqueue"
```

### Task 8: Preserve risk-reducing management with frozen specs

**Files:**
- Modify: `src/telegram_kol_research/strategy_management_planner.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/deepcoin_execution_actions.py`
- Modify: `src/telegram_kol_research/source_message_deletion_worker.py`
- Modify: `tests/test_strategy_management_planner.py`
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `tests/test_source_message_deletion_worker.py`

**Step 1: Write failing delisting and stale-cache tests**

Create an existing verified SOL binding whose opening draft contains a frozen spec. Mark the current venue snapshot suspended or stale and prove new entry is blocked while exact-position partial close, full close, and protective stop workflows continue using the frozen spec. Add a refusal when neither a fresh current spec nor a proven frozen spec exists.

**Step 2: Verify failure**

Run: `pytest tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_source_message_deletion_worker.py -k 'frozen_spec or delisted or stale_spec' -v`

Expected: FAIL because management does not yet distinguish increase-risk from reduce-risk capability.

**Step 3: Implement frozen-spec resolution for risk reduction**

Resolve exact specs from the persisted binding/draft first for existing-position actions, retain exact position/instrument/side ownership checks, and prohibit the fallback for opening or increasing exposure.

**Step 4: Run focused management tests**

Run: `pytest tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_source_message_deletion_worker.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/strategy_management_planner.py src/telegram_kol_research/strategy_management_executor.py src/telegram_kol_research/deepcoin_execution_actions.py src/telegram_kol_research/source_message_deletion_worker.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_source_message_deletion_worker.py
git commit -m "fix: preserve exits with frozen contract specs"
```

### Task 9: Expose per-symbol Deepcoin capability in the workbench

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/styles.css`
- Modify: `tests/test_web_app.py`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write failing API and render tests**

Extend symbol rows with `venue_supported`, `venue_state`, `spec_status`, `tradable`, `reason_code`, `fetched_at`, and `expires_at`. Test selected unsupported symbols remain visible and selected, while their Deepcoin status is clearly non-tradable. Test escaped, bounded error rendering.

**Step 2: Verify failure**

Run: `pytest tests/test_web_app.py tests/test_web_page_render.py -k 'symbol and capability' -v`

Expected: FAIL because capability status is not returned or rendered.

**Step 3: Implement status UI**

Render concise badges for tradable, unsupported, non-live, stale, invalid, and sync-unavailable. Keep unsupported globally selected symbols editable; never imply that selecting one overrides venue support.

**Step 4: Run focused UI tests**

Run: `pytest tests/test_web_app.py tests/test_web_page_render.py -k 'symbol or trading_settings' -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py src/telegram_kol_research/templates/index.html src/telegram_kol_research/static/app.js src/telegram_kol_research/static/styles.css tests/test_web_app.py tests/test_web_page_render.py
git commit -m "feat: show Deepcoin symbol capability status"
```

### Task 10: Add operator commands and operational documentation

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_deepcoin_contract_spec_cli.py`
- Modify: `docs/runbook.md`
- Modify: `docs/server-deployment.md`
- Modify: `config/deepcoin_contract_specs.example.yaml`

**Step 1: Write failing CLI tests**

Specify read-only `deepcoin-contract-specs status` and explicit `deepcoin-contract-specs refresh` commands. Status must not create files; refresh must print only non-sensitive summary/digest data and fail nonzero without replacing a valid cache.

**Step 2: Verify failure**

Run: `pytest tests/test_deepcoin_contract_spec_cli.py -v`

Expected: FAIL because the commands do not exist.

**Step 3: Implement commands and document operations**

Document cache path, TTL, status meanings, refresh, shadow comparison, activation, rollback, and why static YAML is not an unlimited live fallback. Include the official endpoint and fields.

**Step 4: Run CLI tests**

Run: `pytest tests/test_deepcoin_contract_spec_cli.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py tests/test_deepcoin_contract_spec_cli.py docs/runbook.md docs/server-deployment.md config/deepcoin_contract_specs.example.yaml
git commit -m "docs: add dynamic contract spec operations"
```

### Task 11: Run the local regression and review gate

**Files:**
- Modify if required: only files already in this plan

**Step 1: Run formatting and static checks**

Run the repository's configured formatter/linter commands from `pyproject.toml`.

Expected: PASS without unrelated rewrites.

**Step 2: Run focused suites**

Run:

```bash
pytest tests/test_deepcoin_client.py tests/test_deepcoin_contract_specs.py tests/test_deepcoin_contract_spec_cache.py tests/test_deepcoin_symbol_capability.py tests/test_trading_settings.py tests/test_auto_trade_execution.py tests/test_recovery_order_confirmation.py tests/test_recovery_live_submit_gate.py tests/test_recovery_live_submit.py tests/test_strategy_management_planner.py tests/test_strategy_management_executor.py tests/test_source_message_deletion_worker.py tests/test_web_app.py tests/test_web_page_render.py -v
```

Expected: PASS.

**Step 3: Run the full local suite**

Run: `pytest -q`

Expected: PASS, excluding only tests explicitly documented as requiring production identity.

**Step 4: Review the complete diff**

Verify there is no credential output, no automatic addition to the global allowlist, no historical replay, no change to recognition/context targeting, and no path from an unsupported/stale spec to a Deepcoin write.

**Step 5: Commit review fixes if needed**

```bash
git add <only-reviewed-files>
git commit -m "test: harden dynamic contract spec rollout"
```

### Task 12: Deploy dormant and verify on the server

**Files:**
- Modify: `docs/runtime-incident-agent-status.md` only if recording verified production evidence is appropriate to the active project status format

**Step 1: Prove a safe deployment window**

Use existing read-only production checks to prove no time-sensitive strategy operation, entry submission, management mutation, recovery submission, or protection repair is active. If this cannot be proven, stop before deployment and record the exact remaining verification.

**Step 2: Push the reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: remote branch advances to the reviewed local HEAD.

**Step 3: Update production with the standard helper**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: server pulls the exact reviewed commit, reinstalls the editable package, and restarts `telegram-kol.service` only after the safe-window gate.

**Step 4: Verify dormant mode**

Confirm service/HTTP health, exact deployed commit, `deepcoin_contract_specs_mode=static`, no cache-driven execution authority, no historical replay, and no new exchange write caused by deployment.

**Step 5: Run a read-only server refresh**

Query the real public product-info endpoint and produce a non-sensitive candidate snapshot for BTC, ETH, and SOL. Compare BTC/ETH against the reviewed YAML and inspect all six required fields. Do not enable live mode or submit an order.

**Step 6: Run server-focused tests**

Run the focused client/cache/capability/execution/management/web suites on the server.

Expected: PASS using the server environment.

**Step 7: Commit and push production evidence if documentation changed**

```bash
git add docs/runtime-incident-agent-status.md
git commit -m "docs: record dormant contract spec verification"
git push origin codex/deepcoin-auto-trading-v1
```

### Task 13: Shadow comparison and separately approved live activation

**Files:**
- Modify: `docs/runtime-incident-agent-status.md` or a dedicated rollout evidence document

**Step 1: Enable shadow mode only in a proven safe window**

Keep the static provider authoritative. Record comparisons for BTC/ETH and any globally allowed additional symbol without changing order decisions.

**Step 2: Require two independent clean observations**

Both observations must show fresh cache, complete Deepcoin response, exact BTC/ETH parity or reviewed differences, valid SOL specs, zero unknown capability states, and no unsupported/stale symbol reaching enqueue.

**Step 3: Obtain explicit operator approval for live mode**

Do not infer approval from this implementation plan. Present the verified snapshot, differences, rollback command, and residual risks.

**Step 4: Activate BTC/ETH dynamic authority first**

Verify future-only behavior, no historical replay, correct draft spec digests, service health, and rollback.

**Step 5: Activate SOL/new symbols separately**

Only after a no-order dry-run proves correct quantity conversion, quantity step/minimum, price tick, TP/SL normalization, and pre-enqueue refusal paths. Observe the first natural future signal without replaying the prior failed SOL message.

**Step 6: Record evidence and final rollback state**

Document deployed commit, cache digest/timestamps, active mode, enabled symbols, test results, and the exact rollback path. Never include credentials or signed requests.

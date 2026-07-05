# Symbol Risk Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add global per-symbol maximum loss settings with default fallback, plus a searchable Deepcoin-backed allowed-symbol selector in the trading settings page.

**Architecture:** Keep risk selection decoupled in `trading_settings.py`: persisted settings normalize allowed symbols and per-symbol risk, while a helper resolves the effective max loss for a signal symbol. `recovery_scan.py` consumes the resolved value and remains unaware of Web form details. `deepcoin_client.py` exposes market symbol discovery, and `web_app.py` returns a UI-friendly symbol list.

**Tech Stack:** Python dataclasses, SQLAlchemy-backed settings JSON, FastAPI endpoints, vanilla JavaScript, pytest.

---

### Task 1: Persist And Resolve Per-Symbol Risk

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Test: `tests/test_trading_settings.py`

- [ ] **Step 1: Write failing tests**

Add tests that save `symbol_max_loss_usdt`, normalize symbols, ignore invalid loss values, and resolve symbol-specific risk with fallback to `default_max_loss_usdt`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_trading_settings.py -q`
Expected: failure because `symbol_max_loss_usdt` and `max_loss_for_symbol` do not exist yet.

- [ ] **Step 3: Implement settings parsing**

Add `symbol_max_loss_usdt: dict[str, float]`, parse dict/string payloads into uppercase symbol keys with positive float values, and add `TradingSettings.max_loss_for_symbol(symbol)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_trading_settings.py -q`
Expected: all tests pass.

### Task 2: Use Symbol Risk In Signal Recovery

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/recovery_scan.py`
- Test: `tests/test_trading_settings.py`
- Test: `tests/test_recovery_scan.py`

- [ ] **Step 1: Write failing tests**

Add a test that `apply_trading_settings_to_group_config()` uses symbol-specific risk only through an explicit symbol resolver, preserving existing sender-level overrides. Add a recovery scan test showing an ETH signal gets ETH risk while an unconfigured symbol gets default risk.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_trading_settings.py tests/test_recovery_scan.py -q`
Expected: failure because recovery still only reads group/sender `max_loss_usdt`.

- [ ] **Step 3: Implement risk resolver injection**

Keep group config shape backward compatible and add a focused helper that receives settings and symbol, returning the effective max loss. Use it where recovered trade signals are built.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_trading_settings.py tests/test_recovery_scan.py -q`
Expected: all tests pass.

### Task 3: Add Deepcoin Tradable Symbol API

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_client.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_deepcoin_client.py`
- Test: `tests/test_web_app.py`

- [ ] **Step 1: Write failing tests**

Add client test for `list_swap_symbols()` parsing `instId` values from ticker payloads. Add Web API test for `/api/trading-settings/symbols` returning sorted symbols and marking selected symbols.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_deepcoin_client.py tests/test_web_app.py -q`
Expected: failure because the method and endpoint do not exist.

- [ ] **Step 3: Implement API**

Add `DeepcoinRestClient.list_swap_symbols()` backed by the existing market tickers path. Add FastAPI endpoint that returns `{"symbols": [{"symbol": "BTC", "instrument_id": "BTC-USDT-SWAP", "selected": true}]}` and falls back to saved symbols if Deepcoin is unavailable.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_deepcoin_client.py tests/test_web_app.py -q`
Expected: all tests pass.

### Task 4: Replace Allowed Symbols Text Input With Searchable Multi-Select

**Files:**
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_assets_smoke.py`

- [ ] **Step 1: Write failing render/assets tests**

Assert the trading settings page includes symbol selector hooks and symbol risk inputs. Keep this smoke-level because the app uses vanilla JS and existing tests do not run a browser.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -q`
Expected: failure because hooks are absent.

- [ ] **Step 3: Implement UI**

Add hidden inputs for serialized `allowed_symbols` and `symbol_max_loss_usdt`, a search input, a selected list, and checkbox rows populated from the new API. JS keeps form payloads synchronized and lets the user edit per-symbol max loss.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_web_page_render.py tests/test_web_assets_smoke.py -q`
Expected: all tests pass.

### Task 5: Verify, Commit, Push, Deploy

**Files:**
- All modified files

- [ ] **Step 1: Run targeted tests**

Run: `pytest tests/test_trading_settings.py tests/test_recovery_scan.py tests/test_deepcoin_client.py tests/test_web_app.py tests/test_web_page_render.py tests/test_web_assets_smoke.py -q`
Expected: all tests pass.

- [ ] **Step 2: Inspect git diff**

Run: `git diff --stat` and `git diff --check`
Expected: intended files only and no whitespace errors.

- [ ] **Step 3: Commit and push**

Run: `git add ...`, `git commit -m "feat: add per-symbol trading risk settings"`, then `git push origin codex/deepcoin-auto-trading-v1`.

- [ ] **Step 4: Update production**

Run: `powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1`
Expected: server pulls the pushed commit, reinstalls editable package, and restarts `telegram-kol.service`.

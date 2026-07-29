# Symbol Fixed Entry Thresholds Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the shared percentage-based range-entry threshold with three decimal-safe fixed price distances configured independently for every allowed symbol.

**Architecture:** Persist a canonical `symbol_entry_thresholds` mapping in the existing global trading-settings JSON and resolve it to a small typed threshold value object at execution boundaries. Pass the resolved values into the offline Deepcoin draft builder, which remains the single authority for market/limit leg construction and exchange tick normalization. Extend the existing symbol selector so each selected symbol edits maximum loss plus the three new fixed values.

**Tech Stack:** Python 3.12, `Decimal`, SQLAlchemy, FastAPI, Jinja2, browser JavaScript, CSS, pytest.

---

Use @test-driven-development for every implementation task and
@requesting-code-review after the focused and full test suites pass. Preserve
unrelated worktree changes, especially the existing `uv.lock` modification and
untracked audit artifacts.

### Task 1: Add decimal-safe symbol threshold settings

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py:20-205`
- Test: `tests/test_trading_settings.py`

**Step 1: Write failing settings tests**

Add tests covering:

```python
from decimal import Decimal


def test_legacy_settings_seed_initial_fixed_entry_thresholds(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = save_trading_settings(
        session_factory,
        {
            "allowed_symbols": ["BTC", "ETH", "SOL"],
            "symbol_max_loss_usdt": {"BTC": 20, "ETH": 15, "SOL": 10},
        },
    )

    assert settings.entry_thresholds_for_symbol("BTC") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("200"),
        first_limit_offset=Decimal("90"),
        second_limit_offset=Decimal("90"),
    )
    assert settings.entry_thresholds_for_symbol("ETH") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("4"),
        first_limit_offset=Decimal("2"),
        second_limit_offset=Decimal("2"),
    )
    assert settings.entry_thresholds_for_symbol("SOL") == SymbolEntryThresholds.zero()


def test_symbol_entry_thresholds_preserve_small_decimals(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = save_trading_settings(
        session_factory,
        {
            "symbol_entry_thresholds": {
                "PEPE": {
                    "market_leg_threshold": "0.000003",
                    "first_limit_offset": "0.000001",
                    "second_limit_offset": "0.000002",
                }
            }
        },
    )

    assert settings.to_dict()["symbol_entry_thresholds"]["PEPE"] == {
        "market_leg_threshold": "0.000003",
        "first_limit_offset": "0.000001",
        "second_limit_offset": "0.000002",
    }


@pytest.mark.parametrize("invalid", ["-1", -0.1, "nan", "inf", {}, []])
def test_symbol_entry_thresholds_reject_invalid_values(invalid):
    with pytest.raises(ValueError):
        trading_settings_from_payload(
            {
                "symbol_entry_thresholds": {
                    "BTC": {
                        "market_leg_threshold": invalid,
                        "first_limit_offset": "0",
                        "second_limit_offset": "0",
                    }
                }
            }
        )
```

Also extend the existing round-trip test to prove unselected symbol
configuration is retained when it is present in the submitted mapping.

**Step 2: Run the focused tests and verify failure**

Run:

```bash
pytest -q tests/test_trading_settings.py
```

Expected: FAIL because `SymbolEntryThresholds`,
`symbol_entry_thresholds`, and `entry_thresholds_for_symbol` do not exist.

**Step 3: Implement the settings value object and parser**

In `trading_settings.py`:

```python
from decimal import Decimal, InvalidOperation


ENTRY_THRESHOLD_KEYS = (
    "market_leg_threshold",
    "first_limit_offset",
    "second_limit_offset",
)

LEGACY_SYMBOL_ENTRY_THRESHOLD_DEFAULTS = {
    "BTC": {
        "market_leg_threshold": "200",
        "first_limit_offset": "90",
        "second_limit_offset": "90",
    },
    "ETH": {
        "market_leg_threshold": "4",
        "first_limit_offset": "2",
        "second_limit_offset": "2",
    },
}


@dataclass(frozen=True, slots=True)
class SymbolEntryThresholds:
    market_leg_threshold: Decimal
    first_limit_offset: Decimal
    second_limit_offset: Decimal

    @classmethod
    def zero(cls) -> "SymbolEntryThresholds":
        return cls(Decimal("0"), Decimal("0"), Decimal("0"))

    def to_dict(self) -> dict[str, str]:
        return {
            "market_leg_threshold": _canonical_decimal(self.market_leg_threshold),
            "first_limit_offset": _canonical_decimal(self.first_limit_offset),
            "second_limit_offset": _canonical_decimal(self.second_limit_offset),
        }
```

Add `symbol_entry_thresholds: dict[str, dict[str, str]]` to
`TradingSettings`. Implement `entry_thresholds_for_symbol()` so a missing
symbol returns `SymbolEntryThresholds.zero()`.

Implement `_parse_symbol_entry_thresholds()` with these rules:

- normalize symbol keys to uppercase;
- accept JSON numbers and strings;
- construct values with `Decimal(str(value))`;
- reject booleans, negative values, NaN, infinity, containers, and empty text;
- persist canonical non-exponent decimal strings;
- when the entire new mapping is absent, seed BTC and ETH with the approved
  initial values;
- when the mapping exists, do not silently add non-submitted symbol overrides;
- a missing symbol or missing individual field resolves to zero.

Override `TradingSettings.to_dict()` only as needed to ensure the mapping stays
JSON serializable and contains canonical strings. Do not convert risk-budget
fields to `Decimal`.

**Step 4: Run focused tests and verify pass**

Run:

```bash
pytest -q tests/test_trading_settings.py
```

Expected: PASS.

**Step 5: Commit the settings layer**

```bash
git add src/telegram_kol_research/trading_settings.py tests/test_trading_settings.py
git commit -m "feat: add symbol fixed entry threshold settings"
```

### Task 2: Make the Deepcoin draft builder use independent fixed offsets

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_order_builder.py:25-230`
- Modify: `src/telegram_kol_research/deepcoin_order_builder.py:357-400`
- Test: `tests/test_deepcoin_order_builder.py`

**Step 1: Replace percentage expectations with failing fixed-distance tests**

Add or update builder tests for:

```python
def test_long_range_inside_fixed_threshold_uses_market_and_second_offset():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="60000-61000",
            current_price="60850",
            market_leg_threshold="200",
            first_limit_offset="90",
            second_limit_offset="80",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [(leg["order_type"], leg["price"]) for leg in draft["order_legs"]] == [
        ("market", 60850.0),
        ("limit", 60080.0),
    ]


def test_long_range_outside_fixed_threshold_uses_independent_offsets():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            entry_range="60000-61000",
            current_price="60700",
            market_leg_threshold="200",
            first_limit_offset="90",
            second_limit_offset="80",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [(leg["order_type"], leg["price"]) for leg in draft["order_legs"]] == [
        ("limit", 61090.0),
        ("limit", 60080.0),
    ]


def test_short_range_uses_subtracted_fixed_offsets():
    draft = build_deepcoin_order_draft(
        _payload_preview(
            open_side="sell",
            position_side="short",
            entry_range="60000-61000",
            current_price="60300",
            market_leg_threshold="200",
            first_limit_offset="90",
            second_limit_offset="80",
        ),
        contract_spec=_btc_contract_spec(),
    )

    assert [(leg["order_type"], leg["price"]) for leg in draft["order_legs"]] == [
        ("limit", 59910.0),
        ("limit", 60920.0),
    ]
```

Add separate tests proving:

- market threshold `0` never creates a hybrid market leg, even when current
  price equals the anchor;
- zero offsets use the original endpoints;
- `0.000001` offsets survive until contract tick normalization;
- equivalent normalized limit legs still coalesce;
- an explicit `order_type="market"` remains one 100% leg.

**Step 2: Run the builder tests and verify failure**

Run:

```bash
pytest -q tests/test_deepcoin_order_builder.py
```

Expected: FAIL because the builder still multiplies endpoints by a percentage.

**Step 3: Implement fixed-distance calculation**

Parse the three flat per-draft values with a strict helper:

```python
market_leg_threshold = _parse_nonnegative_decimal(
    payload_preview.get("market_leg_threshold", "0"),
    field_name="market_leg_threshold",
)
first_limit_offset = _parse_nonnegative_decimal(
    payload_preview.get("first_limit_offset", "0"),
    field_name="first_limit_offset",
)
second_limit_offset = _parse_nonnegative_decimal(
    payload_preview.get("second_limit_offset", "0"),
    field_name="second_limit_offset",
)
```

Change `_range_entry_leg_prices()` to accept the two offsets:

```python
def _range_entry_leg_prices(
    *,
    position_side: str,
    low: float,
    high: float,
    first_limit_offset: Decimal,
    second_limit_offset: Decimal,
    contract_spec: DeepcoinContractSpec | None,
) -> tuple[float, float]:
    low_decimal = Decimal(str(low))
    high_decimal = Decimal(str(high))
    if position_side == "long":
        first = high_decimal + first_limit_offset
        second = low_decimal + second_limit_offset
    else:
        first = low_decimal - first_limit_offset
        second = high_decimal - second_limit_offset
    if first <= 0 or second <= 0:
        raise DeepcoinOrderDraftError("fixed entry offset produces non-positive price")
    return (
        _normalize_price(float(first), contract_spec),
        _normalize_price(float(second), contract_spec),
    )
```

Change `_hybrid_market_entry_price()` so:

```python
if current_price is None or market_leg_threshold <= 0:
    return None
anchor = high if position_side == "long" else low
if abs(Decimal(str(current_price)) - Decimal(str(anchor))) > market_leg_threshold:
    return None
```

For a hybrid order, build only the second limit price with
`second_limit_offset`; `first_limit_offset` must not affect it. Keep risk
allocation, quantity calculation, client IDs, notes, coalescing, and blocking
checks unchanged.

Remove `entry_range_order_style` and
`max_market_entry_deviation_pct` from active builder calculations. The builder
may accept extra legacy payload keys without using them so queued legacy JSON
does not crash.

**Step 4: Run builder tests and verify pass**

Run:

```bash
pytest -q tests/test_deepcoin_order_builder.py
```

Expected: PASS.

**Step 5: Commit the builder change**

```bash
git add src/telegram_kol_research/deepcoin_order_builder.py tests/test_deepcoin_order_builder.py
git commit -m "feat: build range entries from fixed price offsets"
```

### Task 3: Route per-symbol thresholds through automatic live entry

**Files:**
- Modify: `src/telegram_kol_research/auto_trade_execution.py:470-590`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:863-980`
- Test: `tests/test_auto_trade_execution.py`

**Step 1: Write failing orchestration tests**

Replace the existing percentage-based range test and add long/short cases that
assert the actual requests recorded by `_FakeDeepcoinClient`:

- BTC long, fixed market distance `200`, current price inside the upper-edge
  distance: one market request plus one trigger-limit request at
  `low + second_limit_offset`;
- BTC long outside the distance: two trigger-limit requests at
  `high + first_limit_offset` and `low + second_limit_offset`;
- ETH short inside the lower-edge distance: one market request plus one
  trigger-limit request at `high - second_limit_offset`;
- zero market threshold: no market request;
- explicit market wording: one market request unchanged.

Persist settings in each test using `symbol_entry_thresholds`, not the legacy
percentage field.

**Step 2: Run focused automatic-entry tests and verify failure**

Run:

```bash
pytest -q tests/test_auto_trade_execution.py -k "range_entry or fixed_threshold"
```

Expected: FAIL because orchestration still resolves the shared percentage.

**Step 3: Resolve thresholds once per candidate**

After symbol validation, resolve:

```python
entry_thresholds = settings.entry_thresholds_for_symbol(symbol)
```

Change `market_price_is_near_entry_edge()` to accept a fixed
`max_distance: Decimal`; return `False` when it is zero. Use `Decimal(str(...))`
for the absolute-distance comparison.

Pass the three canonical threshold strings to
`_build_auto_hybrid_deepcoin_draft()` and then into
`build_deepcoin_order_draft()`. Rename the execution-plan note only if needed
to remove the obsolete percentage implication; keep the durable meaning
`range_hybrid_market_half_limit_half`.

When the fixed market threshold does not match, let the existing recovery live
submission path build the two fixed limit legs. Do not add an alternate direct
exchange submission path.

Keep `nearby_entry_market_deviation_pct` exclusively in the single-price
“附近/左右” branch.

**Step 4: Run the automatic-entry tests**

Run:

```bash
pytest -q tests/test_auto_trade_execution.py
```

Expected: PASS.

**Step 5: Commit live routing**

```bash
git add src/telegram_kol_research/auto_trade_execution.py tests/test_auto_trade_execution.py
git commit -m "feat: route symbol thresholds into automatic entries"
```

### Task 4: Route thresholds through recovery previews and live submission

**Files:**
- Modify: `src/telegram_kol_research/recovery_execution_queue.py:20-125`
- Verify/modify if required: `src/telegram_kol_research/recovery_live_submit.py`
- Test: `tests/test_recovery_execution_queue.py`
- Test: `tests/test_auto_trade_execution.py`

**Step 1: Write failing recovery-preview tests**

Update preview assertions to expect:

```python
assert row["payload_preview"]["market_leg_threshold"] == "200"
assert row["payload_preview"]["first_limit_offset"] == "90"
assert row["payload_preview"]["second_limit_offset"] == "90"
assert "max_market_entry_deviation_pct" not in row["payload_preview"]
```

Add ETH and a zero-default symbol case. Assert the generated
`deepcoin_order_draft.order_legs` contains the correct independently offset
prices.

**Step 2: Run focused tests and verify failure**

Run:

```bash
pytest -q tests/test_recovery_execution_queue.py
```

Expected: FAIL because previews still emit the shared percentage.

**Step 3: Pass resolved settings into each preview**

Change `_preview_row()` to accept a `SymbolEntryThresholds` value, emit the
three flat canonical values, and stop emitting active range-style and market
percentage controls. Resolve by `row.symbol` inside
`list_recovery_execution_previews()`.

Inspect `recovery_live_submit.py` and verify it submits the already-built
`order_legs` without reconstructing endpoint prices. Modify it only if a
percentage field is still used to rebuild legs. Preserve the submission gate,
leg coalescing, idempotency, protection intents, and exact position attribution.

**Step 4: Run recovery and live-submission regression tests**

Run:

```bash
pytest -q tests/test_recovery_execution_queue.py tests/test_auto_trade_execution.py
```

Expected: PASS.

**Step 5: Commit recovery routing**

```bash
git add src/telegram_kol_research/recovery_execution_queue.py \
  src/telegram_kol_research/recovery_live_submit.py \
  tests/test_recovery_execution_queue.py tests/test_auto_trade_execution.py
git commit -m "feat: use fixed thresholds in recovery drafts"
```

Before committing, omit `recovery_live_submit.py` from `git add` if inspection
proved no change was required.

### Task 5: Expose symbol thresholds through the settings API

**Files:**
- Modify: `src/telegram_kol_research/web_app.py:2760-2810`
- Modify: `src/telegram_kol_research/web_app.py:5020-5060`
- Test: `tests/test_web_app.py:850-900`

**Step 1: Write failing API tests**

Extend `test_trading_settings_api_persists_runtime_risk_defaults`:

```python
"symbol_entry_thresholds": {
    "BTC": {
        "market_leg_threshold": "200",
        "first_limit_offset": "90",
        "second_limit_offset": "90",
    },
    "PEPE": {
        "market_leg_threshold": "0.000003",
        "first_limit_offset": "0.000001",
        "second_limit_offset": "0.000002",
    },
},
```

Assert POST and GET return the exact canonical strings. Add a 422 test for a
negative threshold. Extend `/api/trading-settings/symbols` assertions so each
symbol row includes an `entry_thresholds` object, using zeros when missing.

**Step 2: Run focused API tests and verify failure**

Run:

```bash
pytest -q tests/test_web_app.py -k "trading_settings"
```

Expected: FAIL because symbol metadata does not include threshold values.

**Step 3: Add threshold data to symbol metadata**

Extend the existing symbol-row builder alongside `max_loss_usdt`:

```python
"entry_thresholds": settings.entry_thresholds_for_symbol(symbol).to_dict(),
```

Let `save_trading_settings()` remain the validation authority. Confirm the API
returns its `ValueError` as the existing settings endpoint's 422 response; do
not duplicate decimal validation in the route.

**Step 4: Run focused API tests**

Run:

```bash
pytest -q tests/test_web_app.py -k "trading_settings"
```

Expected: PASS.

**Step 5: Commit API support**

```bash
git add src/telegram_kol_research/web_app.py tests/test_web_app.py
git commit -m "feat: expose symbol entry thresholds in settings api"
```

### Task 6: Render and edit four per-symbol controls

**Files:**
- Modify: `src/telegram_kol_research/templates/index.html:340-370`
- Modify: `src/telegram_kol_research/static/app.js:2369-2640`
- Modify: `src/telegram_kol_research/static/app.css:2744-2775`
- Test: `tests/test_web_page_render.py:1280-1340`
- Test: `tests/test_web_assets_smoke.py`

**Step 1: Write failing page and asset tests**

Assert the rendered settings form:

- contains a hidden `symbol_entry_thresholds` input;
- does not show `现价入场最大偏离 %`;
- retains `单点附近市价容忍 %`;
- labels it `单点“附近”市价容忍 %`;
- does not present `区间入场方式` as an active control;
- contains data hooks for the four-column per-symbol editor.

Add static asset assertions for:

- parsing threshold JSON;
- initializing a newly selected symbol to three `"0"` values;
- keeping threshold configuration when a symbol is deselected;
- serializing all retained symbol configurations;
- `min = "0"` and decimal-capable input steps.

**Step 2: Run page and asset tests and verify failure**

Run:

```bash
pytest -q tests/test_web_page_render.py tests/test_web_assets_smoke.py
```

Expected: FAIL because the page only renders the maximum-loss editor.

**Step 3: Add template state**

Add:

```html
<input
  type="hidden"
  name="symbol_entry_thresholds"
  value='{{ trading_settings.symbol_entry_thresholds|tojson }}'
  data-symbol-entry-thresholds-input
/>
```

Remove the visible shared range percentage input. Keep legacy settings in the
server model but do not submit a fallback percentage from this form. Rename the
single-point label and remove/hide the obsolete range-style selector.

**Step 4: Extend symbol-selector state and rendering**

Add `thresholdsBySymbol` to `initTradingSymbolSelector()`. Implement a strict
front-end parser that retains decimal input as strings instead of calling
`Number()` before persistence:

```javascript
const ZERO_ENTRY_THRESHOLDS = {
  market_leg_threshold: '0',
  first_limit_offset: '0',
  second_limit_offset: '0',
};
```

For every selected symbol render:

```text
BTC | 最大亏损 USDT | 第一腿市价阈值 | 第一腿限价偏移 | 第二腿限价偏移
```

Use number inputs with `min="0"` and `step="any"`. Accept blank input during
editing but serialize blank as `"0"` on save. Reject negative or non-finite
values in the UI and show the existing save error state. Do not delete
`thresholdsBySymbol[symbol]` when deselecting; this implements the approved
restore-on-reselect behavior. Initialize only never-seen symbols with zeros.

Submit:

```javascript
symbol_entry_thresholds: parseSymbolEntryThresholdMap(
  formData.get('symbol_entry_thresholds') || '{}'
),
```

Ensure the API-loaded symbol metadata updates retained configuration without
overwriting unsaved edits made after page initialization.

**Step 5: Add responsive styling**

Change the risk rows to responsive cards or a grid that remains readable on
desktop and collapses to one or two columns below 760 px. Keep labels visible;
do not rely on placeholder text to identify the three threshold inputs.

**Step 6: Run page and asset tests**

Run:

```bash
pytest -q tests/test_web_page_render.py tests/test_web_assets_smoke.py
```

Expected: PASS.

**Step 7: Commit the settings UI**

```bash
git add src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js \
  src/telegram_kol_research/static/app.css \
  tests/test_web_page_render.py tests/test_web_assets_smoke.py
git commit -m "feat: edit fixed entry thresholds per symbol"
```

### Task 7: Run regression coverage and update operator documentation

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/context/telegram-deepcoin-auto-trading-context.md`
- Test: all files changed in Tasks 1-6

**Step 1: Document the new authority and rollback behavior**

Document:

- the three fixed values and long/short formulas;
- zero market threshold disables hybrid market conversion;
- new symbols default to zeros;
- explicit market and single-point nearby signals are unchanged;
- the old range percentage remains persisted only for rollback;
- only new entries are affected;
- BTC and ETH migration defaults;
- server verification must not create real positions.

**Step 2: Run formatting and focused tests**

Run:

```bash
git diff --check
pytest -q \
  tests/test_trading_settings.py \
  tests/test_deepcoin_order_builder.py \
  tests/test_recovery_execution_queue.py \
  tests/test_auto_trade_execution.py \
  tests/test_web_app.py \
  tests/test_web_page_render.py \
  tests/test_web_assets_smoke.py
```

Expected: no whitespace errors and all focused tests PASS.

**Step 3: Run the broader execution regression suite**

Run:

```bash
pytest -q \
  tests/test_recovery_live_submit.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_execution_bindings.py
```

If `tests/test_recovery_live_submit.py` does not exist, use:

```bash
rg --files tests | rg 'recovery.*submit|live_submit'
```

and run the discovered live-submission test module. Expected: PASS.

**Step 4: Review the final diff**

Run:

```bash
git status --short
git diff --stat HEAD
git diff HEAD -- \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/deepcoin_order_builder.py \
  src/telegram_kol_research/auto_trade_execution.py \
  src/telegram_kol_research/recovery_execution_queue.py \
  src/telegram_kol_research/recovery_live_submit.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/index.html \
  src/telegram_kol_research/static/app.js \
  src/telegram_kol_research/static/app.css \
  docs/migration-handoff.md \
  docs/context/telegram-deepcoin-auto-trading-context.md
```

Verify unrelated `uv.lock` and untracked files are absent from every feature
commit.

**Step 5: Request code review and address findings**

Use @requesting-code-review. Fix any correctness, migration, execution-safety,
or missing-test finding using TDD, then rerun Steps 2 and 3.

**Step 6: Commit documentation and any final test-only adjustments**

```bash
git add docs/migration-handoff.md \
  docs/context/telegram-deepcoin-auto-trading-context.md
git commit -m "docs: document fixed symbol entry thresholds"
```

### Task 8: Push and verify on the production server

**Files:**
- No new source files expected
- Verify: `scripts/server_git_update.ps1`

**Step 1: Confirm branch and commit scope**

Run:

```bash
git branch --show-current
git status --short
git log --oneline --decorate -10
```

Expected: branch is `codex/deepcoin-auto-trading-v1`; feature commits contain
only reviewed files; unrelated local changes remain unstaged.

**Step 2: Push the reviewed commits**

Run:

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds.

**Step 3: Prove a safe deployment window**

Use only existing read-only status/inspection paths to confirm there is no
active time-sensitive strategy operation. Do not create a Telegram test signal,
real order, test position, or management action. If safety cannot be proven,
stop before restart, record the exact pending server checks, and leave the
feature undeployed.

**Step 4: Update production**

From an approved Windows workstation, prefer:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the pushed commit, reinstalls the editable package,
and restarts `telegram-kol.service`.

**Step 5: Verify production without trading**

Verify:

- `/opt/telegram-kol-analyzer` is on the pushed commit;
- `telegram-kol.service` is active and running;
- the trading settings page loads;
- BTC shows `200 / 90 / 90`;
- ETH shows `4 / 2 / 2`;
- SOL and newly selected symbols show `0 / 0 / 0`;
- saving and reloading preserves a small decimal threshold;
- no malformed new execution or protection events appear after restart.

Do not submit a real entry merely to verify pricing. Use settings read-back,
rendered page state, local deterministic tests, and existing passive runtime
evidence.

**Step 6: Record deployment evidence**

Update the appropriate handoff/status note with the deployed commit, service
state, settings read-back result, and any verification intentionally deferred.
Commit and push that documentation only if it is part of the repository's
normal production evidence workflow.

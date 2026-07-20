# Task 4 report: trigger protection adoption visibility

## Delivered

- Exposed an explicit read-only `protection_adoption` projection in strategy details.
  It reports `adopted`, `refused`, or `unverified`, exact adopted TPSL `ordId`s,
  their evidence source, and refusal codes.
- Adopted orders are included only when the ledger row belongs to the same
  verified trigger-entry leg and exact `posId`; unrelated protection orders are
  not displayed.
- Added protected lifecycle-timeline events for successful adoption and refusal.
  The web summary timeline uses the same persisted ledger/audit evidence and
  shows the exact order id only for verified adoption.
- No exchange client is called and no replay, one-click action, or exchange
  mutation endpoint was introduced.

## TDD evidence

The new focused tests first failed with `KeyError: 'protection_adoption'`, then
passed after the read projection and timeline were added.

## Verification

```text
.venv/bin/python -m pytest tests/test_strategy_records.py tests/test_web_queries_messages.py tests/test_web_page_render.py -q
121 passed in 8.89s

.venv/bin/python -m compileall -q src/telegram_kol_research
```

## Scope

Committed files are limited to Task 4's strategy read model, web timeline
projection/template, and focused strategy-record tests. Existing unrelated
worktree changes were not staged.

## Follow-up: stale refusal position guard

- Refusal evidence in the strategy-detail projection now requires both the
  verified trigger-entry leg and its current exact `pos_id`, matching the web
  timeline projection.
- Added a regression test proving an old-position refusal on the same entry leg
  remains hidden and leaves the adoption state `unverified`.

### TDD and verification

The regression first failed because the stale refusal produced `state:
refused`; it passed after the exact-`pos_id` guard was added.

```text
.venv/bin/python -m pytest tests/test_strategy_records.py tests/test_web_queries_messages.py tests/test_web_page_render.py -q
122 passed in 7.07s

.venv/bin/python -m compileall -q src/telegram_kol_research
```

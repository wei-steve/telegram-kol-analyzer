# Management stop-price gate implementation plan

Goal: reject unsafe explicit management stops and conflicting break-even contracts before any component or exchange mutation.

Architecture: keep v2 contract schema and provenance unchanged. A shared validation module checks semantic consistency and finite positive prices, then validates long stop < fresh last price / short stop > fresh last price and absolute deviation in percentage points. Use actual entry averages only for implicit break-even and audit context; never fall back to them for live direction. Preserve profit-locking stops beyond entry. Persist dedicated reasons, evidence, and a deterministic Runtime Incident without enabling incident-agent actions.

Configuration: max_management_stop_deviation_pct defaults to 10 (10%); management_stop_quote_max_age_seconds defaults to 30. These are conservative configurable rejection bounds, not strategy optimization. Existing global settings JSON storage, no schema change.

1. Add regression tests in tests/test_management_stop_price_gate.py and planner integration tests; run and save RED.
2. Add shared checks, settings, provenance comment, planner and execution boundaries. Preserve stored uncertain records and reconciliation behavior.
3. Run focused tests and update old assertions only where the requested semantics intentionally changed. Review other numeric extraction paths read-only.
4. Run full pytest once on final production-code candidate. Record exact counts and bounded limitations; commit explicit paths on codex/management-stop-price-gate based on production af8676dc. No merge, push, deployment or live exchange calls.

Rollback: local candidate only; discard this independent worktree/branch if rejected. No production rollback operation required.

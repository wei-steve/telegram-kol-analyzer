# Strategy Record Workbench Navigation Implementation Plan

**Goal:** Make standalone strategy-record pages clearly link back to the full
five-destination workbench on desktop and mobile.

**Architecture:** Keep `/strategy-records` as a read-only standalone route and
add a small shared navigation partial. The links target the existing workbench
`view` query parameter, so no backend route or trading action changes are
needed.

**Tasks:**

1. Add a failing web test proving both the list and detail standalone pages
   expose `完整工作台` links for `策略`, `持仓`, `动态`, `群组`, and `更多`.
2. Add a shared `_strategy_record_workbench_nav.html` template and include it on
   the list and detail pages.
3. Wrap standalone record pages in `strategy-record-workbench-shell`, rendering
   the links as a desktop left rail and as a compact mobile link row with 44px
   touch targets.
4. Record the route contract in `docs/migration-handoff.md`.
5. Run focused strategy-record and asset tests.

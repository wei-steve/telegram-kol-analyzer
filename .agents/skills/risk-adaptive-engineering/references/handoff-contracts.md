# Handoff Contracts

## Common assignment

Every assignment states OBJECTIVE, RISK_LEVEL, AUTHORIZED_SCOPE, EXCLUSIONS,
ACCEPTANCE_CRITERIA, REQUIRED_EVIDENCE, STOP_CONDITIONS, EFFECTIVE_SANDBOX,
SESSION_MODE, and WORKTREE_BASELINE. The main Sol agent is the sole delegation owner; every
worker is forbidden to spawn agents or delegate work.

## Scout return

STATUS, RELEVANT_FILES, EXECUTION_PATH, DEPENDENCIES, EXISTING_TESTS, RISKS, EVIDENCE.

## Builder return

STATUS, FILES_CHANGED, IMPLEMENTATION, TESTS_RUN, TEST_RESULTS,
DEVIATIONS_FROM_PLAN, UNRESOLVED_RISKS.

## Reviewer return

VERDICT: PASS | FAIL, CRITICAL_FINDINGS, MAJOR_FINDINGS, MINOR_FINDINGS,
MISSING_TESTS, ACCEPTANCE_CRITERIA, RECOMMENDED_ACTION.

Findings cite concrete file and line evidence. The reviewer is dispatched only
under a verified effective read-only runtime. Any worktree mutation invalidates
the review.

## Tester return

VERDICT: PASS | FAIL, COMMANDS_RUN, TEST_RESULTS, REPRODUCTION_RESULT,
REGRESSIONS, FAILURES.

The tester may create ordinary test artifacts but never modifies application source.
The dispatcher compares the worktree against its baseline; application-source
changes make the result FAIL.

## Production verifier return

VERDICT: PASS | FAIL, DEPLOYED_REVISION, SERVICE_STATE, HEALTH_EVIDENCE,
SAFETY_GATE_EVIDENCE, REMAINING_RISKS.

The production verifier is dispatched only in a separate verified effective
read-only session. Its instructions prohibit deployment, restart, repair, and
compensation; if verification needs a write, it stops and reports the gap.

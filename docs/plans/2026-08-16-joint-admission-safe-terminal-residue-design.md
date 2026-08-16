# Joint Admission Safe Terminal Residue Design

## Goal

Allow the recovery-only Batch 119 joint admission to accept the production
shape of exactly 29 actionable bound-close reservations plus already-confirmed
safe residue, while preserving fail-closed treatment of every other status and
every true Batch 119 identity drift.

## Observed production facts

The approved read-only diagnosis proved two independent representation gaps:

- `bound_position_close_reservations` contains 38 rows: 29 rows in the closed
  actionable target-state set and 9 rows already in the safe terminal
  `confirmed` state.
- The Batch 119 target snapshot and management leg have numerically equal
  `quantity_step` values whose strings differ only in decimal representation.
  Every other target-snapshot identity predicate matched.

No production row, identifier, message, credential, or provider payload is
part of this design.

## Design

### Reservation population

The joint loader will inspect the full bounded reservation table in its
existing query-only transaction and partition rows using a closed status
contract:

- actionable target statuses remain the existing five recovery states;
- the only non-target safe status is exact `confirmed`;
- `NULL`, unknown, future, or any other status refuses admission;
- the actionable partition must contain exactly 29 rows;
- the complete table remains bounded and every row remains represented in the
  joint material fingerprint.

Only the 29 actionable rows are passed to the existing descendant-authority
loader. Confirmed residue is not granted apply authority and cannot contribute
raw reservation capability. Its complete canonical row material is included
in the admission fingerprint so any change between captures is detected.

### Numeric identity

`quantity_step` is a numeric identity field. The validator will parse both the
snapshot and management-leg forms with the existing bounded positive-decimal
validation and compare exact Decimal values rather than raw strings. Malformed,
zero, negative, or numerically different values continue to refuse. No other
Batch 119 identity predicate changes.

## Safety boundaries

- The ordinary Batch 119 loader retains global exclusivity.
- The production deployment preflight remains unchanged.
- The joint writer inventory still blocks all non-incident fresh, `NULL`,
  unknown, future, and all-age Deepcoin work.
- There is no database write, migration, bootstrap, exchange mutation,
  notification, message replay, or MiMo v2 activation.
- The recovery apply path can only receive the same 29 actionable rows; safe
  confirmed residue is observation-only.

## Verification

TDD regressions will prove:

- 29 actionable plus 9 confirmed rows are admitted;
- confirmed-residue field drift changes the joint fingerprint;
- an extra actionable row, `NULL`, unknown, or non-confirmed residue refuses;
- numerically equal `quantity_step` representations are admitted;
- a numerically different or malformed `quantity_step` refuses;
- ordinary Batch 119 recovery and deployment preflight behavior do not change.

After focused and adjacent tests pass, run compileall, `git diff --check`, the
full suite, and an independent Critical/Important review. Stop at the exact
push approval boundary; do not retry production with consumed permits.

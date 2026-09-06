# Full-Exit Wording Recognition Design

## Decision

Treat `全出` and `全部出` as complete-exit wording, including messages such as
`余仓全出`. The message must enter the existing full-exit lifecycle path; it
must not be interpreted as a partial take-profit merely because it mentions a
remaining position.

## Scope

The change is limited to the two shared heuristic checks used during lifecycle
recognition:

1. The full-exit guard used to prevent an AI `exit_position` decision being
   downgraded into a position update.
2. The explicit-exit parser used as the deterministic fallback.

Once a matching lifecycle has a verified live binding, the existing management
batch pipeline performs the exact-position market close. Existing same-chat,
symbol/side, ownership, and ambiguity guards remain unchanged.

## Safety

No symbol-only account position is newly eligible for closing. A message still
has to resolve to one lifecycle, and execution still requires its verified
binding. Ambiguous messages remain unacted on.

## Verification

Add focused tests showing that `余仓全出` is classified as an explicit exit and
that an AI full-exit decision for that wording produces a `full_exit` close
candidate instead of a position-update candidate. Run the focused recognition
tests and the relevant automated-execution tests locally. Production
verification occurs after deployment by inspecting the message's instruction
result, management batch, execution event, and Deepcoin position snapshot.

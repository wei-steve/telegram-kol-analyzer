# Managed TPSL Position Attribution Design

## Goal

Keep the UI, risk alerts, and management preflight aware of a stop-loss created
after a split position was opened, without weakening the safety rules for
unattributed exchange orders.

## Problem

Deepcoin can expose a position's attached TP and SL asymmetrically in the
positions response. A later management update may create a separate TPSL row
that has no returned `posId`. The existing matcher only associates such rows
with a position when their creation timestamp is close to the position's entry
timestamp. A legitimate management replacement therefore becomes invisible
after the time window expires.

## Design

When the management executor successfully creates replacement protection for a
known split-position leg, persist the exact returned order IDs and the known
`pos_id` in the protection ledger (the code already does this). Extend the
read-side attribution flow to accept those ledger rows as exact ownership
evidence for currently pending TPSL orders. Merge the ledger-confirmed rows
with inline position evidence, preserving all TP legs and the standalone SL.

Only a ledger row tied to the exact active position, strategy leg, and pending
exchange order may bypass time-based matching. All other unscoped exchange
TPSL rows continue through the existing conservative timestamp/size matching
and remain ambiguous when they cannot be safely attributed.

The shared attribution result will feed the exchange position panel and its
missing-stop alert, so a persisted managed stop is both displayed and excluded
from the false-positive risk count.

## Verification

Add a regression test with a position that has inline TP evidence and a
standalone SL created hours later. Without ledger ownership it must remain
unattributed; with exact ledger evidence it must produce one verified
protection view containing both the TP and SL. Add an integration-level UI
test for the same data to ensure the position is not labelled `无止损`.

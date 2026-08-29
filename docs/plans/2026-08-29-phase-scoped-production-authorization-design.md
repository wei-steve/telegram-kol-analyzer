# Phase-Scoped Production Authorization Design

**Date:** 2026-08-29  
**Status:** approved  
**Risk:** L0 documentation and workflow policy only

## Objective

Remove the repeated requirement to obtain a new user message for every normal
step of one production phase. A user may approve one coherent phase whose stated
scope covers its local work, integration, production reads, service control,
mutations, activation, and observation as applicable.

## Selected Approach

Keep one concise rule in `AGENTS.md` and one matching current-policy section in
the Deepcoin status document:

- normal steps explicitly included in an approved phase do not require repeated
  confirmation;
- exact SHA, manifests, backups, fresh evidence, rollback boundaries, and
  fail-closed unknown handling remain technical requirements;
- pause only when work materially expands beyond the approved phase or an
  irreversible action was not included in its scope.

Historical statements that a past batch did not push, deploy, restart, or write
production data remain audit facts. Repeated forward-looking authorization lists
are removed or replaced by the phase-scoped rule.

## Acceptance

- `AGENTS.md` no longer requires separate push, stage, activation, or
  exchange-semantics approval messages.
- The status document no longer repeats per-action authorization lists.
- Historical evidence and all technical safety invariants remain intact.
- No production code or production state changes.

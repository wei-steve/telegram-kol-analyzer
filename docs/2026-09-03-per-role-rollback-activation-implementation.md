# Per-role rollback activation implementation record

Date: 2026-09-03

Status: implementation, tests, and independent review complete; **not deployed**.

## Scope and immutable boundary

- Exact review base: `358e8187ab4c0f1066501af23cedf624e7b3b032`.
- Frozen production-code candidate: `800b4ed0a9e37724e73842456ace9507401ae405`.
- Design: `docs/plans/2026-09-03-per-role-rollback-activation-design.md`.
- Plan: `docs/plans/2026-09-03-per-role-rollback-activation.md`.
- No production activation or restart was performed. No activation authorization was created or consumed. No schema, business-data, decision, job, or exchange mutation was performed. The staged recognition-lease candidate and existing production rows were not touched.

## Implemented contract

- Activation manifests may add an exact `rollback_releases` map for `web`, `monitor`, `ingest`, and `worker`; each target binds a 40-character commit and its 64-character manifest SHA-256. Stage and activation manifests remain equal for every existing field.
- Canonical authorization v3 binds the complete per-role map, the independently reviewed controller commit, and the controller archive SHA-256. The existing v2 single-rollback contract remains accepted without acquiring v3-only fields.
- Web, ingest, and worker rollback identities require their live runtime identity, systemd PID/start identity, expected manifest, and immutable full-tree validation to agree.
- Monitor is proved separately from effective systemd configuration plus the latest matching successful diagnostic. The diagnostic timestamp must be strictly later than every effective monitor unit and drop-in file mtime; missing, stale, incomplete, or mismatched evidence fails closed.
- On activation failure, rollback publishes each component's own pre-authorized release. A failure restoring any component becomes `rollback_failed`, followed by best-effort stopping of the complete authority component set; completion is never reported for a partial rollback.
- Dry-run executes the complete read-only validation path without consuming authorization, publishing drop-ins, reloading systemd, or stopping/starting services.
- The standard Bash and PowerShell clients build a control archive from the exact reviewed controller commit, verify its members and SHA-256, and bind that identity separately from the runtime candidate.

The authority component-set rule, active-exchange-write gate, entry freeze semantics, runtime/manifest identity checks, immutable-tree checks, and post-control verification remain in force.

## RED and focused-test evidence

RED was established before implementation:

- The split-runtime four-component case using `rollback_releases` failed because the field was not accepted.
- The initial dry-run path failed before the side-effect-free branch existed.
- The initial partial-rollback test showed components could remain active after an incomplete restore.
- Independent review added a real archive-closure test. Before its fix, the exact controller archive failed with `No module named telegram_kol_research.deployment_activation_quiescence_check`.

The final focused run covered the action-plan contract, scoped activation, standard clients, exchange-authority contract, and activation quiescence checks:

```text
188 passed, 1 skipped
```

The skipped check requires PowerShell, which is unavailable in the local environment; PowerShell's archive and binding contract is also covered by static assertions and is kept member-for-member consistent with the executable Bash path.

## Controller dependency-closure correction

Independent review found that the first controller bundle did not contain the dependency closure needed by the activation quiescence subprocess. That candidate would have failed closed before service control, but it could not perform the intended activation.

The corrected bundle now includes a small standard-library-only `entry_revision_exchange_authority_contract.py`. The existing public parser/key API is re-exported through `entry_revision_exchange_authority.py`, while the quiescence process imports the dependency-light contract directly. A regression test assembles precisely the declared archive members and executes the module from that archive. It now reaches the expected controlled missing-database error instead of an import error.

The old and new idle predicates were independently compared across 364 edge inputs with zero mismatches.

## Full suite

The complete suite on frozen candidate `800b4ed0a9e37724e73842456ace9507401ae405` passed:

```text
6970 passed, 4 skipped, 32 warnings in 468.92s (0:07:48)
```

An earlier invocation from the isolated worktree had 15 environment-only failures because that worktree had no `.venv/bin/python`; it otherwise reported 6951 passed and 4 skipped. The suite was rerun with a temporary link to the repository's existing virtual environment. The command removed that link on exit, and the clean rerun above is the acceptance result.

## Independent safety review

The independent review compared exact base `358e8187ab4c0f1066501af23cedf624e7b3b032` with exact candidate `800b4ed0a9e37724e73842456ace9507401ae405` and reported:

```text
P0: 0
P1: 0
P2: 0
```

The review specifically rechecked authority scope, per-role immutable identity, monitor freshness, active-write and entry-freeze gates, v2/v3 authorization behavior, controller/runtime commit independence, dry-run non-mutation, per-component rollback failure handling, and the exact Bash/PowerShell controller archive member set. No existing safety gate was found weakened or bypassed.

## Remaining deployment boundary

This record proves only the reviewed local implementation. The production deployment blocker remains open until a separately authorized rollout installs and exercises this controller. Before any real activation, the production operator must re-establish live role identities, monitor diagnostic freshness, immutable-tree integrity, entry freeze, zero active exchange writes, exact authorization contents, and rollback targets. This implementation record is not activation authorization.

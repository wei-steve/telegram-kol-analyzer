# Monitor release identity convergence design

Date: 2026-09-04

## Decision

Use option A: remove the monitor-specific release identity from the monitor main-process
contract. The three monitor service units will inherit the same generic `PYTHONPATH`,
`TELEGRAM_KOL_RELEASE_COMMIT`, and `TELEGRAM_KOL_RELEASE_MANIFEST_SHA256` values that
`ExecStartPre` already verifies. `/etc/telegram-kol-monitor.env` remains the credentials and
monitor-policy file, but no longer contains release path, commit, or manifest values.

Option B was rejected because atomically keeping two independently persisted sources aligned
would still leave a synchronization invariant. It also creates a dry-run paradox: a dry-run
could either reject the currently stale env file or simulate rewriting it, but cannot both prove
the current effective main-process path and remain side-effect free. One release source removes
that invariant instead of moving it.

## Installed-unit migration boundary

The standard activator publishes release drop-ins but does not install base units. Therefore a
future production rollout must first run the existing, separately authorized monitor
install-only procedure while the monitor units are inactive. The updated installer will:

1. validate the exact immutable release and manifest as today;
2. write a policy/credential env file with no release-identity keys;
3. install the three service units whose `ExecStart` directly invokes the production virtualenv
   CLI and therefore inherits the generic release `PYTHONPATH` from the release drop-in;
4. remove only the three retired `TELEGRAM_KOL_MONITOR_RELEASE_*` assignments from existing
   release drop-ins using a root-owned, single-link, atomic rewrite; only an exact one-assignment
   line is removable, while malformed, duplicate, or trailing same-line assignments fail closed;
5. leave service start and authority activation to their existing bounded phases.

No installer or unit change is executed in this implementation-only turn.

The future migration must run while the monitor services and timer are inactive. After installing
the canonical units and removing the retired assignments, it must run a successful diagnostic for
the observed rollback commit/manifest. That diagnostic must be newer than the env file, every
monitor unit, and every release drop-in before any activation dry-run may use it. The migration
cannot reuse diagnostic evidence captured before the configuration rewrite.

The release-support digest normally requires byte-identical systemd units between candidate and
rollback. The old and new monitor commands differ only by removal of the exact legacy prefix
`/usr/bin/env PYTHONPATH=${TELEGRAM_KOL_MONITOR_RELEASE_PATH}/src`. Digest comparison will
canonicalize only that exact prefix in the three named monitor service units. Any other unit,
argument, sandbox, dependency, or configuration difference remains byte-significant and blocks
activation. After an activation failure, the installed canonical base unit reads the per-role
rollback drop-in's generic release variables, so rollback does not require reinstalling the old
unit.

## Dry-run proof

Add a candidate main-process proof before authorization consumption and before any service
control. `SystemRuntimeAdapter` will query each of the three monitor services with real
`systemctl show` output for `Environment`, `EnvironmentFiles`, `ExecStart`, `FragmentPath`, and
`DropInPaths`.

The candidate proof applies to every activation source mode containing monitor. Immutable mode
also retains the observed rollback proof and fresh diagnostic; `stopped_legacy` has no live
rollback identity to prove, but must still prove the prospective candidate main-process contract
before a dry-run can return `validated` or a real activation can consume authorization.

For each unit the proof must establish:

- the current effective command directly invokes the expected virtualenv CLI and contains no
  command-local `PYTHONPATH` override or monitor-specific release variable;
- `EnvironmentFiles` names an existing, root-owned regular file and the file defines none of
  `PYTHONPATH`, the two generic release keys, or the three retired monitor release keys;
- the effective environment contains a syntactically valid current generic release identity,
  contains no retired monitor release keys, and has an immutable release-shaped `PYTHONPATH`;
- overlaying the prospective candidate drop-in's three generic values produces exactly
  `<candidate release>/src`, candidate commit, and candidate manifest for the main process.

The existing rollback proof will apply the same main-process contract to the observed rollback
release and will continue to require a successful diagnostic newer than every monitor unit and
drop-in mtime. No diagnostic freshness, manifest, full-tree, PID, active-write, freeze, or
authorization gate is removed.

Errors are source-specific and fail closed: malformed or missing `systemctl show` properties,
missing EnvironmentFile, a release key in that file, a legacy main command, missing generic
identity, or a mismatched prospective import path reports the conflicting source/value category.
No secret or unrelated env value is included in the error.

For a release key in `EnvironmentFile`, the dry-run error records the offending key and its value,
the corresponding *generic* prospective drop-in key and value, and the requirement that the file
contain no release identity. For example, the retired monitor path is compared with prospective
`PYTHONPATH=<candidate>/src`, not with a monitor-specific field that the new drop-in no longer
writes. This makes the two conflicting sources explicit without exposing credentials or unrelated
policy values.

Under option A, “sources agree” means the effective generic identity and prospective generic
drop-in agree while the policy/credential EnvironmentFile contains no release key. Even a retired
monitor-specific key whose value happens to equal the candidate is rejected: accepting it would
retain the second identity source that this design removes.

## Freeze-window message loss record

The failed activation froze entry admission from `2026-09-04T08:56:33Z` until
`2026-09-04T09:21:02Z`. Raw messages 14795 and 14796 aged past the 15-minute stale-instruction
boundary with zero attempts and were terminalized as `expired_stale_instruction` after thaw.
Raw 14795 was a take-profit-related message in the same chat as active lifecycle 1074.

The stale-instruction limit must not be widened: executing old trading instructions is less safe
than dropping them. A separate future design should persist a deployment-freeze interval with a
stable identifier and timestamps, bind each enqueued job to whether its message arrived inside
that interval, and make thaw enumerate system-freeze-expired jobs for owner notification and
manual disposition. Existing `raw_messages.posted_at`, job `enqueued_at/completed_at/status`, and
the deployment freeze state provide the event timestamps, but they do not durably preserve which
specific freeze caused an expiry. A durable freeze-interval record or equivalently signed
deployment evidence reference is therefore required; this design does not add it.

## Verification and rollback

RED proves the current dry-run ignores a main-process source conflict. GREEN covers matching and
conflicting sources, missing keys/files, exact legacy-to-canonical digest compatibility, continued
rejection of unrelated unit changes, rollback diagnostic freshness, and all existing activation
safety gates. Run focused tests after each edit, then one complete pytest suite and an independent
exact-base security review.

Code rollback restores the prior installer, units, digest, and activator. No production state is
changed in this turn. A future production rollback after the install-only migration keeps the
canonical installed unit and changes only the generic per-role release drop-in; this is the
reason the narrow digest compatibility proof is required and tested.

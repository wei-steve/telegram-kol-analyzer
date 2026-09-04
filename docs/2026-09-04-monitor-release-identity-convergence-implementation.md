# Monitor release identity convergence implementation

Date: 2026-09-04

## Scope and commits

- Exact review base: `8c8e88390c0248b8b0368905a5363a1432c9f17c`.
- Design/plan commit: `30b0ece38333a40238a17cf59905bb5f7d3b4b69`.
- Initial implementation commit: `eaf542d1d11dda127d6a1ad7b26038acdbfecda7`.
- Independently reviewed production-code candidate after findings were resolved:
  `6a493d1588a2a4cdd34abfb2abd85580fc8f3b71`.

This turn changed local code, units, installer, tests, design, and the known-issues record only. It
did not stage a release, create or consume an activation authorization, write a production
drop-in, control or restart a service, change schema or business data, touch any
`execution_running` row, or call an exchange write path.

## Implemented contract

Option A was implemented. The monitor's three service units now invoke the production virtualenv
CLI directly and inherit the same generic release identity as other components:
`PYTHONPATH`, `TELEGRAM_KOL_RELEASE_COMMIT`, and
`TELEGRAM_KOL_RELEASE_MANIFEST_SHA256`. The monitor installer no longer writes the three
`TELEGRAM_KOL_MONITOR_RELEASE_*` values to `/etc/telegram-kol-monitor.env` and includes a bounded,
atomic cleanup that removes only an exact retired one-assignment line from existing monitor
release drop-ins. Duplicate, malformed, or same-line trailing assignments fail closed.

Every activation source mode containing monitor, including `stopped_legacy`, now runs a candidate
main-process proof before the active-write check, dry-run success return, authorization
consumption, or service control. The proof uses actual `systemctl show` properties for each monitor
service, reads the declared EnvironmentFile under strict metadata and syntax constraints, and
rejects:

- a missing or malformed Environment/EnvironmentFiles/ExecStart/FragmentPath/DropInPaths value;
- a missing or unsafe EnvironmentFile;
- any generic or retired release identity in that policy/credential file;
- a legacy or command-local `PYTHONPATH` ExecStart;
- missing or malformed current generic identity.

On an EnvironmentFile conflict, evidence names the old key/value and the corresponding real
prospective generic key/value. Unrelated environment values and credentials are not emitted.
Option A deliberately rejects a retired monitor key even when its value happens to match the
candidate; retaining it would recreate the second identity source.

The observed rollback proof still requires the exact generic rollback identity and a successful
diagnostic newer than the EnvironmentFile, every monitor unit, and every drop-in. Timer parsing
still treats `telegram-kol-monitor.timer` as processless and therefore does not require the
service-only Environment property.

Runtime-support digest compatibility normalizes only an exact legacy ExecStart prefix at the
start of a line, only in the three named monitor service files. All arguments, sandbox directives,
other unit files, comments, config, dependencies, and any other byte remain significant.

## RED and focused evidence

RED was recorded before each behavior change:

- split monitor main identity: `Failed: DID NOT RAISE ActivationError`;
- old v2 single-rollback authority activation did not call either monitor identity proof;
- all three base units still used the legacy command-local monitor path;
- installer had no retired-key cleanup and runtime-support digests rejected the exact migration;
- independent-review follow-up produced 5 expected failures: stopped-legacy dry-run was allowed,
  three conflict messages named the wrong prospective field, and a trailing assignment was
  deleted instead of rejected.

After the review fixes, the focused activation and monitor installer suite passed:

```text
123 passed, 1 skipped in 0.73s
```

`git diff --check` and `bash -n scripts/install_server_monitor.sh` also passed.

## Full suite

The first isolated-worktree full run reached `6979 passed, 4 skipped, 15 failed`; every failure
occurred before tested logic because deployment-script tests require `<worktree>/.venv/bin/python`
and the isolated worktree had no `.venv` path. A temporary, untracked symlink to the existing
project virtualenv was added only for test execution; both affected test files then passed
`46 passed, 1 skipped`.

With that harness correction, the pre-review-fix candidate passed `6994 passed, 4 skipped`. Because
the independent review required production-code changes, the complete suite was run again on the
final reviewed code candidate:

```text
6998 passed, 4 skipped, 32 warnings in 476.49s
```

The warnings are existing deprecation warnings. The temporary `.venv` symlink was removed and was
never staged.

## `systemctl show` parser audit

A repository-wide search covered all `systemctl show`, Environment, EnvironmentFiles, and monitor
timer parsing sites. `scoped_release_activation.py` is the only production parser that requests
the Environment property for this identity proof. Its timer path is explicitly guarded by
`_PROCESSLESS_UNITS` and requires only FragmentPath and DropInPaths. No second location was found
that incorrectly requires a service exec-context property from a timer.

## Independent security review

The initial exact-base review found P0=0, P1=1, P2=2. All findings were fixed:

- candidate proof now also covers `stopped_legacy`;
- cleanup requires an exact single assignment line;
- conflict evidence maps retired keys to the actual prospective generic keys;
- digest normalization was additionally anchored to a real line-start ExecStart.

The same independent reviewer then re-reviewed exact base
`8c8e88390c0248b8b0368905a5363a1432c9f17c` through candidate
`6a493d1588a2a4cdd34abfb2abd85580fc8f3b71` and reported **P0=0, P1=0, P2=0**. The reviewer also
re-ran the focused suite (`123 passed, 1 skipped`), `git diff --check`, and the installer syntax
check successfully. It found no weakening or bypass of rollback diagnostic freshness,
manifest/immutable identity, timer property, v2/v3 authorization, per-role rollback, freeze, or
active-write gates.

The remaining boundary is production-dependent rather than a review finding: tests fix the
systemd 255 output contract with realistic stubs, but installing the canonical units and running a
real production dry-run require a separate authorization. That future migration must generate a
fresh rollback diagnostic after every migrated unit/drop-in/env-file mtime; older diagnostic
evidence is not admissible.

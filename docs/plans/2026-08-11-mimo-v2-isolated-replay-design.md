# MiMo v2 Isolated Replay Design

Date: 2026-08-11

Status: approved

## Goal

Add a bounded server replay and benchmark command that compares MiMo v1 with
the rebuilt MiMo v2 contract on real message context and media without granting
execution authority or writing to the production database.

The command is a validation tool only. It must not listen for Telegram
messages, create a Deepcoin writer, invoke automatic trading, send a
notification, or change the live MiMo mode.

## Isolation boundary

The command accepts an existing source database, a read-only media root, an
explicit message-ID file, and a new empty artifact directory.

Before any model call, it opens the source database read-only and uses SQLite's
online backup API to create one consistent working database inside the artifact
directory. The source connection is then closed. All later prompt-registry,
run-attempt, recognition-experiment, and replay bookkeeping writes occur only
in the working copy.

The command never constructs a session factory against the source path. The
working database is disposable and is not an authority for production
execution.

Media files are read from the supplied media root. They are never copied into
artifacts, edited, renamed, or deleted. Artifacts contain no image bytes.

## Inputs and bounds

The CLI requires:

- `--database` pointing to an existing regular SQLite file;
- `--message-id-file` containing one positive raw-message ID per line;
- `--media-root` pointing to the corresponding read-only media tree;
- `--artifact-dir` naming a path that does not exist or is empty; and
- `--max-messages` between 1 and 200.

Blank lines and `#` comments are allowed in the ID file. Duplicate IDs are
removed without changing first-occurrence order. An empty list, malformed ID,
over-limit list, symlinked input/output boundary, missing message, or message
owned by another database fails before model calls.

No implicit recent-message filter is provided in Task 12. Corpus selection is
an explicit operator responsibility and therefore auditable.

## Replay flow

For each approved message, in order:

1. Confirm the message and referenced media exist in the isolated copy.
2. Build the same authoritative context used by the production recognition
   path.
3. Run the existing v1 MiMo inference against the working copy and time it.
4. Run strict MiMo v2 inference against the same working copy, context, and
   media and time it.
5. If v2 succeeds, pass it through the existing deterministic adapter and time
   that adapter separately.
6. Compare only the closed execution projection. Free-form summaries and
   reasons never determine mismatch safety.
7. Record one bounded comparison row and continue to the next approved ID.

The replay does not call the authoritative coordinator because that coordinator
can create candidates, lifecycle transitions, instruction items, claims, and
automatic-trading work. Direct v1/v2 inference plus the pure adapter gives the
required semantic and performance evidence without entering those mutation
paths.

## Comparison policy

The v1 and v2 compatibility projections are canonicalized from closed fields:

- recognition/action kind;
- strategy symbol, side, entry, stop, take profit, leverage, and order type;
- lifecycle event, target lifecycle/thread, management action and fraction;
- bounded instruction kinds and parameters; and
- entry-context/entry-fragment compatibility evidence.

Any executable projection difference is classified `unsafe_mismatch`. This
includes omitted v1 action, unsupported new v2 action, field/target drift,
partial/full-exit confusion, cancellation/close confusion, non-trading
promotion, and lost sibling action.

Matching no-action projections may differ in summary wording or informational
intent labels without becoming unsafe. Provider, JSON, contract, adapter,
missing-input, and image errors are recorded as validation failures and make
the command exit nonzero; they are not silently treated as safe matches.

## Performance gates

The summary reports bounded percentile calculations over successful comparable
runs:

- adapter P95 must be below 50 ms; and
- v2 end-to-end P95 must be no more than 115% of v1 P95.

If there are no comparable successful pairs, performance validation fails.
Percentiles use a deterministic nearest-rank calculation so local and server
results agree.

## Artifacts

The new artifact directory contains only:

- `replay.db`, the disposable working database copy;
- `comparisons.json`, bounded per-message statuses, timings, error codes and
  fingerprints;
- `comparisons.csv`, the same bounded comparison fields; and
- `summary.json`, counts, percentile gates and the overall result.

Artifacts exclude source message text, image bytes/data URLs, provider raw
responses, credentials, authorization headers, full prompt text, and exchange
responses. Errors are sanitized and bounded using existing error conventions.

Artifacts are written atomically where practical. A partial run remains
non-authoritative and the CLI exits nonzero.

## Prohibited dependencies

The replay module may import recognition/prompt, contract, run-audit, evidence
fingerprint, and pure adapter code. It must not import or construct:

- Telegram listener or synchronization workers;
- authoritative message processing or automatic-trade execution;
- Deepcoin trading/write clients;
- notification, operator-bot, or alert senders; or
- production deployment/settings writers.

Tests inspect module dependencies and patch known writer constructors to fail
if they are reached.

## CLI result

The command prints one compact JSON summary. It exits zero only when:

- every requested message was processed;
- every v1/v2 pair completed and adapted successfully;
- there are zero unsafe mismatches;
- both performance gates pass; and
- artifact validation reports zero forbidden content and zero production
  writes/notifications.

All other outcomes exit nonzero. The command does not enable MiMo v2, modify a
watermark, deploy code, or restart the production service.

## Testing

Tests must prove:

- source database bytes and metadata remain unchanged in a static fixture;
- all inference writes land in the working copy;
- bounded deterministic ID parsing and empty/new artifact enforcement;
- v1/v2 matching, unsafe mismatch, provider/contract/adapter failure and
  missing-image behavior;
- deterministic percentile and performance-gate results;
- JSON/CSV artifacts contain no text, image bytes, prompts, secrets, or raw
  responses;
- no listener, authoritative coordinator, Deepcoin writer or notifier can be
  invoked; and
- CLI success/failure exit codes reflect semantic and performance gates.

Server execution is not part of Task 12 implementation. It remains a later
deployment/verification step while production stays on `mimo_contract_mode=v1`.


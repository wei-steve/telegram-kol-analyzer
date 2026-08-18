# Runtime Serialization Remediation — Phase Index

**Goal:** Execute the runtime remediation designed in
`docs/plans/2026-08-18-runtime-serialization-remediation-design.md` as seven
independent phases, one per session, so that no session needs the previous
session's context.

**Read this file first, then open exactly one phase file.**

## How to run a phase session

Start a fresh session and give it only this instruction:

```text
读取 docs/runtime-serialization-remediation-status.md，按 current_phase_file 指定的
阶段文件执行该阶段。只做这一个阶段。
```

The phase file is self-contained: it restates the root cause it addresses, the
exact files and line anchors, the change, the verification, and the rollback. Do
not read the other phase files during a phase session — that is the entire point
of the split.

The one shared file every phase does use is
`2026-08-18-runtime-serialization-remediation/deployment-procedure.md`. Local
edit, push to GitHub, and a gated server-side updater is a fixed procedure, so it
is written once rather than copied into seven files where it would drift. Each
phase names its own `-ChangeClass` there.

## Phase order and what each one buys

| Phase | Name | Fixes | Expected effect |
|---|---|---|---|
| 0 | Loop health observability | — | Produces the measurement that proves 1 worked; finds any remaining blocking call |
| 1 | Unblock the event loop | Defect 1 | Removes whole-system stalls caused by Deepcoin latency |
| 2 | Per-chat lock sharding | Defect 2 | Groups stop blocking each other; missed entries from cross-group queueing stop |
| 3 | Compensation window repair | Defect 3, partial | Backlog after a stall is no longer silently expired |
| 4 | Durable job table, shadow enqueue | Defect 3, groundwork | Durable job state exists and is proven to match reality, still dormant |
| 5 | Queue consumer takeover | Defect 3, completion | Listener becomes enqueue-only; restarts stop losing in-flight messages |
| 6 | Process separation | Structural | Web traffic and recognition can no longer perturb execution timing |

Phases 1 and 2 are expected to remove the majority of the reported symptoms.
Stopping after phase 3 is a legitimate outcome if the symptoms are gone; phases
4 through 6 remove the remaining failure class rather than the common one.

## Invariants that hold across every phase

- **Claim the phase first.** Check `phase_status` and `claimed_by` in the status
  file. If another session holds it, stop and say so. See the claim protocol in
  that file — two sessions once took the same phase and collided.
- **Never `git add -A`.** Sessions share one checkout; stage explicit paths.
- One phase per user turn. Never begin the next phase in the same session.
- Behavior-changing phases ship dormant or flagged, with a tested disable path.
- Recognition and contextual resolution remain authoritative. This work changes
  when and on which thread they run, never what they decide.
- No deploy or restart during an active time-sensitive strategy operation.
- Server verification is the real verification. Local tests are necessary but
  not sufficient.
- Every phase ends by updating `docs/runtime-serialization-remediation-status.md`.

## Deployment in one line

Edit locally, push to GitHub, then run `scripts/server_git_update.ps1` from the
local machine with the pushed 40-hex commit and the phase's change class. The
server-side updater fetches that exact commit, runs the preflight gates, stops
`telegram-kol.service`, fast-forwards, reinstalls the package, and restarts.
Details and per-phase change classes are in `deployment-procedure.md`.

## Phase files

- `2026-08-18-runtime-serialization-remediation/deployment-procedure.md` (shared)
- `2026-08-18-runtime-serialization-remediation/phase-0-loop-health-observability.md`
- `2026-08-18-runtime-serialization-remediation/phase-1-unblock-event-loop.md`
- `2026-08-18-runtime-serialization-remediation/phase-2-per-chat-lock-sharding.md`
- `2026-08-18-runtime-serialization-remediation/phase-3-compensation-window-repair.md`
- `2026-08-18-runtime-serialization-remediation/phase-4-durable-job-shadow-enqueue.md`
- `2026-08-18-runtime-serialization-remediation/phase-5-queue-consumer-takeover.md`
- `2026-08-18-runtime-serialization-remediation/phase-6-process-separation.md`

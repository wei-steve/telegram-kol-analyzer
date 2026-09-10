# Observation scripts

Read-only samplers that back a phase's verification window. They are archived
here **exactly as they ran**, so a number in a script's header is what that run
believed, not necessarily what was measured.

## observe-6pre4.sh

Backed the L2 window for phase 6-pre-4 (silence probe before reconnecting).
Ran on the production host as `/root/observe-6pre4.sh <deploy-sha>`; samples and
the `DONE` marker land in `/root/evidence/phase-6-pre-4/`.

- **Window that counts**: 2026-09-10T09:08:45Z -> 09:51:57Z on `3d40a59a`,
  43m12s, 44 samples, no window resets, `healthy=1` and `head_ok=1` throughout.
- **Result**: 0 `silence_timeout` gaps, 4 probes, 4 passes.
- **Header correction**: the script says the baseline is 127 gaps in 24h. That
  was the figure from 6-pre-1's quantification. Measured directly before this
  deploy it was **130** in 24h and **3** in the preceding 30 minutes; the 3 is
  what the window compares against. The full record is the
  `phase-6-pre-4-completed` entry in `docs/rest-ws-trading-status.md`.
- **Two voided windows** precede it, archived on the host as
  `observer-samples-void-1.jsonl` (probe count doubled by a duplicate log line)
  and `observer-samples-void-2.jsonl` (`journalctl --since` read UTC as local
  time on a UTC+8 host). Both defects are written up in the status file.

Things this script does that were learned the hard way, and should be kept:

- Compares the production HEAD **live in every sample** (`head_ok`) rather than
  trusting the sha passed at startup. A deploy mid-window is otherwise invisible.
- Writes a `DONE` marker on exit instead of being polled with `pgrep -f`, whose
  pattern matches the checking command line itself.
- Counts a positive signal (`probe_passes`), not only the absence of the defect.
  A metric that falls has more than one possible cause.

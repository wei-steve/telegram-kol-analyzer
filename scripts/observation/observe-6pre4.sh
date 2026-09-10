#!/usr/bin/env bash
# Phase 6-pre-4 L2 observation. Read-only. DONE marker on exit (never pgrep).
#
# The verdict metric is unusually direct here: silence_timeout gaps. Baseline
# is 127 of them in 24h (1013s), and 3 in the 30 minutes before deploy. If the
# probe works, a 30-minute window should show far fewer -- ideally none.
set -u
DB=/opt/telegram-kol-analyzer/data/research.db
OUT=/root/evidence/phase-6-pre-4
mkdir -p "$OUT"
LOG="$OUT/observer-samples.jsonl"
DONE="$OUT/DONE"
rm -f "$DONE"
DEPLOY_SHA="${1:?deploy sha required}"
START=$(date -u +%s); WSTART=$START; CAP=$((24*3600))
q() { sqlite3 -readonly "$DB" "$1" 2>/dev/null; }
finish() { echo "{\"event\":\"$1\",\"at\":\"$(date -u '+%Y-%m-%dT%H:%M:%SZ')\",\"detail\":\"${2:-}\"}" | tee -a "$LOG" > "$DONE"; exit "${3:-0}"; }

while true; do
  NOW=$(date -u +%s); NOW_ISO=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  WS_ISO=$(date -u -d "@$WSTART" '+%Y-%m-%d %H:%M:%S')
  UNITS=1
  for u in telegram-kol-worker telegram-kol-web telegram-kol-ingest; do
    [ "$(systemctl is-active $u)" = "active" ] || UNITS=0
  done
  HEAD_NOW=$(cd /opt/telegram-kol-analyzer && git rev-parse HEAD 2>/dev/null)
  HEAD_OK=1; [ "$HEAD_NOW" = "$DEPLOY_SHA" ] || HEAD_OK=0

  MSGS=$(q "SELECT COUNT(*) FROM raw_messages WHERE created_at >= '$WS_ISO';")
  CHATS=$(q "SELECT COUNT(DISTINCT chat_id) FROM raw_messages WHERE created_at >= '$WS_ISO';")
  # The verdict metric, and its neighbours so a drop is not mistaken for the
  # stream having died in some other way.
  SILENCE_GAPS=$(q "SELECT COUNT(*) FROM deepcoin_ws_connection_gaps WHERE disconnected_at >= '$WS_ISO' AND reason='silence_timeout';")
  OTHER_GAPS=$(q "SELECT COUNT(*) FROM deepcoin_ws_connection_gaps WHERE disconnected_at >= '$WS_ISO' AND reason != 'silence_timeout';")
  OPEN_GAPS=$(q "SELECT COUNT(*) FROM deepcoin_ws_connection_gaps WHERE reconnected_at IS NULL;")
  FRAMES=$(q "SELECT COUNT(*) FROM deepcoin_ws_events WHERE received_at >= '$WS_ISO';")
  # Safety: the probe must not make entries wait more, nor leave writes unknown.
  WS_DEFERRED=$(q "SELECT COUNT(*) FROM message_instruction_items WHERE status='pending' AND retired_at IS NULL AND result_json LIKE '%ws_observation_pending%';")
  STUCK=$(q "SELECT COUNT(*) FROM instruction_execution_contracts WHERE state='submit_unknown';")
  CRITICALS=$(q "SELECT COUNT(*) FROM runtime_incidents WHERE severity='critical' AND created_at >= '$WS_ISO';")

  # 6-pre-4. A passing probe writes no gap row, so a drop in SILENCE_GAPS on
  # its own cannot say whether the probe held the connection or the stream was
  # simply busy. These come from the journal, the only place an individual
  # probe decision is recorded.
  # One pass over the journal, not two: the window grows to 30 minutes and a
  # second read of the same tens of thousands of lines every minute buys nothing.
  # journalctl reads --since in LOCAL time; WS_ISO is UTC and this host is
  # UTC+8, so passing it bare widened the query by eight hours and the very
  # first sample of a fresh window already counted three probes. Same shape as
  # the epoch misread earlier in this phase: a timestamp is not a number until
  # its zone is stated.
  read -r PROBES PROBE_PASSES <<<"$(journalctl -u telegram-kol-worker -u telegram-kol-ingest --since "$WS_ISO UTC" --no-pager 2>/dev/null | awk '
    /Deepcoin silence probe [a-z_]+ \(/ { t++ }
    /Deepcoin silence probe (missed_nothing|baseline_refreshed_after_frame) \(/ { p++ }
    END { printf "%d %d", t+0, p+0 }')"
  PROBES=${PROBES:-0}; PROBE_PASSES=${PROBE_PASSES:-0}

  HEALTHY=1
  [ "$UNITS" = "1" ] || HEALTHY=0
  [ "$HEAD_OK" = "1" ] || HEALTHY=0
  [ "${STUCK:-0}" = "0" ] || HEALTHY=0
  [ "${CRITICALS:-0}" = "0" ] || HEALTHY=0
  # An unclosed gap that outlives a probe interval means the stream is not
  # recovering -- that is a failure of this change, not a quiet success.
  [ "${OPEN_GAPS:-0}" = "0" ] || HEALTHY=0

  printf '{"at":"%s","deploy_sha":"%s","window_start":"%s","units_ok":%s,"head_ok":%s,"head_now":"%s","messages":%s,"chats":%s,"silence_gaps":%s,"other_gaps":%s,"open_gaps":%s,"frames":%s,"ws_deferred_entries":%s,"submit_unknown":%s,"criticals":%s,"probes":%s,"probe_passes":%s,"healthy":%s}\n' \
    "$NOW_ISO" "$DEPLOY_SHA" "$WS_ISO" "$UNITS" "$HEAD_OK" "${HEAD_NOW:0:12}" "$MSGS" "$CHATS" \
    "$SILENCE_GAPS" "$OTHER_GAPS" "$OPEN_GAPS" "$FRAMES" "$WS_DEFERRED" "$STUCK" "$CRITICALS" "$PROBES" "$PROBE_PASSES" "$HEALTHY" >> "$LOG"

  if [ "$HEALTHY" = "0" ]; then
    echo "{\"at\":\"$NOW_ISO\",\"event\":\"window_reset_unhealthy\",\"head_ok\":$HEAD_OK,\"open_gaps\":$OPEN_GAPS}" >> "$LOG"
    WSTART=$NOW
  elif [ $((NOW-WSTART)) -ge 1800 ] && [ "$MSGS" -ge 5 ]; then
    finish window_met "messages=$MSGS chats=$CHATS silence_gaps=$SILENCE_GAPS other_gaps=$OTHER_GAPS frames=$FRAMES probes=$PROBES probe_passes=$PROBE_PASSES (baseline 3 silence gaps per 30min)" 0
  fi
  [ $((NOW-START)) -ge $CAP ] && finish cap_reached_without_window "" 2
  sleep 60
done

#!/usr/bin/env bash
# Batch-loop driver for Semantic Internal Linker v3.
# Runs the script in --batch-size chunks so no single invocation blows the
# timeout, resuming from the sheet checkpoint until all candidates are done.
#
# Usage:
#   bash internal_linker_v3_batch.sh [--size N] [--max-runs N] [--dry-run] [--export-diffs]
#
# The sheet is the source of truth: anchors + statuses persist between runs,
# so re-running the loop is safe and idempotent.

SHEET="YOUR_SHEET_ID"
PY=python
SCRIPT=internal_linker_v3.py
BATCH_SIZE=25
MAX_RUNS=20
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --size) BATCH_SIZE="$2"; shift 2 ;;
    --max-runs) MAX_RUNS="$2"; shift 2 ;;
    --dry-run) EXTRA+=(--dry-run); shift ;;
    --export-diffs) EXTRA+=(--export-diffs); shift ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

echo "== v3 batch loop: size=$BATCH_SIZE max_runs=$MAX_RUNS extra=${EXTRA[*]} =="
for i in $(seq 1 "$MAX_RUNS"); do
  echo "--- run $i ---"
  "$PY" "$SCRIPT" --sheet "$SHEET" --inline --anchor --batch-size "$BATCH_SIZE" "${EXTRA[@]}"
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "!! run $i failed (rc=$rc) — stopping"
    exit $rc
  fi
  # stop when a run reports fewer pending than the batch size (all done)
  remaining=$("$PY" "$SCRIPT" --sheet "$SHEET" --inline --dry-run --batch-size "$BATCH_SIZE" 2>/dev/null | grep -oE "processing [0-9]+ of [0-9]+ pending" | tail -1)
  echo "   remaining: $remaining"
  n=$(echo "$remaining" | grep -oE "^processing [0-9]+" | grep -oE "[0-9]+" | head -1)
  if [ -n "$n" ] && [ "$n" -lt "$BATCH_SIZE" ]; then
    echo "== all pending candidates processed ($n < batch size) — done"
    exit 0
  fi
done
echo "== max-runs reached ($MAX_RUNS) — re-run the loop to continue =="

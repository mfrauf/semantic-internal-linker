#!/usr/bin/env bash
# Internal Linker weekly run — Hermes cron wrapper (no_agent mode).
# Runs the semantic similarity engine, then triggers the n8n digest webhook.
# Silent on success (n8n delivers the Telegram digest); prints ONLY on failure
# so the cron error-alert path surfaces real problems.
set -u
export PATH="link-venv/bin:/usr/bin:/bin:$PATH"
SHEET="YOUR_SHEET_ID_V1"
WEBHOOK="https://YOUR_N8N_HOST/webhook/internal-linker-digest"
LOG="/tmp/internal_linker_cron.log"

cd /path/to/your/project
python3 internal_linker.py --sheet "$SHEET" --threshold 0.4 --top-n 5 --fetch --anchor >"$LOG" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    echo "INTERNAL LINKER FAILED (exit $RC):"
    cat "$LOG"
    exit 1
fi

# Engine OK. If new candidates were appended, poke n8n to send the digest.
if grep -q "NEW candidates appended" "$LOG"; then
    curl -s -m 20 -X POST -A "Mozilla/5.0 (Hermes cron)" \
        -H "Content-Type: application/json" \
        -d '{"source":"hermes-cron"}' "$WEBHOOK" >/dev/null 2>&1
fi

# Silent on success — the n8n digest is the user-facing notification.
exit 0

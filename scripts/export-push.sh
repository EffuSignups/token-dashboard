#!/usr/bin/env bash
# export-push.sh — scan + delta-export + push to the VPS inbox. LMF (or any
# POSIX box). Fired by the Claude Code SessionEnd hook; safe to run by hand.
# Same spool-then-push failure model as export-push.ps1: nothing is deleted
# until scp succeeds, so failed pushes retry on the next session.
set -euo pipefail

export TOKEN_DASHBOARD_BOX="${TOKEN_DASHBOARD_BOX:-LMF}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="${SCRATCH_DIR:-$HOME/Claude/Projects/_scratch}"
SPOOL="$SCRATCH/token-dashboard-spool"
STAMP="$SCRATCH/token-dashboard-lastpush"
LOG="$SCRATCH/token-dashboard-push.log"
INBOX="vps:~/Claude/Projects/_scratch/token-dashboard-inbox/"

mkdir -p "$SPOOL"

# debounce: skip if we pushed in the last 15 minutes
if [ -f "$STAMP" ] && [ -n "$(find "$STAMP" -mmin -15 2>/dev/null)" ]; then
    exit 0
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

OUT="$SPOOL/td-${TOKEN_DASHBOARD_BOX}-$(date '+%Y%m%d-%H%M%S').json.gz"
cd "$REPO"
python3 cli.py export --out "$OUT" > /dev/null

pushed=0
for f in "$SPOOL"/*.json.gz; do
    [ -e "$f" ] || continue
    if scp -q "$f" "$INBOX"; then
        rm -f "$f"
        pushed=$((pushed + 1))
    else
        log "ERROR: scp failed for $f"
        exit 1
    fi
done
touch "$STAMP"
log "pushed $pushed file(s)"

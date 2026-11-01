#!/bin/sh
# Run on the bot server from cron, e.g. every 10 minutes:
#   */10 * * * * /opt/nyong1/atropos/sync_server_signals.sh >> /var/log/atropos_sync.log 2>&1
# Needs a clone of nyong1 at $REPO that can push (a deploy key with write access).
set -e
REPO=${REPO:-/opt/nyong1}
SRC=${SRC:-/opt/pantera_gate_bot/atropos_signals.jsonl}
BRANCH=${BRANCH:-claude/bitartropos-5min-tp-sl-0wuz5j}

cd "$REPO"
git fetch -q origin "$BRANCH"
git checkout -q "$BRANCH"
git merge -q --ff-only "origin/$BRANCH"
python3 atropos/import_server_log.py --src "$SRC"
git add atropos/server_signals.csv atropos/server_btc_raw.jsonl
if ! git diff --cached --quiet; then
    git commit -qm "Sync BTC Atropos signals from server ($(date '+%Y-%m-%d %H:%M'))"
    git push -q origin "$BRANCH"
fi

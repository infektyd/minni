#!/bin/bash
# Orchestrates the live IRL test. Assumes minnid is already up on
# /tmp/mlive/run/minnid.sock with the loadgen principal authored.
set -u
S=/tmp/claude-0/-home-user-minni/f649f307-b1e3-5ff9-ae58-fe4e1e7afc66/scratchpad
LIVE=$S/live
ART=$LIVE/artifacts
DIST=/home/user/minni/plugins/minni/dist
VENV=$S/venv/bin/python
mkdir -p $ART /tmp/mlive/teammate-vault

export MINNI_HOME=/tmp/mlive
export MINNI_DB_PATH=/tmp/mlive/minni.db
export MINNI_AGENT_VAULTS='{"mapped-bot": "/tmp/mlive/teammate-vault"}'

# 1. recorder (the artifact under test)
PYTHONPATH=/home/user/minni/src $VENV -m minni.minni_cli watch --json --interval 1 \
  > $ART/watch.jsonl 2> $ART/watch.err &
WATCH_PID=$!
sleep 3

# 2. workload start marker (everything before this is preflight, excluded)
date +%s.%N > $ART/START

# 3. both external workloads, concurrently
MINNI_BYPASS_AUDIT_LIMIT=true MINNI_HOME=/tmp/mlive \
  node $LIVE/audit_writer.mjs $DIST $ART > $ART/audit_writer.log 2>&1 &
AW_PID=$!
$VENV $LIVE/triage_bot.py $ART/ledger.jsonl > $ART/triage_bot.log 2>&1 &
TB_PID=$!

wait $AW_PID; AW_RC=$?
wait $TB_PID; TB_RC=$?
sleep 8   # let the recorder drain its final polls

# 4. post-hoc single-shot views (text-mode sanitization + --agent filter, live)
PYTHONPATH=/home/user/minni/src $VENV -m minni.minni_cli watch --once \
  > $ART/watch_text.out 2>/dev/null
PYTHONPATH=/home/user/minni/src $VENV -m minni.minni_cli watch --once --json \
  --agent mapped-bot > $ART/watch_agent_filter.jsonl 2>/dev/null

kill -INT $WATCH_PID 2>/dev/null   # SIGINT exercises the final-drain path
sleep 2
echo "workloads done (audit_writer=$AW_RC triage_bot=$TB_RC); verifying"
$VENV $LIVE/verify.py $ART $(cat $ART/START)

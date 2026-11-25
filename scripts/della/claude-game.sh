#!/bin/bash
# Supervise one paid Claude game on the Della login node, resuming after transport outages, until it ends.
#
# Usage (detached, from the repository): ( nohup bash scripts/della/claude-game.sh RUN_DIR DOLLAR_LIMIT > /dev/null 2>&1 < /dev/null & )
# Compute nodes cannot reach api.anthropic.com, so the game runs on the login node; it is one light
# Python process that mostly waits on HTTPS. The API key is read from an owner-only file into the
# environment, never into an argument. Writes RUN_DIR/supervisor.pid ("pid host") and RUN_DIR/supervisor.log.
# Exit 0: the game finished (completed or censored) or an operator STOP paused it. Exit 2: the run ended
# for a reason that needs a person (budget, an invalid run). Exit 3: transport outages outlasted the restarts.
set -euo pipefail
RUN_DIR=${1:?run directory}
DOLLAR_LIMIT=${2:?dollar ceiling for this run}
case "$RUN_DIR" in /*) ;; *) echo "RUN_DIR must be an absolute path, got $RUN_DIR"; exit 2 ;; esac
ROOT=/scratch/gpfs/ARORA/hh9077
REPO=$ROOT/clock-alignment
PY=$ROOT/envs/anthropic/bin/python
KEY_FILE=$ROOT/.secrets/anthropic_api_key
SEED=7
MAX_INPUT_TOKENS=124000      # as in games 4 to 6
MAX_OUTPUT_TOKENS=20480      # 5 x the 4,096 of earlier games (Helen, 2026-11-25); thinking counts toward it
FAMILY_TOKENS=327680         # 5 x the 65,536 of games 4 to 6, because thinking now counts against it
MAX_PAID_CALLS=10000         # above what the dollar ceiling buys, so dollars bind
TIMEOUT_SECONDS=900          # one xhigh reply can think for several minutes
MAX_RESTARTS=6               # transport outages the supervisor rides out before it stops
RESTART_WAIT_SECONDS=600

mkdir -p "$RUN_DIR"
LOG=$RUN_DIR/supervisor.log
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
echo "$$ $(hostname)" > "$RUN_DIR/supervisor.pid"
if [ ! -s "$KEY_FILE" ]; then log "no API key at $KEY_FILE"; exit 2; fi
ANTHROPIC_API_KEY=$(cat "$KEY_FILE")
export ANTHROPIC_API_KEY
export OMP_NUM_THREADS=1 PYTHONUNBUFFERED=1
cd "$REPO"

status_of() {
  "$PY" -c 'import json, sys
r = json.load(open(sys.argv[1]))
print(r["status"], "|", r.get("error") or "")' "$RUN_DIR/result.json"
}

restarts=0
while true; do
  RESUME=()
  if [ -f "$RUN_DIR/experiment.sqlite" ]; then RESUME=(--resume); fi
  log "starting runner ${RESUME[*]:-(new game)} with a \$$DOLLAR_LIMIT ceiling"
  set +e
  "$PY" -m clock_alignment claude --out "$RUN_DIR" --seed "$SEED" --allow-paid --dollar-limit "$DOLLAR_LIMIT" \
    --max-input-tokens "$MAX_INPUT_TOKENS" --max-output-tokens "$MAX_OUTPUT_TOKENS" --family-tokens "$FAMILY_TOKENS" \
    --max-paid-calls "$MAX_PAID_CALLS" --timeout-seconds "$TIMEOUT_SECONDS" "${RESUME[@]}" >> "$RUN_DIR/runner.out" 2>&1
  code=$?
  set -e
  if [ ! -f "$RUN_DIR/result.json" ]; then log "runner exited $code without a result"; exit 2; fi
  outcome=$(status_of)
  log "runner exited $code: $outcome"
  case "$outcome" in
    completed*|censored*) log "FINISHED"; exit 0 ;;
    paused*) log "paused by a STOP file"; exit 0 ;;
    "invalid | ProviderFailure: "*"no usable answer in"*)
      restarts=$((restarts + 1))
      if [ "$restarts" -gt "$MAX_RESTARTS" ]; then log "transport outages outlasted $MAX_RESTARTS restarts"; exit 3; fi
      log "transport outage; restart $restarts of $MAX_RESTARTS in $RESTART_WAIT_SECONDS s"
      sleep "$RESTART_WAIT_SECONDS" ;;
    *) log "stopped for a person to look"; exit 2 ;;
  esac
done

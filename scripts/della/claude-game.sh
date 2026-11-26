#!/bin/bash
# Supervise one paid Claude game on the Della login node, resuming after transport outages, until it ends.
#
# Usage (detached, from the repository): ( nohup bash scripts/della/claude-game.sh RUN_DIR DOLLAR_LIMIT MODEL > /dev/null 2>&1 < /dev/null & )
# Compute nodes cannot reach api.anthropic.com, so the game runs on the login node; it is one light
# Python process that mostly waits on HTTPS. The API key is read from an owner-only file into the
# environment, never into an argument. Writes RUN_DIR/supervisor.pid ("pid host") and RUN_DIR/supervisor.log.
# Exit 0: the game finished (completed or censored) or an operator STOP paused it. Exit 2: the run ended
# for a reason that needs a person (budget, an invalid run). Exit 3: transport outages outlasted the restarts.
set -euo pipefail
RUN_DIR=${1:?run directory}
DOLLAR_LIMIT=${2:?dollar ceiling for this run}
MODEL=${3:?Claude model: claude-haiku-5-5 or claude-opus-5-5}
case "$RUN_DIR" in /*) ;; *) echo "RUN_DIR must be an absolute path, got $RUN_DIR"; exit 2 ;; esac
ROOT=/scratch/gpfs/ARORA/hh9077
REPO=$ROOT/clock-alignment
PY=$ROOT/envs/anthropic/bin/python
KEY_FILE=$ROOT/.secrets/anthropic_api_key
SEED=7
# A prompt over the input cap stops the run (nothing is ever cut from a prompt), so the cap only has
# to sit above the longest prompt. First-cycle estimates are dollars for night 1 and day 1.
# Game 6 never bound its output limits: its longest reply used 41% of the per-call cap, and its
# busiest family 78% of its allowance over 7 days. Each model's limits keep that headroom.
case "$MODEL" in
  claude-opus-5-5)
    MAX_INPUT_TOKENS=124000      # as in games 4 to 6
    MAX_OUTPUT_TOKENS=20480      # 5 x the 4,096 of earlier games (Helen, 2026-11-25); thinking counts toward it
    FAMILY_TOKENS=327680         # 5 x the 65,536 of games 4 to 6, because thinking now counts against it
    FIRST_CYCLE_ESTIMATE=28 ;;   # the paused 2026-11-25 attempt's night 1 and day 1 measured about $27
  claude-haiku-5-5)
    # Game 6's longest prompt (91,337 Qwen tokens) is about 150,000 Claude tokens at the 1.64 ratio
    # measured over the first 112 calls; 300,000 leaves room for longer notes and later days.
    MAX_INPUT_TOKENS=300000
    # Haiku's first 59 calls (2026-11-25) wrote up to 16,581 output tokens and charged families about
    # 1,890 tokens per call, 8.5 x game 6's 222; game 6's busiest family made 229 calls, so about
    # 433,000 tokens. 16 x the game 6 limits keeps players from being cut off (Helen, 2026-11-25).
    # By day 3 output per call had risen to ~12,400 and one reply reached 61,357, so Helen raised them
    # (2026-11-26) to the API maximum per call and 64 x game 6 per family; claude-haiku2-20261125 got
    # them mid-game through `amend-limits`, and a new game starts with them.
    MAX_OUTPUT_TOKENS=128000
    FAMILY_TOKENS=4194304
    FIRST_CYCLE_ESTIMATE=1 ;;    # $0.43 estimated from game 6's prompt sizes, rounded up to whole dollars
  *) echo "unknown MODEL $MODEL; use claude-haiku-5-5 or claude-opus-5-5"; exit 2 ;;
esac
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
  log "starting $MODEL runner ${RESUME[*]:-(new game)} with a \$$DOLLAR_LIMIT ceiling"
  set +e
  "$PY" -m clock_alignment claude --model "$MODEL" --out "$RUN_DIR" --seed "$SEED" --allow-paid --dollar-limit "$DOLLAR_LIMIT" \
    --max-input-tokens "$MAX_INPUT_TOKENS" --max-output-tokens "$MAX_OUTPUT_TOKENS" --family-tokens "$FAMILY_TOKENS" \
    --max-paid-calls "$MAX_PAID_CALLS" --timeout-seconds "$TIMEOUT_SECONDS" \
    --first-cycle-estimate "$FIRST_CYCLE_ESTIMATE" "${RESUME[@]}" >> "$RUN_DIR/runner.out" 2>&1
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

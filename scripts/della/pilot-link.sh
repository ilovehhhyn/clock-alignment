#!/bin/bash
#SBATCH --account=arora --job-name=ca-pilot -N1 --ntasks=1 -c8 --mem=64G --gres=gpu:1
#SBATCH --time=00:59:00 --qos=gpu-test
#SBATCH --output=/scratch/gpfs/ARORA/hh9077/logs/%x-%j.out
# Run one resumable link of a local Qwen game, pausing before the link's time limit.
#
# Required environment: RUN_DIR (one game's output directory) and MAX_DAYS (announced final day).
# Optional: MODEL (a vLLM profile in clock_alignment/config.py; its pinned revision is read from
# there), LINK_MINUTES (the link's wall time; the game pauses 14 minutes before it), SEED,
# MAX_INPUT_TOKENS (the per-call input cap; the server's context is this plus 4096), FAMILY_TOKENS (the
# whole-game allowance of an original and its children), ROPE_FACTOR (static YaRN factor over a
# 32768-token base for a context past the model's native one) and TENSOR_PARALLEL (the GPUs the server
# splits the model over; it must equal the GPUs the job holds, so submit with --gres=gpu:N).
# A finished game exits without starting a server. Any server death ends the job: its calls
# fail, the runner stops the run, and the EXIT trap stops every background child. The trap
# signals children only, so the script's own exit code reaches Slurm's afterok chain intact.
set -euo pipefail
: "${RUN_DIR:?set RUN_DIR to the game directory}" "${MAX_DAYS:?set MAX_DAYS to the announced final day}"
case "$RUN_DIR" in /*) ;; *) echo "RUN_DIR must be an absolute path, got $RUN_DIR"; exit 2 ;; esac
SEED=${SEED:-7}
MODEL=${MODEL:-Qwen/Qwen3-4B-Instruct-2507}
LINK_MINUTES=${LINK_MINUTES:-59}
SHUTDOWN_MINUTES=14                   # one in-flight call, the report and the server shutdown
ROOT=/scratch/gpfs/ARORA/hh9077
REPO=$ROOT/clock-alignment
ENV=$ROOT/envs/vllm
PY=$ENV/bin/python
MAX_INPUT_TOKENS=${MAX_INPUT_TOKENS:-48000}
FAMILY_TOKENS=${FAMILY_TOKENS:-32768}
TENSOR_PARALLEL=${TENSOR_PARALLEL:-1}
MAX_MODEL_LEN=$((MAX_INPUT_TOKENS + 4096))   # the config's input cap plus its per-call output cap
ROPE=()
if [ -n "${ROPE_FACTOR:-}" ]; then
  ROPE=(--hf-overrides "{\"rope_scaling\":{\"rope_type\":\"yarn\",\"factor\":$ROPE_FACTOR,\"original_max_position_embeddings\":32768}}")
fi
HEALTH_TIMEOUT_SECONDS=$((15 * 60))
if [ "$LINK_MINUTES" -le "$SHUTDOWN_MINUTES" ]; then
  echo "LINK_MINUTES must exceed $SHUTDOWN_MINUTES, got $LINK_MINUTES"; exit 2
fi
PAUSE_AFTER_SECONDS=$(( (LINK_MINUTES - SHUTDOWN_MINUTES) * 60 ))
GPUS_HELD=$(nvidia-smi -L | wc -l)
if [ "$GPUS_HELD" -ne "$TENSOR_PARALLEL" ]; then
  echo "TENSOR_PARALLEL must equal the GPUs the job holds: TENSOR_PARALLEL=$TENSOR_PARALLEL, held $GPUS_HELD; submit with --gres=gpu:$TENSOR_PARALLEL"; exit 2
fi
export HF_HOME=$ROOT/hf_cache HF_HUB_OFFLINE=1 HF_TOKEN_PATH=/dev/null
export VLLM_USE_FLASHINFER_SAMPLER=0 FLASHINFER_WORKSPACE_BASE=$ROOT/flashinfer-cache
export PATH=$ENV/bin:$PATH OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
cd "$REPO"
REVISION=$("$PY" -c 'import sys
from clock_alignment.config import MODEL_PROFILES
profile = MODEL_PROFILES.get(sys.argv[1])
if profile is None or profile.provider != "vllm" or profile.revision is None:
    sys.exit(f"MODEL must name a vLLM profile with a pinned revision, got {sys.argv[1]!r}")
print(profile.revision)' "$MODEL")
VLLM_API_KEY=$("$PY" -c 'import secrets; print(secrets.token_hex(32))')
export VLLM_API_KEY LOCAL_LLM_API_KEY=$VLLM_API_KEY
trap 'kill $(jobs -p) 2> /dev/null || true' EXIT

LINK=$RUN_DIR/links/${SLURM_JOB_ID:-manual}
mkdir -p "$LINK"
if [ -f "$RUN_DIR/result.json" ] && "$PY" -c 'import json, sys
sys.exit(0 if json.load(open(sys.argv[1]))["status"] in ("completed", "censored") else 1)' "$RUN_DIR/result.json"; then
  echo "game in $RUN_DIR already finished; nothing to run"
  exit 0
fi
PORT=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
echo "model $MODEL revision $REVISION, context $MAX_MODEL_LEN, family tokens $FAMILY_TOKENS, rope ${ROPE[*]:-none}, tensor parallel $TENSOR_PARALLEL, link of $LINK_MINUTES minutes, pause after $PAUSE_AFTER_SECONDS s"
nvidia-smi --query-gpu=name,memory.total --format=csv > "$LINK/gpu.txt"
nvidia-smi --query-gpu=timestamp,memory.used,utilization.gpu --format=csv -l 15 > "$LINK/gpu-usage.csv" &
"$ENV/bin/vllm" serve "$MODEL" --revision "$REVISION" --served-model-name "$MODEL" \
  --host 127.0.0.1 --port "$PORT" --max-model-len "$MAX_MODEL_LEN" "${ROPE[@]}" \
  --tensor-parallel-size "$TENSOR_PARALLEL" --gpu-memory-utilization 0.85 --seed 0 > "$LINK/vllm.log" 2>&1 &
SERVER=$!
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
until curl -sf "http://127.0.0.1:$PORT/health" > /dev/null; do
  if ! kill -0 "$SERVER" 2> /dev/null; then echo "vLLM exited during startup"; tail -n 40 "$LINK/vllm.log"; exit 3; fi
  if [ "$SECONDS" -ge "$deadline" ]; then echo "vLLM not healthy after $HEALTH_TIMEOUT_SECONDS s"; exit 4; fi
  sleep 5
done
echo "vLLM healthy after $SECONDS s on port $PORT"
nvidia-smi --query-gpu=memory.used --format=csv > "$LINK/gpu-after-load.txt"

RESUME=()
if [ -f "$RUN_DIR/experiment.sqlite" ]; then RESUME=(--resume); fi
rm -f "$RUN_DIR/STOP"
( sleep $((PAUSE_AFTER_SECONDS > SECONDS ? PAUSE_AFTER_SECONDS - SECONDS : 60)) && touch "$RUN_DIR/STOP" ) &
set +e
"$PY" -m clock_alignment local --out "$RUN_DIR" --seed "$SEED" --max-days "$MAX_DAYS" --model "$MODEL" \
  --max-input-tokens "$MAX_INPUT_TOKENS" --family-tokens "$FAMILY_TOKENS" \
  --endpoint "http://127.0.0.1:$PORT" "${RESUME[@]}" > "$LINK/result.json"
STATUS=$?
set -e
"$PY" -m clock_alignment report --out "$RUN_DIR" > "$LINK/report.json" || echo "report failed; see $LINK"
"$PY" -c 'import json, sys
r = json.load(open(sys.argv[1]))
print("link result:", r["status"], "day", r["days"], "requests", r["requests"], "error", r["error"])' "$LINK/result.json" || true
echo "runner exit code $STATUS"
exit "$STATUS"

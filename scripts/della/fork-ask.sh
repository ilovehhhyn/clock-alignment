#!/bin/bash
#SBATCH --account=griffith --job-name=ca-fork -N1 --ntasks=1 -c8 --mem=64G --gres=gpu:1 --constraint=gpu80
#SBATCH --time=00:30:00 --qos=gpu-test
#SBATCH --output=/scratch/gpfs/ARORA/hh9077/logs/%x-%j.out
# Serve the game's model on this node and ask forked copies of saved decisions why they said what they said.
# The live ledger is only copied (SQLite backup API); nothing is written to the run directory.
# Required environment: RUN_DIR and MODEL. Optional: PER_KIND (cases per kind, default 6), MAX_INPUT_TOKENS,
# ROPE_FACTOR and TENSOR_PARALLEL (the GPUs the server splits the model over; it must equal the GPUs the
# job holds, so submit with --gres=gpu:N). Use the game's own values.
set -euo pipefail
: "${RUN_DIR:?set RUN_DIR to the game directory}" "${MODEL:?set MODEL to the vLLM profile of the game}"
ROOT=/scratch/gpfs/ARORA/hh9077
REPO=$ROOT/clock-alignment
ENV=$ROOT/envs/vllm
PY=$ENV/bin/python
MAX_INPUT_TOKENS=${MAX_INPUT_TOKENS:-96000}
MAX_MODEL_LEN=$((MAX_INPUT_TOKENS + 4096))
ROPE=()
if [ -n "${ROPE_FACTOR:-}" ]; then
  ROPE=(--hf-overrides "{\"rope_scaling\":{\"rope_type\":\"yarn\",\"factor\":$ROPE_FACTOR,\"original_max_position_embeddings\":32768}}")
fi
TENSOR_PARALLEL=${TENSOR_PARALLEL:-1}
HEALTH_TIMEOUT_SECONDS=$((15 * 60))
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
print(MODEL_PROFILES[sys.argv[1]].revision)' "$MODEL")
VLLM_API_KEY=$("$PY" -c 'import secrets; print(secrets.token_hex(32))')
export VLLM_API_KEY LOCAL_LLM_API_KEY=$VLLM_API_KEY
trap 'kill $(jobs -p) 2> /dev/null || true' EXIT

STAMP=$(date +%Y%m%d-%H%M%S)
PROBES=$ROOT/clock-runs/probes
mkdir -p "$PROBES"
SNAPSHOT=$PROBES/$(basename "$RUN_DIR")-fork-$STAMP.sqlite
"$PY" -c 'import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()' \
  "$RUN_DIR/experiment.sqlite" "$SNAPSHOT"
PORT=$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
"$ENV/bin/vllm" serve "$MODEL" --revision "$REVISION" --served-model-name "$MODEL" \
  --host 127.0.0.1 --port "$PORT" --max-model-len "$MAX_MODEL_LEN" "${ROPE[@]}" \
  --tensor-parallel-size "$TENSOR_PARALLEL" --gpu-memory-utilization 0.85 --seed 0 > "$PROBES/fork-$STAMP-vllm.log" 2>&1 &
SERVER=$!
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
until curl -sf "http://127.0.0.1:$PORT/health" > /dev/null; do
  if ! kill -0 "$SERVER" 2> /dev/null; then echo "vLLM exited during startup"; tail -n 40 "$PROBES/fork-$STAMP-vllm.log"; exit 3; fi
  if [ "$SECONDS" -ge "$deadline" ]; then echo "vLLM not healthy after $HEALTH_TIMEOUT_SECONDS s"; exit 4; fi
  sleep 5
done
echo "vLLM healthy after $SECONDS s; snapshot $SNAPSHOT"
"$PY" scripts/fork_ask.py "$SNAPSHOT" "$PROBES/fork-$STAMP.json" --endpoint "http://127.0.0.1:$PORT" \
  --model "$MODEL" --per-kind "${PER_KIND:-6}"

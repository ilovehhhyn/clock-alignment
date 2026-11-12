#!/bin/bash
# Submit LINKS resumable links for one game; each starts only after the previous succeeded.
# Usage (on the Della login node, from the repository): scripts/della/submit-chain.sh RUN_DIR MAX_DAYS LINKS
# Environment: ACCOUNT (default arora), QOS (default gpu-test; Della chooses the partition from it),
# LINK_MINUTES (default 59; the link's wall time, passed to the job), MODEL (a vLLM profile name),
# MAX_INPUT_TOKENS, FAMILY_TOKENS and ROPE_FACTOR (passed to the job), GPUS (default 1; the job's --gres and the
# server's tensor parallel size), MEM (default 64G), CONSTRAINT (for example gpu80), and AFTER (a job the first
# link waits for). The link ids are appended to RUN_DIR/chain.txt for stop-game.sh.
set -euo pipefail
RUN_DIR=${1:?run directory}
MAX_DAYS=${2:?announced final day}
LINKS=${3:?number of links}
ACCOUNT=${ACCOUNT:-arora}
QOS=${QOS:-gpu-test}
LINK_MINUTES=${LINK_MINUTES:-59}
MODEL=${MODEL:-Qwen/Qwen3-4B-Instruct-2507}
MAX_INPUT_TOKENS=${MAX_INPUT_TOKENS:-48000}
FAMILY_TOKENS=${FAMILY_TOKENS:-32768}
GPUS=${GPUS:-1}
MEM=${MEM:-64G}
previous=${AFTER:-}
printf -v wall '%02d:%02d:00' $((LINK_MINUTES / 60)) $((LINK_MINUTES % 60))
options=(--account="$ACCOUNT" --qos="$QOS" --time="$wall" --gres="gpu:$GPUS" --mem="$MEM")
if [ -n "${CONSTRAINT:-}" ]; then options+=(--constraint="$CONSTRAINT"); fi
for index in $(seq 1 "$LINKS"); do
  dependency=()
  if [ -n "$previous" ]; then dependency=(--dependency="afterok:$previous"); fi
  previous=$(sbatch --parsable "${options[@]}" "${dependency[@]}" \
    --export="ALL,RUN_DIR=$RUN_DIR,MAX_DAYS=$MAX_DAYS,MODEL=$MODEL,LINK_MINUTES=$LINK_MINUTES,MAX_INPUT_TOKENS=$MAX_INPUT_TOKENS,FAMILY_TOKENS=$FAMILY_TOKENS,ROPE_FACTOR=${ROPE_FACTOR:-},TENSOR_PARALLEL=$GPUS" \
    scripts/della/pilot-link.sh)
  echo "link $index: job $previous"
  echo "$previous" >> "$RUN_DIR/chain.txt"
done

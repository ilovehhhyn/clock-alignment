#!/bin/bash
# Stop one game: pause the running link through its STOP file and cancel the chain's other links.
# Usage (on the Della login node): scripts/della/stop-game.sh RUN_DIR
# The link ids come from RUN_DIR/chain.txt, written by submit-chain.sh; the ledger is never touched.
set -euo pipefail
RUN_DIR=${1:?run directory}
CHAIN=$RUN_DIR/chain.txt
[ -f "$CHAIN" ] || { echo "no $CHAIN; cancel the links by id yourself"; exit 2; }
touch "$RUN_DIR/STOP"
pending=$(squeue -u "$USER" -h -o "%i %t" | awk 'NR==FNR {ids[$1]; next} ($1 in ids) && $2 != "R" {print $1}' "$CHAIN" -)
if [ -n "$pending" ]; then scancel $pending; echo "cancelled pending links: $pending"; fi
running=$(squeue -u "$USER" -h -o "%i %t" | awk 'NR==FNR {ids[$1]; next} ($1 in ids) && $2 == "R" {print $1}' "$CHAIN" -)
echo "STOP written; running link(s) will pause at the next decision: ${running:-none}"

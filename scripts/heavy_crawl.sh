#!/usr/bin/env bash
# Resumable private web collection and eligible article extraction. Usage: heavy_crawl.sh <until KST HH:MM> <block-name> [--optional]
# cron (box): 00:00-03:00 and 11:00-14:00 KST, optional 22:00-24:00 (runs when work is due). flock + timeout in cron.
# Public web prefers the zecbot tunnel. Confirmed local tunnel failure selects box egress.
# Extraction shares the daily Qwen-first provider chain and request ledger. KG_IG=0.
set -uo pipefail
KG_HOME="${KG_HOME:-/workspace/kitchen-genome}"
cd "$KG_HOME" || exit 1
mkdir -p data_store/logs
exec 9>"$KG_HOME/data_store/heavy_crawl.lock"
if ! flock -n 9; then echo "[$(date -Is)] HEAVY=skipped reason=locked block=${2:-?}"; exit 0; fi
export KG_IG=0
export KG_SERVICE_DB="${KG_SERVICE_DB:-$KG_HOME/data_store/kg_service.db}"
echo "[$(date -Is)] start block=${2:-manual} until=$1"
PYTHONPATH=daemon python3 -m kg_collect.heavy --db "$KG_SERVICE_DB" run --until "$1" --block "${2:-manual}" ${3:-} 2>>data_store/logs/heavy_crawl.err
echo "[$(date -Is)] end rc=$?"

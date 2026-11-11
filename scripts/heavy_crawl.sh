#!/usr/bin/env bash
# Heavy web collection block (plain script, no LLM). Usage: heavy_crawl.sh <until KST HH:MM> <block-name> [--optional]
# cron (box): 00:00-03:00 and 11:00-14:00 KST, optional 22:00-24:00 (runs only when the backlog is large). flock + timeout in cron.
# All public web goes through the zecbot SOCKS tunnel; if it is down the run is skipped and logged (no direct fallback).
# Never touches DeepSeek, git, deploy or Instagram.
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

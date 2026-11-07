#!/usr/bin/env bash
# In-place Kitchen Genome collection. No checkout, commit, merge, or deploy.
# DeepSeek web runs even when Instagram is skipped. Never calls the legacy Instagram account.
set -euo pipefail
REPO="${1:-.}"
KG_HOME="${KG_HOME:-/workspace/kitchen-genome}"
KG_DB="${KG_SERVICE_DB:-$KG_HOME/data_store/kg_service.db}"
export KG_SERVICE_DB="$KG_DB"
cd "$REPO"
AI_ARGS=()
[ "${KG_DEEPSEEK:-1}" != 1 ] || AI_ARGS=(--deepseek)
if [ "${KG_COLLECT:-1}" = 1 ] && [ -f "$KG_DB" ]; then
  COLLECT_JSON=$(mktemp)
  COLLECT_RC=0
  PYTHONPATH=daemon timeout "${KG_COLLECT_TIMEOUT:-1500}" python3 -m kg_collect --db "$KG_DB" cycle \
    --units "${KG_COLLECT_UNITS:-60}" --cost-cap "${KG_COLLECT_COST_CAP:-78}" --no-browser "${AI_ARGS[@]}" >"$COLLECT_JSON" 2>/dev/null || COLLECT_RC=$?
  if [ "$COLLECT_RC" = 0 ]; then
    COLLECT_OUT=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); s=d.get("ai_stats") or {}; print("COLLECT="+d["status"], "run="+str(d.get("run_id")), "ai="+d.get("ai","disabled"), "reason="+d.get("ai_reason","none"), "cost="+str(d.get("cost_used")), "ai_docs=%s/%s/%s/%s/%s" % (s.get("considered",0), s.get("skipped_filter",0), s.get("called",0), s.get("failed",0), s.get("findings",0)))' "$COLLECT_JSON") || COLLECT_OUT='COLLECT=failed reason=invalid_summary'
  else
    COLLECT_OUT="COLLECT=failed reason=exit_$COLLECT_RC"
  fi
  rm -f "$COLLECT_JSON"
else
  COLLECT_OUT='COLLECT=skipped reason=disabled_or_missing_db ai=disabled'
fi
echo "$COLLECT_OUT"
if [ "${KG_IG:-1}" = 1 ] && [ -f "$KG_DB" ]; then
  IG_RC=0
  IG_OUT=$(PYTHONPATH=daemon timeout --kill-after=15 900 python3 crawler/instagram_kg.py --db "$KG_DB" \
    --cost-cap "${KG_IG_COST_CAP:-20}" "${AI_ARGS[@]}" 2>/dev/null) || IG_RC=$?
  if [ "$IG_RC" = 124 ] || [ "$IG_RC" = 137 ]; then
    IG_OUT=$(PYTHONPATH=daemon python3 crawler/instagram_kg.py --db "$KG_DB" --mark-timeout 2>/dev/null) || true
    IG_OUT="IG_FAILED account=kitchen.genome reason=timeout"
  fi
  IG_OUT=$(printf '%s\n' "$IG_OUT" | sed -n '/^IG_\(OK\|SKIPPED\|BLOCKED\|FAILED\) /p' | tail -1)
  if [ -z "$IG_OUT" ]; then
    IG_OUT="IG_FAILED account=kitchen.genome reason=exit_$IG_RC"
    PYTHONPATH=daemon python3 crawler/instagram_kg.py --db "$KG_DB" --mark-timeout >/dev/null 2>&1 || true
  fi
else
  IG_OUT='IG_SKIPPED account=kitchen.genome reason=disabled_or_missing_db'
fi
echo "$IG_OUT"
exit 0

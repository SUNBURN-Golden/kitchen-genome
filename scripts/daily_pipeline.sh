#!/usr/bin/env bash
# Daily deterministic + pending-only collection. Publishes a reviewable PR; never merges/deploys.
set -euo pipefail
REPO="${1:-/tmp/kg}"; KG_HOME="${KG_HOME:-/workspace/kitchen-genome}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
STAGE=resolve; COLLECT_STATE=skipped; IG_STATE=skipped; REVIEW_PENDING=unknown; FINAL=0
finish() {
  local rc=$?
  if [ "$FINAL" = 0 ]; then
    echo "RESULT=FAILED stage=$STAGE collect=$COLLECT_STATE ig=$IG_STATE reason=command_failed exit=$rc"
  fi
}
trap finish EXIT
if [ ! -d "$REPO/.git" ] && [ ! -f "$REPO/.git" ]; then
  if [ -d "$KG_HOME/.git" ]; then echo "WARN: missing clone; using $KG_HOME"; REPO="$KG_HOME"
  else echo 'FATAL: no clone available'; exit 2; fi
fi
[ "${KG_RESOLVE_ONLY:-0}" = 1 ] && { echo "REPO=$REPO"; FINAL=1; exit 0; }
cd "$REPO"
STAGE=git
GIT_COMMON=$(git rev-parse --git-common-dir)
exec 9>"$GIT_COMMON/kg_daily.lock"
if ! flock -n 9; then FINAL=1; echo 'RESULT=SKIPPED reason=locked'; exit 0; fi
if ! git diff --quiet || ! git diff --cached --quiet; then echo 'FATAL: tracked changes present'; exit 2; fi
DAY=$(TZ=Asia/Seoul date +%Y%m%d); BR="crawl/$DAY-$(date +%H%M%S)-$$"
git checkout -q main
git pull -q --ff-only
git checkout -q -b "$BR"
KG_DB="${KG_SERVICE_DB:-$KG_HOME/data_store/kg_service.db}"
export KG_SERVICE_DB="$KG_DB" KG_HOME
STAGE=crawl
python3 crawler/crawl.py
# In-place collector. It does not checkout, and web collection does not wait on Instagram.
STAGE=collect
COLLECT_LOG=$(bash "$SCRIPT_DIR/collect_daily.sh" "$REPO" || true)
printf '%s\n' "$COLLECT_LOG"
COLLECT_OUT=$(printf '%s\n' "$COLLECT_LOG" | sed -n '/^COLLECT=/p' | tail -1)
IG_OUT=$(printf '%s\n' "$COLLECT_LOG" | sed -n '/^IG_/p' | tail -1)
[ -n "$COLLECT_OUT" ] || COLLECT_OUT='COLLECT=failed reason=missing_summary'
[ -n "$IG_OUT" ] || IG_OUT='IG_FAILED account=kitchen.genome reason=missing_summary'
# Machine registry edits ship separately through human-reviewed PRs.
git checkout -q -- sources 2>/dev/null || true
COLLECT_STATE=${COLLECT_OUT#COLLECT=}; COLLECT_STATE=${COLLECT_STATE%% *}
IG_STATE=${IG_OUT%% *}; IG_STATE=${IG_STATE#IG_}
STAGE=daemon
AI_ARGS=()
[ "${KG_DEEPSEEK:-1}" != 1 ] || AI_ARGS=(--deepseek)
if [ "${KG_COLLECT:-1}" = 1 ] && [ -f "$KG_DB" ]; then
  DAEMON_RC=0
  PYTHONPATH=daemon timeout 600 python3 -m kg_daemon --db "$KG_DB" run --collect-db "$KG_DB" \
    --cost-cap "${KG_DAEMON_COST_CAP:-20}" "${AI_ARGS[@]}" >/dev/null 2>&1 || DAEMON_RC=$?
  echo "DAEMON=finished exit=$DAEMON_RC"
  REVIEW_PENDING=$(PYTHONPATH=daemon python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("SELECT COUNT(*) FROM review_queue WHERE state=?",("open",)).fetchone()[0])' "$KG_DB" 2>/dev/null) || REVIEW_PENDING=unknown
fi
STAGE=apply
python3 crawler/apply.py
python3 data/enrich_en.py
STAGE=build
python3 scripts/build_deploy.py >/dev/null
grep -q '^const SHOW_VERDICTS=false;' web/app.js
grep -q '^const SHOW_NEWSLETTER=false;' web/app.js
if grep -rqi 'noindex' web; then echo 'GUARD FAIL: noindex found'; exit 3; fi
STAGE=pr
# Stage only publication artifacts (new files included); private inbox/session/runtime output never enters git.
git add -A -- data/seed.json crawler/pending.json crawler/applied.json web
if git diff --cached --quiet; then
  FINAL=1; echo "RESULT=NO_CHANGE review_pending=$REVIEW_PENDING collect=$COLLECT_STATE ig=$IG_STATE"; exit 0
fi
SUM="daily deterministic collection; private leads require review"
git commit -q -m "crawl $DAY: $SUM"
git push -q -u origin "$BR"
PR=$(gh pr create --repo SUNBURN-Golden/kitchen-genome --base main --head "$BR" --title "crawl $DAY: review collection" \
  --body 'Daily deterministic collection. AI and dedicated Instagram findings stay in the private pending inbox. Build guards passed. Human review required; squash merge is a later action.')
FINAL=1
echo "RESULT=PR_CREATED pr=$PR review_pending=$REVIEW_PENDING collect=$COLLECT_STATE ig=$IG_STATE"

#!/usr/bin/env bash
# Kitchen Genome daily pipeline: crawl -> apply -> build -> PR/squash-merge -> deploy prep.
# Headless up to the Cloudflare deploy; the deploy itself needs the user-Cloudflare connector (see README "자동 크롤러").
# Usage: bash scripts/daily_pipeline.sh [/path/to/clone]   (default /tmp/kg; falls back to /workspace/kitchen-genome when that path is gone,
# e.g. /tmp/kg symlink lost after a box restore — the 2026-11-03 07:00 run died on `cd /tmp/kg`)
# Exit 0 always on success; last line is one of: RESULT=NO_CHANGE | RESULT=MERGED_NO_DEPLOY pr=<url> | RESULT=NEEDS_DEPLOY pr=<url> | RESULT=DEPLOYED pr=<url>
# Collection is in-place and never calls the legacy Instagram account. AI findings never apply through the rule path;
# the DeepSeek review stage (daemon/kg_ai/review.py) reviews each one: approved -> seed `ai_reviewed` (badged 'AI 검토',
# source URL, data/ai_review_log.jsonl for rollback), rejected -> closed with reason. Nothing stays pending.
set -euo pipefail
REPO="${1:-/tmp/kg}"; KG_HOME="${KG_HOME:-/workspace/kitchen-genome}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CF_TOKEN="${KG_CF_TOKEN_FILE:-/home/box/.secrets/cloudflare_pages_token}"
STAGE=resolve; FINAL=0
finish() {
  local rc=$?
  if [ "$FINAL" = 0 ]; then
    echo "RESULT=FAILED stage=$STAGE reason=command_failed exit=$rc"
  fi
}
trap finish EXIT
if [ ! -d "$REPO/.git" ] && [ ! -f "$REPO/.git" ]; then
  if [ -d "$KG_HOME/.git" ]; then echo "WARN: $REPO is not a clone; using $KG_HOME"; REPO="$KG_HOME"
  else echo "FATAL: no clone at $REPO or $KG_HOME"; exit 2; fi
fi
[ "${KG_RESOLVE_ONLY:-0}" = 1 ] && { echo "REPO=$REPO"; FINAL=1; exit 0; }
cd "$REPO"
STAGE=git
GIT_COMMON=$(git rev-parse --git-common-dir)
exec 9>"$GIT_COMMON/kg_daily.lock"
if ! flock -n 9; then FINAL=1; echo 'RESULT=SKIPPED reason=locked'; exit 0; fi
if ! git diff --quiet || ! git diff --cached --quiet; then echo 'FATAL: tracked changes present'; exit 2; fi
DAY=$(date +%Y%m%d); BR="crawl/$DAY"
git checkout -q main && git pull -q --ff-only
git branch -D "$BR" >/dev/null 2>&1 || true; git checkout -q -b "$BR"
KG_DB="${KG_SERVICE_DB:-$KG_HOME/data_store/kg_service.db}"
export KG_SERVICE_DB="$KG_DB" KG_HOME
ai_review() {
  # DeepSeek review of open AI findings; its cost counts within KG_AI_DAILY_COST_CAP (max 20).
  if [ "${KG_DEEPSEEK:-1}" = 1 ] && [ "${KG_AI_REVIEW:-1}" = 1 ] && [ -f "$KG_DB" ]; then
    local out
    out=$(PYTHONPATH=daemon timeout 600 python3 -m kg_ai --db "$KG_DB" --seed "$REPO/data/seed.json" --log "$REPO/data/ai_review_log.jsonl" 2>/dev/null) \
      || out="AI_REVIEW failed exit=$?"
    printf '%s\n' "$out" | sed -n '/^AI_REVIEW/p' | tail -1
  fi
}
STAGE=ai_review_backlog
ai_review   # drain yesterday's findings first so new collection cannot starve the review
STAGE=crawl
python3 crawler/crawl.py
# Web collection does not wait on Instagram. The legacy account is not invoked.
STAGE=collect
COLLECT_LOG=$(bash "$SCRIPT_DIR/collect_daily.sh" "$REPO" || true)
printf '%s\n' "$COLLECT_LOG"
COLLECT_OUT=$(printf '%s\n' "$COLLECT_LOG" | sed -n '/^COLLECT=/p' | tail -1)
IG_OUT=$(printf '%s\n' "$COLLECT_LOG" | sed -n '/^IG_/p' | tail -1)
[ -n "$COLLECT_OUT" ] || COLLECT_OUT='COLLECT=failed reason=missing_summary'
[ -n "$IG_OUT" ] || IG_OUT='IG_FAILED account=kitchen.genome reason=missing_summary'
echo "$IG_OUT"
# Machine registry edits ship separately through human-reviewed PRs.
git checkout -q -- sources 2>/dev/null || true
STAGE=daemon
AI_ARGS=()
[ "${KG_DEEPSEEK:-1}" != 1 ] || AI_ARGS=(--deepseek)
if [ "${KG_COLLECT:-1}" = 1 ] && [ -f "$KG_DB" ]; then
  DAEMON_RC=0
  PYTHONPATH=daemon timeout 600 python3 -m kg_daemon --db "$KG_DB" run --collect-db "$KG_DB" \
    --cost-cap "${KG_DAEMON_COST_CAP:-20}" "${AI_ARGS[@]}" >/dev/null 2>&1 || DAEMON_RC=$?
  echo "DAEMON=finished exit=$DAEMON_RC"
fi
STAGE=ai_review
ai_review
STAGE=apply
python3 crawler/apply.py
python3 data/enrich_en.py
STAGE=build
python3 scripts/build_deploy.py >/dev/null
# guards: verdicts and newsletter stay hidden, no noindex anywhere
grep -q '^const SHOW_VERDICTS=false;' web/app.js
grep -q '^const SHOW_NEWSLETTER=false;' web/app.js
if grep -rqi 'noindex' web; then echo "GUARD FAIL: noindex found"; exit 3; fi
STAGE=pr
git add -A data crawler web scripts .gitignore
if git diff --cached --quiet; then git checkout -q main; git branch -D "$BR" -q; echo "$IG_OUT"; FINAL=1; echo "RESULT=NO_CHANGE"; exit 0; fi
WEB_CHANGED=0; git diff --cached --quiet -- web || WEB_CHANGED=1
SUM=$(python3 -c "import json;s=json.load(open('crawler/out/last_summary.json'));print('; '.join(f\"{a['name']}: {a['kind']} {a.get('new_name') or a.get('field') or ''}\" for a in s['applied']) or 'pending queue only', '| pending', s['pending_count'])")
git commit -q -m "crawl $DAY: $SUM"
git push -q -u origin "$BR" --force
PR=$(gh pr create --repo SUNBURN-Golden/kitchen-genome --base main --head "$BR" --title "crawl $DAY: $SUM" \
  --body "Automated daily crawl (scripts/daily_pipeline.sh). Applied only with >=2 independent reliable sources; see crawler/applied.json. Review queue: crawler/pending.json. AI findings are published only after the DeepSeek review stage approves them (badged 'AI 검토', source URL, data/ai_review_log.jsonl; rollback: scripts/ai_review_rollback.py); rejected ones are closed with a reason. SHOW_VERDICTS/SHOW_NEWSLETTER=false, no noindex (guards passed).")
gh pr merge "$PR" --squash --delete-branch
git checkout -q main && git pull -q --ff-only
python3 scripts/build_deploy.py >/dev/null   # dist/deploy_kitchengenome.js from merged main
echo "$IG_OUT"
FINAL=1
if [ "$WEB_CHANGED" = 1 ]; then
  if [ -f "$CF_TOKEN" ]; then
    ./deploy.sh main >/dev/null && echo "RESULT=DEPLOYED pr=$PR" || echo "RESULT=NEEDS_DEPLOY pr=$PR"
  else
    echo "RESULT=NEEDS_DEPLOY pr=$PR"
  fi
else
  echo "RESULT=MERGED_NO_DEPLOY pr=$PR"
fi

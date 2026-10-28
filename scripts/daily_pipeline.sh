#!/usr/bin/env bash
# Kitchen Genome daily pipeline: crawl -> apply -> build -> PR/squash-merge -> deploy prep.
# Headless up to the Cloudflare deploy; the deploy itself needs the user-Cloudflare connector (see README "자동 크롤러").
# Usage: bash scripts/daily_pipeline.sh [/path/to/clone]   (default /tmp/kg)
# Exit 0 always on success; last line is one of: RESULT=NO_CHANGE | RESULT=MERGED_NO_DEPLOY pr=<url> | RESULT=NEEDS_DEPLOY pr=<url>
set -euo pipefail
REPO="${1:-/tmp/kg}"; cd "$REPO"
DAY=$(date +%Y%m%d); BR="crawl/$DAY"
git checkout -q main && git pull -q --ff-only
git branch -D "$BR" >/dev/null 2>&1 || true; git checkout -q -b "$BR"
python3 crawler/crawl.py
IG_OUT=$(timeout 900 python3 crawler/instagram.py 2>&1 | grep -E '^IG_(OK|SKIPPED|BLOCKED)' | tail -1) || true
[ -n "$IG_OUT" ] || IG_OUT="IG_BLOCKED reason=ig_step_crashed_or_timeout"
echo "$IG_OUT"   # routine: on IG_BLOCKED, tell 준태님 and notify BabyShower; never retry the same day
# Kitchen Genome collection bot: one polite cycle into the SEPARATE, git-ignored service DB (never data/, web/ or the site). Non-fatal by design:
# any failure/timeout prints COLLECT=... and the daily flow continues unchanged. Disable with KG_COLLECT=0. Machine-written registry growth is
# discarded here (it ships through reviewed PRs), so this clone stays clean for tomorrow's ff-only pull.
KG_DB="${KG_SERVICE_DB:-/workspace/kitchen-genome/data_store/kg_service.db}"
if [ "${KG_COLLECT:-1}" = 1 ] && [ -f "$KG_DB" ]; then
  COLLECT_OUT=$(PYTHONPATH=daemon timeout "${KG_COLLECT_TIMEOUT:-1500}" python3 -m kg_collect --db "$KG_DB" cycle --units "${KG_COLLECT_UNITS:-60}" 2>/dev/null \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print('COLLECT='+str(d.get('status')), 'run='+str(d.get('run_id')), 'cost='+str(d.get('cost_used')))" 2>/dev/null) || true
  [ -n "$COLLECT_OUT" ] || COLLECT_OUT="COLLECT=failed_or_timeout (non-fatal)"
  git checkout -q -- sources 2>/dev/null || true
  echo "$COLLECT_OUT"
else
  echo "COLLECT=skipped (KG_COLLECT=0 or no service DB at $KG_DB)"
fi
python3 crawler/apply.py
python3 data/enrich_en.py
python3 scripts/build_deploy.py >/dev/null
# guards: verdicts and newsletter stay hidden, no noindex anywhere
grep -q '^const SHOW_VERDICTS=false;' web/app.js
grep -q '^const SHOW_NEWSLETTER=false;' web/app.js
if grep -rqi 'noindex' web; then echo "GUARD FAIL: noindex found"; exit 3; fi
git add -A data crawler web scripts .gitignore
if git diff --cached --quiet; then git checkout -q main; git branch -D "$BR" -q; echo "$IG_OUT"; echo "RESULT=NO_CHANGE"; exit 0; fi
WEB_CHANGED=0; git diff --cached --quiet -- web || WEB_CHANGED=1
SUM=$(python3 -c "import json;s=json.load(open('crawler/out/last_summary.json'));print('; '.join(f\"{a['name']}: {a['kind']} {a.get('new_name') or a.get('field') or ''}\" for a in s['applied']) or 'pending queue only', '| pending', s['pending_count'])")
git commit -q -m "crawl $DAY: $SUM"
git push -q -u origin "$BR" --force
PR=$(gh pr create --repo SUNBURN-Golden/kitchen-genome --base main --head "$BR" --title "crawl $DAY: $SUM" \
  --body "Automated daily crawl (scripts/daily_pipeline.sh). Applied only with >=2 independent reliable sources; see crawler/applied.json. Review queue: crawler/pending.json. SHOW_VERDICTS/SHOW_NEWSLETTER=false, no noindex (guards passed).")
gh pr merge "$PR" --squash --delete-branch
git checkout -q main && git pull -q --ff-only
python3 scripts/build_deploy.py >/dev/null   # dist/deploy_kitchengenome.js from merged main
echo "$IG_OUT"
if [ "$WEB_CHANGED" = 1 ]; then
  if [ -f /home/box/.secrets/cloudflare_pages_token ]; then
    ./deploy.sh main >/dev/null && echo "RESULT=DEPLOYED pr=$PR" || echo "RESULT=NEEDS_DEPLOY pr=$PR"
  else
    echo "RESULT=NEEDS_DEPLOY pr=$PR"
  fi
else
  echo "RESULT=MERGED_NO_DEPLOY pr=$PR"
fi

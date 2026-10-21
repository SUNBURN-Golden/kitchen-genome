#!/usr/bin/env bash
# Redeploy web/ to Cloudflare Pages project 'kitchengenome' (free).
# Needs a Cloudflare API token with Pages:Edit:  CLOUDFLARE_API_TOKEN=... ./deploy.sh
# (Agents without a token: use the user-Cloudflare connector -> upload-token JWT ->
#  /pages/assets/upload + upsert-hashes -> POST deployments with manifest; see git history/notes.)
set -euo pipefail
cd "$(dirname "$0")"
export CLOUDFLARE_ACCOUNT_ID=aa6bf084982ee841c3ea39b45895a15e
OUT=$(mktemp -d); cp web/{index.html,app.js,style.css,data.js} "$OUT"/
cp concept-v1/{robots.txt,sitemap.xml,rss.xml} "$OUT"/ 2>/dev/null || true
npx -y wrangler@latest pages deploy "$OUT" --project-name kitchengenome --branch main --commit-dirty=true
for u in https://kitchengenome.pages.dev https://kitchengenome.soulbound.co.kr; do
  echo "$u $(curl -s -o /tmp/kg.html -w '%{http_code}' "$u") $(grep -o '<title>[^<]*' /tmp/kg.html)"
done

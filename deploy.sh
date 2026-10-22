#!/usr/bin/env bash
# Redeploy web/ + functions/ to Cloudflare Pages project 'kitchengenome' (free).
set -euo pipefail
cd "$(dirname "$0")"
export CLOUDFLARE_ACCOUNT_ID=aa6bf084982ee841c3ea39b45895a15e
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] && [ -f /home/box/.secrets/cloudflare_pages_token ]; then
  export CLOUDFLARE_API_TOKEN="$(tr -d '\r\n' < /home/box/.secrets/cloudflare_pages_token)"
fi
mkdir -p node_modules/.cache/wrangler
python3 scripts/build_pages.py
python3 scripts/build_pages_en.py
python3 scripts/build_feeds.py
npx -y wrangler pages deploy web --project-name kitchengenome --branch "${1:-main}" --commit-dirty=true
for u in https://kitchengenome.pages.dev https://kitchengenome.soulbound.co.kr https://kitchengenome.soulbound.co.kr/api/me; do
  echo "$u $(curl -s -o /tmp/kg.html -w '%{http_code}' "$u") $(head -c 120 /tmp/kg.html | tr '\n' ' ')"
done

# 키친 게놈 (Kitchen Genome)

서울 셰프 계보 트래커. 유명 주방 출신 셰프들이 연 독립 식당을 계보도로 잇고, 공개 데이터 기반 '저평가 / 과대평가 / 균형' 판정과 '다음에 뜰 집'을 함께 보여줍니다.

A Seoul chef-lineage tracker: maps alumni of top kitchens to the restaurants they opened, with public-data verdicts (undervalued / overrated / balanced) and a "next to rise" list.

## 구조 / Layout
- `web/` — 정적 사이트 (index.html, app.js, style.css, data.js) / static site root
- `data/` — 시드 데이터와 빌드 스크립트, `outreach/` 식당 확인 절차 / seed data, build scripts, restaurant verification outreach
- `deploy.sh` — 수동 배포 / manual deploy

## 데이터 재빌드 / Data rebuild
```
cd data
python3 build_data.py && python3 enrich_1.py && python3 enrich_2.py && python3 enrich_3.py
```
순서대로 실행 / run in this order.

## 배포 / Deploy
- Cloudflare Pages 프로젝트 `kitchengenome` → https://kitchengenome.soulbound.co.kr
- 초안 기간 동안 전체 noindex (`X-Robots-Tag: noindex, nofollow`) / site-wide noindex while rankings are draft.
- `main`에 push하면 GitHub Actions(`.github/workflows/deploy.yml`)가 `web/`을 배포. 저장소 시크릿 `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` 필요.
- Push to `main` deploys `web/` via GitHub Actions; requires repo secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.

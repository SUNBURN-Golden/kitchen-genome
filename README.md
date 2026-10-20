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
- Cloudflare Pages 프로젝트 `kitchengenome` (account `aa6bf084982ee841c3ea39b45895a15e`) → https://kitchengenome.soulbound.co.kr
- 자동 배포 없음. GitHub Actions 워크플로는 제거했어요. / No auto-deploy; the Actions workflow was removed.
- 흐름 / Workflow: 변경 → PR → 머지 → 커넥터로 수동 배포 / change → PR → merge → deploy manually via the Cloudflare connector.
  1. `main` 체크아웃 후 `python3 scripts/build_deploy.py` (needs `blake3`) → `dist/deploy_kitchengenome.js` (gitignored).
  2. user-Cloudflare `execute`로 `GET /accounts/{id}/pages/projects/kitchengenome/upload-token`의 `result.jwt`만 반환하게 해서 파일로 저장(write_to_file) → `python3 scripts/upload_assets.py <그 파일>` (커넥터 안에서는 asset 업로드가 403이라 박스에서 올림; 토큰 출력 금지).
  2b. `execute`에 `dist/deploy_kitchengenome.js` 내용을 `code`로 넣어 배포 생성.
  3. 확인: https://kitchengenome.soulbound.co.kr 200, 제목 '키친 게놈', noindex.
- 토큰이 있으면 `deploy.sh`(wrangler)로도 가능 / `deploy.sh` (wrangler) works if a Pages:Edit token is available.
- 초안 기간 동안 noindex 유지: `<meta name="robots" content="noindex">` + `X-Robots-Tag: noindex, nofollow` 헤더. 지우지 마세요. / Keep noindex.
- 판정(과대평가/저평가)은 `web/app.js`의 `SHOW_VERDICTS=false`로 숨겨져 있어요. 공개는 준태님 승인 후. / Verdicts hidden behind `SHOW_VERDICTS`.

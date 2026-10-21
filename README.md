# 키친 게놈 (Kitchen Genome)

서울 셰프 계보 트래커. 유명 주방 출신 셰프들이 연 독립 식당을 계보도로 잇고, 공개 데이터 기반 '저평가 / 과대평가 / 균형' 판정과 '다음에 뜰 집'을 함께 보여줍니다.

A Seoul chef-lineage tracker: maps alumni of top kitchens to the restaurants they opened, with public-data verdicts (undervalued / overrated / balanced) and a "next to rise" list.

## 구조 / Layout
- `web/` — 정적 사이트 (index.html, app.js, style.css, data.js) + 빌드 생성물 `r/<id>/`, `k/<id>/`, `directory/`, 영어판 `en/…`, sitemap.xml, rss.xml, en/rss.xml (`scripts/build_pages.py`, `build_pages_en.py`, `build_feeds.py`; 영문명은 `data/enrich_en.py`, `name_en_official`=false면 로마자 표기; `build_deploy.py`가 자동 실행) / static site root + generated SEO pages
- `data/` — 시드 데이터와 빌드 스크립트, `outreach/` 식당 확인 절차 / seed data, build scripts, restaurant verification outreach
- `deploy.sh` — 수동 배포 / manual deploy

## 데이터 재빌드 / Data rebuild
```
cd data
python3 build_data.py && python3 enrich_1.py && python3 enrich_2.py && python3 enrich_3.py && python3 enrich_en.py
```
순서대로 실행 / run in this order.

## 배포 / Deploy
- Cloudflare Pages 프로젝트 `kitchengenome` (account `aa6bf084982ee841c3ea39b45895a15e`) → https://kitchengenome.soulbound.co.kr
- 자동 배포 없음. GitHub Actions 워크플로는 제거했어요. / No auto-deploy; the Actions workflow was removed.
- 흐름 / Workflow: 변경 → PR → 머지 → 커넥터로 수동 배포 / change → PR → merge → deploy manually via the Cloudflare connector.
  1. `main` 체크아웃 후 `python3 scripts/build_deploy.py` (needs `blake3`) → `dist/deploy_kitchengenome.js` (gitignored).
  2. user-Cloudflare `execute`로 `GET /accounts/{id}/pages/projects/kitchengenome/upload-token`의 `result.jwt`만 반환하게 해서 파일로 저장(write_to_file) → `python3 scripts/upload_assets.py <그 파일>` (커넥터 안에서는 asset 업로드가 403이라 박스에서 올림; 토큰 출력 금지).
  2b. `execute`에 `dist/deploy_kitchengenome.js` 내용을 `code`로 넣어 배포 생성.
  3. 확인: https://kitchengenome.soulbound.co.kr 200, 제목 '키친 게놈', noindex 없음.
- 토큰이 있으면 `deploy.sh`(wrangler)로도 가능 / `deploy.sh` (wrangler) works if a Pages:Edit token is available.
- 검색 노출 허용 (2026-10-21 준태님 승인): noindex 메타·X-Robots-Tag 헤더 제거, robots.txt `Allow: /` + Sitemap. / Site is indexable; robots.txt allows crawling.
- 판정(과대평가/저평가)은 `web/app.js`의 `SHOW_VERDICTS=false`로 숨겨져 있어요. 공개는 준태님 승인 후. / Verdicts hidden behind `SHOW_VERDICTS`.

## 자동 크롤러 / Daily crawler pipeline
- `crawler/crawl.py` — 공개 출처만, robots.txt 준수, 요청 간 2초. Bing 뉴스 RSS(식당별 검색), 언론사 RSS(`config.json`), 식당 공식 사이트(상태만). **인스타그램·네이버·guide.michelin.com은 수집하지 않음** (미쉐린 robots.txt 법적 고지가 자동 수집 금지 → 미쉐린은 사람이 확인해 `crawler/manual_findings.json`에 기록).
- `crawler/apply.py` — 폐업·상호 변경·허용 필드(note, area, chef, lineage_src, lineage_status, opened)는 서로 다른 신뢰 출처 2곳 이상일 때만 `data/seed.json`에 반영. 나머지(출처 1곳, 셰프 이동, 오픈, 사이트 다운)는 `crawler/pending.json` 검토 대기열로. 아웃리치 확인 상태 필드(verification_status 등)는 절대 수정 안 함. 로그: `crawler/applied.json`.
- 사람이 확인한 근거는 `crawler/manual_findings.json`에 같은 형식으로 추가 (id, kind, url, host, date, title, new_name | field+value).
- 참고: `data/build_data.py`는 예전 시드 생성기라 지금 실행하면 seed.json을 덮어씀. 일일 파이프라인은 seed.json을 직접 고치고 `enrich_en.py`(→ web/data.js)만 다시 돌림.

### 매일 실행 / Daily run
```
bash scripts/daily_pipeline.sh /tmp/kg
```
크롤 → 반영 → 빌드 → 가드(SHOW_VERDICTS/SHOW_NEWSLETTER=false, noindex 없음) → PR → squash merge → `dist/deploy_kitchengenome.js` 생성. 마지막 줄:
- `RESULT=NO_CHANGE` — 끝.
- `RESULT=MERGED_NO_DEPLOY pr=…` — 대기열만 바뀜, 배포 불필요.
- `RESULT=NEEDS_DEPLOY pr=…` — **커넥터 단계 필요** (박스 스크립트로는 못 함, Pages 토큰 없음):
  1. user-Cloudflare `execute`로 `GET /accounts/aa6bf084982ee841c3ea39b45895a15e/pages/projects/kitchengenome/upload-token` → JWT를 파일(예: /tmp/kg_jwt.txt)에 저장 (출력 금지)
  2. `python3 scripts/upload_assets.py /tmp/kg_jwt.txt`
  3. user-Cloudflare `execute`에 `dist/deploy_kitchengenome.js` 내용을 code로 넣어 배포 생성 → 결과의 deployment id 기록, 사이트맵 URL 200 확인.

# 키친 게놈 (Kitchen Genome)

서울 셰프 계보 트래커. 유명 주방 출신 셰프들이 연 독립 식당을 계보도로 잇고, 공개 데이터 기반 '저평가 / 과대평가 / 균형' 판정과 '다음에 뜰 집'을 함께 보여줍니다.

A Seoul chef-lineage tracker: maps alumni of top kitchens to the restaurants they opened, with public-data verdicts (undervalued / overrated / balanced) and a "next to rise" list.

## 구조 / Layout
- Typography: self-hosted Black Han Sans 400 (headings/numbers) and Gothic A1 400/600 (body), SIL OFL 1.1 licences in `web/fonts/`; the colour, type and radius tokens are in the opening `:root` block of `web/style.css` (see [design decisions](DECISIONS.md)).
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

### 인스타그램 단계 / Instagram step (`crawler/instagram.py`, 준태님 결정 2026-10-21: 아가마중 계정으로 로그인)
- **06:45–08:00 KST에만 실행.** 그 외 시간엔 `IG_SKIPPED`.
- 로그인 ID는 스크립트에 고정, 비밀번호는 환경변수 `AGAMAJUNG_IG_PASSWORD`에서 실행 중에만 읽음 (출력·로그·커밋 금지). 세션은 레포 밖 `/home/box/.secrets/kg_ig_agamajung.session`(600)에 저장해 재사용. 세션 만료 시 같은 날 재로그인 안 함.
- 체크포인트·챌린지·2FA·이메일 코드·의심 로그인·429·로그인 실패: 즉시 중단, 재시도·우회 없음, `crawler/out/ig_blocked.flag`에 사유 기록, `IG_BLOCKED reason=…` 출력. 플래그가 있으면 사람이 지울 때까지 IG 단계 안 돌림.
- 하루 25개 핸들 이하, 요청 간 8초 이상, 프로필 + 최근 게시물 6개 캡션·날짜만. 발췌 40자 이하, 게시물 URL, 날짜만 저장. IG 결과는 검토 대기열 전용 (신뢰 출처 1곳 + IG = 2곳으로 인정).
- 의존성: `pip install --break-system-packages instaloader`. 테스트(목, 네트워크 없음): `python3 -m unittest tests/test_instagram.py`.
- 파이프라인 출력 한 줄: `IG_OK handles=N findings=M` | `IG_SKIPPED reason=…` | `IG_BLOCKED reason=…` (→ 준태님 보고 + BabyShower 알림).

## Login (Cloudflare Pages Functions + D1) — PR #16
- Social login only (Google now; Kakao stubbed/disabled). Code: `functions/` (Pages Functions), `web/auth.js`, `/login/`, `/en/login/`.
- Routes: `GET /auth/google/start?news=1|0&next=` → Google (scope `openid email`, state + PKCE in a signed 10-min `kg_oauth` cookie) → `GET /auth/google/callback` → upsert `users` in D1 → signed HttpOnly `kg_session` cookie (30 days). `POST|GET /auth/logout`. `GET /api/me` → `{config:{google,kakao}, user}`.
- Newsletter checkbox "계보 업데이트 받아보기" is pre-checked; its value travels via `news=` in start and is saved at callback (`newsletter_opt_in`, `consent_at`).
- D1: `kitchengenome` (`bb88b9ae-9380-4d86-ae8d-226f97c19274`), bound as `DB` on Pages project kitchengenome (production + preview). Schema: `migrations/0001_users.sql` (already applied).
- Env (Pages → Settings → Variables, as **secrets**): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SESSION_SECRET` (random ≥32 bytes). Until all three exist, `/api/me` reports `google:false` and buttons show "준비 중".
- Google OAuth client redirect URI: `https://kitchengenome.soulbound.co.kr/auth/google/callback` (add `https://kitchengenome.pages.dev/auth/google/callback` for preview if wanted).

### Deploying with Functions
The asset-only deploy (`upload_assets.py` + `deploy_kitchengenome.js`) does **not** ship Functions; an asset-only deployment simply has no `/api/me`, so login shows "준비 중" (safe). To ship Functions:
- With a Pages:Edit token: `CLOUDFLARE_API_TOKEN=... npx wrangler pages deploy web --project-name kitchengenome --branch main` from the repo root (wrangler picks up `functions/` and `wrangler.toml`). Use `--branch preview` first to test.
- Via the connector (no token): `npx wrangler pages functions build --outfile dist/_worker.bundle --output-routes-path dist/_routes.json` on the box, upload assets as usual, then add multipart fields `_worker.bundle` (file, the bundle) and `_routes.json` (file) to the deployments POST in `deploy_kitchengenome.js`. Test on a non-main branch first.

## Local validation

Build generated pages and feeds without invoking deployment tooling:

```sh
python3 scripts/build_pages.py
python3 scripts/build_pages_en.py
python3 scripts/build_feeds.py
python3 -m pytest -q
python3 scripts/check_ui.py
node --check web/app.js
git diff --check
```

The browser check requires Python Playwright and Chromium. It intercepts every
request locally, exercises the kitchen select, dialog focus and directory
filter, and checks wrapping, overflow, targets and dark mode with reduced motion enabled.
It checks root-to-chef connector continuity for seven/eight groups and clicks
the upper/lower edges of Korean/English source-list links to catch overlapping targets.
Use `--screenshots` to save 1440px/390px light/dark images of the home tree,
Mingles and Korean/English source lists in ignored `shots/`. Add
`--screenshot-dir PATH` to save that evidence elsewhere.
The existing crawler tests can rewrite `crawler/vault.json` and
`crawler/inbox_summary.json`; inspect those changes and discard test-only
metadata before committing. If both files were clean before the test run, use
`git restore -- crawler/vault.json crawler/inbox_summary.json` to discard those
known test outputs after inspection.

Font slices total about 474 KB. They include KS X 1001 syllables plus supported
Korean characters from the current page corpus. To regenerate offline, install
fontTools with Brotli, put the three upstream Google Fonts TTF files named in
`scripts/subset_fonts.py` in a source directory, then run
`python3 scripts/subset_fonts.py SOURCE_DIRECTORY`. Commit the WOFF2 outputs;
normal page builds do not need the TTF sources or a network connection.

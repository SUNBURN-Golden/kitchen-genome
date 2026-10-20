# 계보 오너 확인 절차

상태값(seed.json `verification_status`): unverified → contacted → confirmed | correction_requested | declined | no_reply
필드: contacted_at, confirmed_at (YYYY-MM-DD), verification_note

1. **배치 선정** — contacts.csv에서 priority 1(서울·계보 verified)부터 5~10곳. `confidence`가 search_summary/seed인 계정은 프로필을 직접 열어 업장 공식 계정인지 확인. 이메일은 공식 사이트 기재분만 사용(추측 금지).
2. **발송 (준태님 승인 후)** — 채널 우선순위: 공식 이메일 > 업장 IG DM > 셰프 개인 IG. templates.md 1·2번에 값 채워 발송. 같은 셰프 여러 업장(밀스/마오, 오르조/이탈리안클럽)은 한 메시지로.
3. **기록** — `data/outreach/log.csv`(id,date,channel,to,template,result)에 한 줄 추가, seed `verification_status=contacted`, `contacted_at` 기입. (enrich_3.py의 STATUS 딕셔너리에 적으면 재빌드 시 반영)
4. **14일 후 팔로업** — contacted이고 contacted_at+14일 지난 곳에 템플릿 3번 1회만. 그 뒤 14일 더 무응답이면 `no_reply`, 더 연락하지 않음.
5. **회신 반영** — 수정 요청: `correction_requested`, 내용을 verification_note에 기록 → seed/build 데이터 수정 → 재빌드(build_data.py → enrich_1 → enrich_2 → enrich_3) → `confirmed`. 게재 거부: `declined`, 해당 페이지 비공개 처리.
6. **확인 완료** — "확인" 회신 시 `confirmed`, `confirmed_at` 기입, 회신 원문(스크린샷/메일) 보관.
7. **사이트 배지 (추후)** — confirmed 노드에 '셰프 확인' 배지, 나머지는 '공개 자료 기준'. 프론트 작업은 별도 요청.

원칙: 한 곳당 최대 2회 연락, 게재 거부는 즉시 존중, 판정(과대/저평가)은 확인 요청 대상이 아님 — 계보 사실만 묻는다.

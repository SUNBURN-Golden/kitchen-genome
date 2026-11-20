"""Candidate-target review. New names the extractor surfaces become `candidate` targets; before anyone looks at them we drop the ones that
are clearly not a chef or a restaurant, and queue the rest for a human with their evidence (quotes, hosts).

Filters (rules, deterministic):
- acronym: all-caps Latin tokens of 2-6 letters (optionally dotted), e.g. 'CIA' (= Culinary Institute of America in '美 CIA 출신'), 'ICIF', 'KFC'.
  A real restaurant named in capitals is still reachable: seed/known aliases resolve BEFORE this filter, and a human can re-activate it.
- institution words: schools, hotels-as-employers, groups, broadcasters, associations ('학교', '대학', '아카데미', '호텔', '그룹', '협회', 'Cordon Bleu', ...).
- chef names that are role nouns or common words are already refused by extract.clean_name.
"""
import json
import re

from kg_daemon.db import stable_id, utcnow
from .targets import norm

ACRONYM = re.compile(r'^(?:[A-Z]\.?){2,6}$')
INSTITUTION = re.compile(r'학교|대학|아카데미|academy|institute|school|university|college|cordon\s*bleu|꼬르동|츠지|호텔|hotel|그룹|group|협회|association|방송|넷플릭스|netflix|'
                         r'tvn|jtbc|sbs|kbs|mbc|백화점|department|재단|foundation|위원회|공사|주식회사|corp|inc\.?$|ltd', re.I)
NAME_STOP = {norm(n) for n in (
    '최근', '이번', '한국', '선보인', '진행한', '셰프', '쉐프', '오너', '총괄', '헤드', '수석', '수셰프',
    '헤드 셰프', '오너 셰프', '총괄 셰프', '수석 셰프', '주방장', '파티시에', '소믈리에',
    '직원', '인턴', '스태프', '식당', '레스토랑', 'chef', 'head chef', 'owner', 'restaurant',
)}


def reject_reason(kind: str, name: str | None) -> str | None:
    if not name:
        return 'empty'
    n = name.strip()
    if norm(n) in NAME_STOP:
        return 'generic_name'
    if ACRONYM.match(n):
        return 'acronym_not_a_venue'
    if INSTITUTION.search(n):
        return 'institution_not_a_venue' if kind == 'restaurant' else 'institution_not_a_person'
    if kind == 'chef' and re.search(r'[A-Za-z0-9]', n):
        return 'chef_name_not_hangul_person_name'
    return None


def review_candidates(conn, now: str | None = None) -> dict:
    """Reject filtered candidates (status='rejected', reason kept in attrs) and queue the rest for a human, with evidence."""
    now = now or utcnow()
    out = {'rejected': [], 'queued_for_review': []}
    for t in conn.execute("SELECT * FROM target WHERE status='candidate'").fetchall():
        name = t['name_ko'] or t['name_en']
        why = reject_reason(t['kind'], name)
        attrs = json.loads(t['attrs_json'] or '{}')
        if why:
            attrs['rejected_reason'] = why
            attrs['rejected_at'] = now
            conn.execute("UPDATE target SET status='rejected', attrs_json=? WHERE target_id=?", (json.dumps(attrs, ensure_ascii=False), t['target_id']))
            out['rejected'].append({'name': name, 'kind': t['kind'], 'reason': why})
            continue
        ev = conn.execute("""SELECT host, label, epistemic, quote, canonical_url FROM extraction WHERE resolved_chef_target=? OR resolved_restaurant_target=? OR resolved_org_target=?
                             ORDER BY epistemic='fact' DESC, published_at DESC LIMIT 5""", (t['target_id'],) * 3).fetchall()
        payload = {'name': name, 'kind': t['kind'], 'hosts': sorted({e['host'] for e in ev}), 'evidence': [{'quote': e['quote'], 'url': e['canonical_url'], 'label': e['label'],
                   'epistemic': e['epistemic']} for e in ev], 'promote_rule': '>=2 independent hosts with claim/fact, or 1 official fact'}
        rid = stable_id('rev', 'candidate', t['target_id'])
        conn.execute("""INSERT OR IGNORE INTO review_queue(review_id,reason,subject_type,subject_id,payload_json,dedupe_key,created_at) VALUES (?,?,?,?,?,?,?)""",
                     (rid, 'candidate_target', 'target', t['target_id'], json.dumps(payload, ensure_ascii=False), f"cand:{t['target_id']}", now))
        out['queued_for_review'].append(payload)
    conn.commit()
    return out

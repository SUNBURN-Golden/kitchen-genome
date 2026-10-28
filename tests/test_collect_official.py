"""Official-site / booking-page profile extractor (official.py, booking.py) and gap filling from profile facts. Offline."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_collect import booking, gaps, official   # noqa: E402
from kg_daemon import db as kgdb   # noqa: E402

T = {'target_id': 'tgt_x', 'name_ko': '무노', 'name_en': 'Moono'}


def fields(facts):
    return {(f['field'], f['value']) for f in facts}


def ex(text, jsonld=None, basis='seed', kind='official_site', title=''):
    return official.extract_profile(text, jsonld, target=T, host='moononyc.com', source_kind=kind, site_basis=basis, content_hash='h', title=title)


def test_jsonld_restaurant_block_gives_address_opened_chef_and_operating():
    ld = [{'@type': 'Restaurant', 'name': 'Moono', 'address': {'streetAddress': '9 W 26th St', 'addressLocality': 'New York', 'addressRegion': 'NY'},
           'foundingDate': '2024-06-01', 'founder': {'@type': 'Person', 'name': 'Mingoo Kang'}, 'openingHours': 'Tu-Sa 17:00-22:00'}]
    fs = ex('Welcome', ld)
    assert {('address', '9 W 26th St, New York, NY'), ('opened', '2024-06'), ('chef', 'Mingoo Kang'), ('status', 'operating')} <= fields(fs)
    assert all(f['epistemic'] == 'fact' for f in fs)        # seed-tied site announcing its own profile


def test_jsonld_block_for_a_different_venue_on_a_group_site_is_ignored():
    ld = [{'@type': 'Restaurant', 'name': 'Other Place', 'foundingDate': '2010'}]
    assert not ex('Welcome', ld)


def test_korean_text_rules_open_chef_address_and_guards():
    txt = ('무노는 2024년 6월 문을 열었습니다. 오너 셰프 김민수가 이끈다. 주소: 서울특별시 강남구 도산대로 123, 2층\n'
           '2025년 하반기 2호점 오픈 예정입니다.\n10월 3일은 명절 휴무로 영업을 종료합니다.')
    f = fields(ex(txt))
    assert ('opened', '2024-06') in f
    assert ('chef', '김민수') in f
    assert any(k == 'address' and v.startswith('서울특별시 강남구 도산대로 123') for k, v in f)
    assert not any(k == 'opened' and v.startswith('2025') for k, v in f)       # planned, not done
    assert ('status', 'closed') not in f                                        # holiday closure is not a closure


def test_english_since_michelin_is_not_an_opening_but_opened_in_is():
    assert not any(k == 'opened' for k, _ in fields(ex('Since 2019, one Michelin star.')))
    assert ('opened', '2018-09') in fields(ex('Moono opened its doors in September 2018 in Flatiron.'))


def test_closure_and_relocation_and_negation():
    assert ('status', 'closed') in fields(ex('After ten years, we have closed. Thank you.'))
    assert not any(k == 'status' and v == 'closed' for k, v in fields(ex('폐업 소문이 돌았지만 폐업하지 않았습니다. 사실이 아닙니다.')))
    assert any(k == 'relocation' for k, _ in fields(ex('무노는 청담동으로 이전했습니다.')))


def test_probe_verified_site_and_booking_pages_are_claims_not_facts():
    assert all(f['epistemic'] == 'claim' for f in ex('Moono opened in 2018. Hours 5pm', basis='probe_verified'))
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({'props': {'shop': {'shopName': '무노', 'roadAddress': '서울 강남구 도산대로 1', 'openDate': '2021.03.02'}}}) + '</script>'
    fs = booking.extract_booking(html, '', [], target=T, host='app.catchtable.co.kr', content_hash='b')
    assert ('address', '서울 강남구 도산대로 1') in fields(fs) and ('opened', '2021-03') in fields(fs)
    assert all(f['epistemic'] == 'claim' for f in fs)
    other = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({'shop': {'shopName': '딴집', 'openDate': '2001'}}) + '</script>'
    assert not booking.extract_booking(other, '', [], target=T, host='app.catchtable.co.kr', content_hash='c')


def test_profile_facts_fill_gaps_with_evidence_and_official_outranks_press():
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    now = '2026-10-28T00:00:00Z'
    c.execute("INSERT INTO target(target_id,kind,name_ko,name_en,origin,discovered_via,discovered_at,status,attrs_json) VALUES ('tgt_x','restaurant','무노','Moono','seed','seed',?,'active','{}')", (now,))
    c.execute("INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetcher_version,extractor_version,fetched_at) VALUES ('f1','https://moononyc.com','https://moononyc.com','moononyc.com','html','ok','t','t',?)", (now,))
    for fld in ('opened', 'status_recent'):
        c.execute('INSERT INTO gap(gap_id,target_id,field,opened_at) VALUES (?,?,?,?)', ('g_' + fld, 'tgt_x', fld, now))
    official.store(c, ex('Moono opened its doors in 2018. Reservations: Tue-Sat dinner.', title='Moono'), fetch_id='f1', canonical_url='https://moononyc.com', host='moononyc.com', now=now)
    r = gaps.resolve_gaps(c, now)
    assert r['resolved'] == 2
    got = {g['field']: (g['resolved_value'], g['resolved_by_fact']) for g in c.execute('SELECT * FROM gap')}
    assert got['opened'][0] == '2018' and got['opened'][1].startswith('pf_')
    assert got['status_recent'][0] == 'operating'
    assert gaps.resolve_gaps(c, now) == {'resolved': 0, 'conflict': 0}          # idempotent

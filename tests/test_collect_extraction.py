"""Extraction / resolution / gold unit tests (offline, rules only)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from collect_world import SEED   # noqa: E402
from kg_collect import gold   # noqa: E402
from kg_collect.extract import extract_text   # noqa: E402
from kg_collect.resolve import resolve_extractions   # noqa: E402
from kg_collect.targets import Gazetteer, load_seed_targets, norm, upsert_target   # noqa: E402
from kg_daemon import db as kgdb   # noqa: E402


@pytest.fixture()
def conn():
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    load_seed_targets(c, SEED)
    return c


def ext(conn, text, host='press1.test', tier='press', ctx=None, official=()):
    return extract_text(text, gaz=Gazetteer(conn), host=host, published_at='2026-10-20', tier=tier, ctx=ctx, ocr_conf=None, official_hosts=set(official), content_hash='h')


# ── extraction rules ──
def labels(rs):
    return [(r['label'], r['epistemic'], r['ambiguity']) for r in rs]


def test_guest_event_is_not_employment(conn):
    rs = ext(conn, '서하람 셰프는 달빛정원에서 게스트 셰프로 하루 협업 디너를 연다.')
    assert 'employment_claim' not in {r['label'] for r in rs} and 'collab' in {r['label'] for r in rs}


def test_coworking_is_not_mentorship(conn):
    rs = ext(conn, '한도윤 셰프와 서하람 셰프는 같은 주방에서 함께 일했다.')
    assert 'mentorship_claim' not in {r['label'] for r in rs}


def test_hearsay_and_negation_are_unverified(conn):
    for t in ('모래시계가 곧 문을 닫는다는 얘기가 있다.', '달빛정원은 폐업하지 않았다.'):
        assert all(r['epistemic'] == 'unverified' for r in ext(conn, t) if r['label'] == 'closure'), t


def test_official_host_status_is_fact_press_is_claim(conn):
    t = '달빛정원은 2021년 5월 문을 열었다.'
    assert [r['epistemic'] for r in ext(conn, t, host='dalbit.test', official={'dalbit.test'}) if r['label'] == 'opening'] == ['fact']
    assert [r['epistemic'] for r in ext(conn, t) if r['label'] == 'opening'] == ['claim']


def test_generic_closure_statistics_do_not_attach_to_job_target(conn):
    ctx = {'restaurant': {'name_ko': '달빛정원', 'target_id': 'x'}}
    assert ext(conn, '지난해 폐업 신고를 한 사업자 수가 100만명에 육박했다.', ctx=ctx) == []
    assert [r['restaurant_text'] for r in ext(conn, '이곳은 지난달 문을 닫았다.', ctx=ctx)] == ['달빛정원']        # only an explicit demonstrative subject may use job context


def test_ocr_low_confidence_is_unverified(conn):
    rs = extract_text('달빛정원은 2021년 5월 문을 열었다.', gaz=Gazetteer(conn), host='dalbit.test', published_at=None, tier='official', ctx=None, ocr_conf=40.0, official_hosts={'dalbit.test'}, content_hash='z')
    assert rs and all(r['epistemic'] == 'unverified' for r in rs)


# ── entity resolution: exact only; homonym needs context ──
def test_homonym_resolved_only_by_restaurant_context_else_unresolved(conn):
    ra = conn.execute("SELECT target_id FROM target WHERE name_ko='달빛정원'").fetchone()[0]
    ct = conn.execute("SELECT target_id FROM target WHERE name_ko='한도윤'").fetchone()[0]
    other = upsert_target(conn, 'chef', '한도윤B', origin='seed', attrs={'restaurants': ['r_mor']}, aliases=['한도윤'])        # alias collision = homonym
    gaz = Gazetteer(conn)
    assert len(gaz.lookup('한도윤')) == 2
    rec = {'label': 'employment_claim', 'epistemic': 'claim', 'ambiguity': None, 'chef_text': '한도윤', 'restaurant_text': '달빛정원', 'org_text': None}
    resolve_extractions(conn, [rec], gaz, 'u', 'u')
    assert rec['resolved_chef_target'] == ct and rec['resolve_note'] == 'homonym_resolved_by_restaurant_context'
    rec2 = {'label': 'employment_claim', 'epistemic': 'claim', 'ambiguity': None, 'chef_text': '한도윤', 'restaurant_text': None, 'org_text': None}
    resolve_extractions(conn, [rec2], gaz, 'u', 'u')
    assert rec2['resolved_chef_target'] is None and rec2['resolve_note'] == 'homonym_unresolved'
    assert other and ra


def test_no_fuzzy_merge_of_similar_names(conn):
    assert Gazetteer(conn).lookup('한도연') == [] and norm('Han  Doyun') == norm('han-doyun')


# ── gold ──
def test_synthetic_gold_meets_thresholds_and_reports_false_merges():
    res = gold.run_file(os.path.join(ROOT, 'tests', 'fixtures', 'gold_synthetic.json'))
    assert res['precision'] >= 0.95 and res['recall'] >= 0.85 and res['epistemic_accuracy'] >= 0.95
    assert res['entity_resolution']['false_merge_rate'] == 0.0 and res['entity_resolution']['cases'] >= 5


# ── regressions from the first real-press run (verbatim sentences; entities are real public figures/venues) ──
REAL_TARGETS = {'targets': [{'kind': 'restaurant', 'name_ko': n} for n in ('모수', '밍글스', '이타닉 가든', '라망 시크레')]
                + [{'kind': 'chef', 'name_ko': n, 'restaurants': r} for n, r in (('안성재', ['모수']), ('강민구', ['밍글스']), ('손종원', ['이타닉 가든']), ('이연복', []))], 'sentences': [], 'resolve': []}


@pytest.fixture()
def real_gaz():
    return Gazetteer(gold.build_gold_db(REAL_TARGETS))


def _labels(gaz, text, tier='press'):
    return {(r['label'], r['chef_text'], r['restaurant_text'] or r['org_text'], r['epistemic']) for r in extract_text(text, gaz=gaz, host='x.test', tier=tier)}


def test_real_negated_closure_is_relocation(real_gaz):
    got = _labels(real_gaz, '[현장] 국내 유일 미쉐린 3스타 \'모수\'…"폐업 아닌 이전, 한식 대표할 것"', 'search_lead')
    assert ('relocation', None, '모수', 'unverified') in got and not any(g[0] == 'closure' for g in got)


def test_real_tv_closure_risk_and_ethnicity_are_not_facts(real_gaz):
    assert not _labels(real_gaz, '이연복도 초조…‘스트릿 레스토랑’ 폐업 위기 최하위 팀은?', 'search_lead')
    assert not any(g[0] == 'career' for g in _labels(real_gaz, '"안성재 화교 출신, 중식 대가에 후한 점수" …\'흑백요리사\' 제작진 "악성루머에 강력한 법적 조치"', 'search_lead'))


def test_real_new_quoted_venue_and_appointment(real_gaz):
    got = _labels(real_gaz, '이곳에 자리한 최고급 호텔 파크하얏트 파리-방돔에 지난달 17일 한식당 ‘세옥(SEOK)’이 문을 열었다.')
    assert ('opening', None, '세옥', 'claim') in got
    got = _labels(real_gaz, '프렌치 럭셔리 호텔 소피텔 앰배서더 서울은 신임 총괄 셰프로 ‘기욤 콤파라(Guillaume Comparat)’를 선임했다고 밝혔다.')
    assert ('employment_claim', '기욤 콤파라', '소피텔 앰배서더 서울', 'claim') in got


def test_real_leads_and_collab(real_gaz):
    got = _labels(real_gaz, '조선호텔앤리조트의 손종원 셰프가 이끄는 한식당 \'이타닉 가든\'과 양식당 \'라망 시크레\'가 미쉐린 1스타를 또 다시 획득했다.')
    assert ('employment_claim', '손종원', '이타닉 가든', 'claim') in got
    got = _labels(real_gaz, '서울에서 미쉐린 3스타를 받은 밍글스의 셰프 강민구가 파리에서 처음 선보이는 레스토랑 ‘SEOK’이 파크 하얏트 파리-방돔에 문을 열었다.')
    assert ('employment_claim', '강민구', '밍글스', 'claim') in got and ('opening', '강민구', 'SEOK', 'claim') in got

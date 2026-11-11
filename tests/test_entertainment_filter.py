"""Deterministic TV/drama/entertainment pre-filter (offline, injected transports).

A: heuristic unit tests. B: review-stage integration (zero paid calls on entertainment, chef
articles still reach the model). C: collect-stage integration (no extraction rows, audited skip,
recheck completes, no false extractor alerts, gaps stay clean). D: full-suite regression is run
separately; test_ai_review.py / test_extractor_alerts.py must pass unchanged.
"""
import json
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon'))
sys.path.insert(0, str(ROOT / 'tests'))

from collect_world import SEED   # noqa: E402
from kg_ai import review as rv   # noqa: E402
from kg_ai.budget import Budget   # noqa: E402
from kg_ai.deepseek import Client   # noqa: E402
from kg_collect import cycle, recheck   # noqa: E402
from kg_collect.deepseek import Adapters   # noqa: E402
from kg_collect.entertainment import core_cues, entertainment_detail, entertainment_reason, url_cue   # noqa: E402
from kg_collect.pipeline import HOST_YIELD_PREFIX, host_yield_check, process_doc   # noqa: E402
from kg_collect.rawstore import RawStore   # noqa: E402
from kg_collect.targets import Gazetteer, upsert_target   # noqa: E402
from kg_daemon import db   # noqa: E402
from kg_daemon.claims import queue_review   # noqa: E402
from test_deepseek_client import meter, response   # noqa: E402
from test_deepseek_collect import world, doc, adapter, finding   # noqa: E402
from test_ai_review import TEXT, client_for   # noqa: E402
from test_monthly_recheck import NOW, setup_due   # noqa: E402
from tests_balance_fake import healthy_balance   # noqa: E402

BNT_COMP = 'https://bntnews.co.kr/article/view/bnt202607060060'
BNT_TV = 'https://bntnews.co.kr/TV/article/view/bnt202610160124'
IMBC = 'https://enews.imbc.com/News/RetrieveNewsInfo/521461'

# Shortened competition article: 예능/생존팀/탈락 spread through the body; mentions a gazetteer chef
# (서하람) exactly like the production article whose findings reached paid review.
COMP = ('스트릿 레스토랑 파이터는 tvN 예능 프로그램이다. 이번 회에서는 생존팀이 공개됐다. '
        "정호영의 '호형호제'가 부타동 세트로 4번째 생존팀에 이름을 올렸다. 서하람 셰프도 심사위원으로 출연했다. "
        '심사위원들은 매운맛을 평가했다. 다음 라운드에서는 탈락 팀이 결정된다. 시청자들은 긴장감을 즐겼다.')
COMP_QUOTE = "정호영의 '호형호제'가 부타동 세트로 4번째 생존팀에 이름을 올렸다"

BRIEF = ('JTBC 새 토일드라마 파이널 테이블이 오는 24일 첫 방송된다. 안효섭이 레스토랑 셰프 강한 역을 맡는다. '
         '극 중 파밀리아는 폐업 위기에 놓인다. 요리 서바이벌 대회에서 우승해 가게를 살리는 이야기다.')

# Competition-show chef interview: real restaurant facts + ONE clustered show mention (span ≪ 20%).
CHEF_CLUSTERED_RAW = (
    '서울 성동구 성수동의 레스토랑 온기는 2026년 3월 문을 열었다. 김도현 셰프가 총괄 셰프를 맡는다. '
    '김 셰프는 tvN 서바이벌 예능 흑백요리사에서 탈락한 뒤 이 가게를 열었다고 밝혔다. '
    '온기는 제철 한식을 코스로 선보이며, 점심은 6만 원, 저녁은 12만 원이다. '
    '김 셰프는 매일 아침 직접 장을 보고 소스를 만든다고 했다. 주방은 오픈형으로 손님들이 요리 과정을 볼 수 있다. '
    '그는 프랑스에서 10년을 일했고 파리 레스토랑에서 수셰프를 지냈다. 귀국 후에는 모기업에서 총괄을 맡았다. '
    '온기의 시그니처는 버섯 리소토와 한우 안심구이이며, 와인 페어링은 소믈리에가 담당한다. '
    '예약은 캐치테이블로 받으며 주말은 한 달 전 마감된다. 김 셰프는 내년 2호점도 계획 중이라고 말했다. '
    '그는 방송 출연이 가게에 도움이 됐지만 결국 음식으로 승부해야 한다고 했다. 앞으로도 제철 식재료를 직접 고르며 '
    '손님들에게 변함없는 맛을 보여주겠다는 각오를 전했다. 가게는 화요일에 쉰다.')
CHEF_CLUSTERED = (CHEF_CLUSTERED_RAW.replace('온기는', '달빛정원은').replace('온기', '달빛정원')
                  .replace('김도현', '한도윤').replace('김 셰프', '한 셰프'))

# Same show vocabulary artificially SPREAD across the body (the residual false-positive surface, §7).
CHEF_SPREAD = (
    '서울 성동구 성수동의 레스토랑 달빛정원은 2026년 3월 문을 열었다. 한도윤 셰프가 총괄 셰프를 맡는다. '
    '한 셰프는 tvN 서바이벌 프로그램에 출연한 적이 있다. '
    '달빛정원은 제철 한식을 코스로 선보이며, 점심은 6만 원, 저녁은 12만 원이다. '
    '한 셰프는 매일 아침 직접 장을 보고 소스를 만든다고 했다. 주방은 오픈형으로 손님들이 요리 과정을 볼 수 있다. '
    '그는 프랑스에서 10년을 일했고 파리 레스토랑에서 수셰프를 지냈다. 귀국 후에는 모기업에서 총괄을 맡았다. '
    '달빛정원의 시그니처는 버섯 리소토와 한우 안심구이이며, 와인 페어링은 소믈리에가 담당한다. '
    '예약은 캐치테이블로 받으며 주말은 한 달 전 마감된다. 한 셰프는 내년 2호점도 계획 중이라고 말했다. '
    '그는 예능 출연이 가게에 도움이 됐지만 결국 음식으로 승부해야 한다고 했다. 앞으로도 제철 식재료를 직접 고르며 '
    '손님들에게 변함없는 맛을 보여주겠다는 각오를 전했다. 마지막 탈락의 순간까지 긴장을 늦추지 않았다고 한다. 가게는 화요일에 쉰다.')

CHEF_ADDR = (
    '부산 해운대구 달맞이길 62에 자리한 레스토랑 파도는 2026년 5월 오픈했다. 이수진 셰프의 가게다. '
    '이 셰프는 서바이벌 프로그램 출신으로 알려졌다. 파도는 해산물 코스를 선보인다. '
    '그녀는 탈락의 고배를 마신 뒤에도 주방을 지켰고, 예능 출연 이후에도 가게에 집중했다고 말했다. '
    '파도의 대표 메뉴는 전복 리소토와 광어 회이며 저녁 코스는 9만 원이다. '
    '예약은 전화 051-746-1234로 받는다. 영업시간은 12시부터 22시까지이며 월요일 휴무다.')

CHEF_TEXT = '서울 한남동의 달빛정원은 2026-10-01 문을 열었다. 한도윤 셰프가 총괄 셰프를 맡는다. 한 셰프는 방송에 출연했다.'


# ───────────── A. heuristic unit tests ─────────────
@pytest.mark.parametrize('url,cue', [
    (BNT_TV, 'tv'),
    ('https://x.test/entertainment/a', 'entertainment'),
    ('https://x.test/entertain/a', 'entertain'),
    ('https://x.test/drama/a', 'drama'),
    ('https://x.test/연예/a', '연예'),
    ('https://x.test/%EC%97%B0%EC%98%88/a', '연예'),
    ('https://x.test/%54V/a', 'tv'),
    ('https://www.hankyung.com/article/2026101546211', None),
    ('https://www.wkorea.com/2026-10-20/강민구의-seok/', None),
    ('https://digitalchosun.dizzo.com/site/data/html_dir/2026/09/07/2026090780197.html', None),
    ('https://x.test/interview/a', None),
    ('https://x.test/a?category=tv', None),
    ('https://tv.example.com/a', None),
    (None, None),
    ('', None),
])
def test_url_cue(url, cue):
    assert url_cue(url) == cue
    assert entertainment_reason(url, '') == ('skipped_entertainment' if cue else None)


@pytest.mark.parametrize('text,skip', [
    (COMP, True),                                   # 예능+생존팀+탈락 distributed
    (BRIEF, True),                                  # short drama brief, 4 families
    (CHEF_CLUSTERED_RAW, False),                    # clustered show mention in a real interview
    ('그는 흑백요리사 출연 이후 드라마 같은 인생을 살고 있다. 달빛정원은 2026-10-01 문을 열었다.', False),
    ('드라마 소개. ' * 5, False),                    # one family repeated never triggers
    ('달빛정원은 2026-10-01 문을 열었다. 한도윤 셰프가 총괄 셰프를 맡는다.', False),
    ('달빛정원은 2026년 10월 1일 문을 닫았다.', False),
    (TEXT, False),                                  # test_ai_review.py TEXT: one 드라마 among real facts
])
def test_text_rule(text, skip):
    assert entertainment_reason('https://press.test/a', text) == ('skipped_entertainment' if skip else None)


def test_dominance_clustered_passes_spread_skips():
    d = entertainment_detail(None, CHEF_CLUSTERED)
    assert len(d['cues']) >= 2 and d['span_ratio'] < 0.2          # clustered: cues do NOT dominate
    assert entertainment_reason(None, CHEF_CLUSTERED) is None
    s = entertainment_detail(None, CHEF_SPREAD)
    assert len(s['cues']) >= 2 and s['span_ratio'] >= 0.2         # same vocabulary, spread: dominates
    assert entertainment_reason(None, CHEF_SPREAD) == 'skipped_entertainment'


@pytest.mark.parametrize('text', [
    CHEF_ADDR,                                       # raw phone + road address
    CHEF_ADDR.replace('예약은 전화 051-746-1234로 받는다.', '예약은 캐치테이블로 받는다.'),   # address only
    CHEF_SPREAD + ' 예약은 전화 [REDACTED_PHONE]로 받는다.',       # sanitized phone marker
    CHEF_SPREAD + ' 가게는 [REDACTED_ADDRESS]에 있다.',            # sanitized address marker
])
def test_real_world_veto(text):
    d = entertainment_detail(None, text)
    assert len(d['cues']) >= 2 and d['span_ratio'] >= 0.2 and d['real_world']
    assert entertainment_reason(None, text) is None               # veto stands the text rule down
    assert entertainment_reason(BNT_TV, text) == 'skipped_entertainment'   # URL rule still applies


@pytest.mark.parametrize('text', [
    '그는 드라마 같은 인생을 살았다. 예능 출연도 했다.',
    '그는 드라마같은 인생을 살았다. 예능 출연도 했다.',
])
def test_drama_metaphor_is_not_a_cue(text):
    assert core_cues(text) == ['예능']
    assert entertainment_reason(None, text) is None


def test_boilerplate_cut():
    body = '달빛정원은 2026-10-01 문을 열었다. 한도윤 셰프가 총괄 셰프를 맡는다. '
    for chrome in ('관련기사 드라마 예능 화제', '많이 본 뉴스 드라마 예능 화제', "bnt's Pick 드라마 예능", 'bnt’s Pick 드라마 예능'):
        assert entertainment_reason(None, body + chrome) is None
    # the test_extractor_alerts.py fixture shape: 예능 sidebar AFTER 많이 본 뉴스, BEFORE 관련기사
    fixture = ('연예 기사 0번 본문입니다. 배우 아무개가 드라마에 출연한다.'
               ' 많이 본 뉴스 뮤지컬 배우 서하람과 가수 최유정이 새 예능 마스터로 합류한다. 서하람 서하람 관련기사 꼬리 0')
    assert core_cues(fixture) == ['드라마']
    assert entertainment_reason(None, fixture) is None


def test_nfd_input_same_verdict():
    for text, skip in ((COMP, True), (CHEF_CLUSTERED, False), (BRIEF, True)):
        nfd = unicodedata.normalize('NFD', text)
        assert entertainment_reason(None, nfd) == ('skipped_entertainment' if skip else None)


# ───────────── B. review-stage integration ─────────────
def review_env(tmp_path, url, text):
    conn = db.connect(str(tmp_path / 'state.db')); db.migrate(conn)
    cycle.init_db(conn, SEED)
    raw = tmp_path / 'raw'; (raw / 'h').mkdir(parents=True)
    (raw / 'h' / 't.txt').write_text(text, encoding='utf-8')
    host = urlsplit(url).netloc
    conn.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_ref,fetched_at,fetcher_version,extractor_version)
                    VALUES ('f',?,?,?,'html','ok','h/t.txt',?,'fixture','rules-new')""", (url, url, host, db.utcnow()))
    # Gazetteer targets for the chef-article entities so they pass the (separate) no-targets gate and
    # reach the model. They are deliberately NOT in the seed below, so publication lands unlinked.
    for kind, name in (('restaurant', '세옥'), ('restaurant', 'SEOK'), ('restaurant', '더 플라잉 호그'),
                       ('restaurant', '밍글스'), ('chef', '강민구'), ('chef', '김정석')):
        upsert_target(conn, kind, name, now=db.utcnow())
    conn.commit()
    seed = {'kitchens': [], 'restaurants': [{'id': 'r1', 'name': '달빛정원', 'chef': '한도윤'}]}
    sp = tmp_path / 'seed.json'; sp.write_text(json.dumps(seed, ensure_ascii=False), encoding='utf-8')
    return conn, sp, tmp_path / 'log.jsonl', str(raw)


def add_f(conn, xid, label, quote, url, text, **kw):
    start = text.find(quote)
    assert start >= 0
    conn.execute("""INSERT INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,chef_text,restaurant_text,org_text,role_text,date_text,
                    span_start,span_end,quote,extractor,extractor_version,published_at,created_at,asserted_by,pending_only)
                    VALUES (?,?,?,?,?,'inference',?,?,?,?,?,?,?,?,'deepseek:web','web-extract-1','2026-10-02',?,'ai_inference',1)""",
                 (xid, 'f', url, urlsplit(url).netloc, label, kw.get('chef'), kw.get('rest'), kw.get('org'), kw.get('role'), kw.get('date'),
                  start, start + len(quote), quote, db.utcnow()))
    queue_review(conn, 'ai_inference', 'extraction', xid, {'asserted_by': 'ai_inference'}, dedupe_key='ai:extraction:' + xid)
    conn.commit()


def assert_skipped(conn, log, s, xid):
    assert s['calls'] == 0 and s['skipped_entertainment'] == 1 and s['prechecked_rejects'] == 1
    assert s['rejected'] == 1 and s['remaining'] == 0 and s['cost_used'] == 0
    q = conn.execute("SELECT state, payload_json FROM review_queue WHERE subject_id=?", (xid,)).fetchone()
    assert q['state'] == 'dismissed'
    ai = json.loads(q['payload_json'])['ai_review']
    assert ai['decision'] == 'reject' and ai['reason'] == 'skipped_entertainment' and 'model_meta' not in ai
    rec = next(json.loads(l) for l in log.read_text(encoding='utf-8').splitlines() if json.loads(l)['extraction_id'] == xid)
    assert rec['action'] == 'reject' and rec['reason'] == 'skipped_entertainment' and rec['model_meta'] is None
    assert conn.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0] == 0


def test_review_bnt_competition_skipped_zero_cost(tmp_path):
    conn, sp, log, raw = review_env(tmp_path, BNT_COMP, COMP)
    add_f(conn, 'x_comp', 'employment_claim', COMP_QUOTE, BNT_COMP, COMP, chef='정호영', rest='호형호제')
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {}, []))
    assert_skipped(conn, log, s, 'x_comp')


def test_review_bnt_tv_url_skipped_without_text_cues(tmp_path):
    text = '파이널 테이블 제작발표회가 열렸다. 배우들이 모였다.'
    conn, sp, log, raw = review_env(tmp_path, BNT_TV, text)
    add_f(conn, 'x_tv', 'opening', '파이널 테이블 제작발표회가 열렸다', BNT_TV, text, rest='파이널 테이블')
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {}, []))
    assert_skipped(conn, log, s, 'x_tv')


def test_review_imbc_drama_skipped_via_text(tmp_path):
    conn, sp, log, raw = review_env(tmp_path, IMBC, BRIEF)
    add_f(conn, 'x_imbc', 'opening', 'JTBC 새 토일드라마 파이널 테이블이 오는 24일 첫 방송된다', IMBC, BRIEF, rest='파이널 테이블')
    assert url_cue(IMBC) is None
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {}, []))
    assert_skipped(conn, log, s, 'x_imbc')


CHEF_CASES = [
    ('https://www.hankyung.com/article/2026101546211',
     '이곳에 자리한 최고급 호텔 파크하얏트 파리-방돔에 지난달 17일 한식당 ‘세옥(SEOK)’이 문을 열었다. '
     '서울 ‘밍글스’를 이끄는 미쉐린 3스타 셰프 강민구(42)가 파리에 처음 선보인 레스토랑이다. 강민구 셰프는 방송에 출연했다.',
     '지난달 17일 한식당 ‘세옥(SEOK)’이 문을 열었다', 'opening', {'rest': '세옥', 'chef': '강민구'}),
    ('https://www.wkorea.com/2026-10-20/강민구의-seok/',
     '서울에서 미쉐린 3스타를 받은 밍글스의 셰프 강민구가 파리에서 처음 선보이는 레스토랑 ‘SEOK’이 파크 하얏트 파리-방돔(Park Hyatt Paris-Vendôme)에 문을 열었다. '
     '강민구 셰프는 방송에 출연했다.',
     '파리에서 처음 선보이는 레스토랑 ‘SEOK’이 파크 하얏트 파리-방돔(Park Hyatt Paris-Vendôme)에 문을 열었다', 'opening', {'rest': 'SEOK', 'chef': '강민구'}),
    ('https://digitalchosun.dizzo.com/site/data/html_dir/2026/09/07/2026090780197.html',
     '갈라 디너 이후에도 협업은 이어진다. 10월 4일부터 31일까지 더 플라잉 호그에서 두 셰프가 함께 구성한 \'스와니예 × 라 리스트 셀렉션 7코스\'가 별도로 운영된다. '
     '김정석 셰프는 방송에 출연했다.',
     "10월 4일부터 31일까지 더 플라잉 호그에서 두 셰프가 함께 구성한 '스와니예 × 라 리스트 셀렉션 7코스'가 별도로 운영된다", 'menu', {'rest': '더 플라잉 호그', 'chef': '김정석'}),
]


@pytest.mark.parametrize('url,text,quote,label,kw', CHEF_CASES)
def test_review_chef_articles_reach_the_model(tmp_path, url, text, quote, label, kw):
    conn, sp, log, raw = review_env(tmp_path, url, text)
    add_f(conn, 'x_chef', label, quote, url, text, **kw)
    calls = []
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {'x_chef': 'approve'}, calls))
    assert s['skipped_entertainment'] == 0 and s['calls'] == 1 and s['approved'] == 1
    sent = json.loads(calls[0]['messages'][2]['content'])['findings']
    assert any(f['id'] == 'x_chef' for f in sent)                 # the finding really reached the model
    seed = json.loads(sp.read_text(encoding='utf-8'))
    pub = seed.get('ai_reviewed_unlinked', [])
    assert any(p['extraction_id'] == 'x_chef' and p['badge'] == 'AI 검토' and p['src'] == url for p in pub)


def test_review_chef_competition_interview_not_skipped(tmp_path):
    conn, sp, log, raw = review_env(tmp_path, 'https://press.test/interview', CHEF_CLUSTERED)
    quote = '서울 성동구 성수동의 레스토랑 달빛정원은 2026년 3월 문을 열었다'
    add_f(conn, 'x_int', 'opening', quote, 'https://press.test/interview', CHEF_CLUSTERED, rest='달빛정원')
    calls = []
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {'x_int': 'approve'}, calls))
    assert s['skipped_entertainment'] == 0 and s['calls'] == 1 and s['approved'] == 1 and s['published'] == 1
    seed = json.loads(sp.read_text(encoding='utf-8'))
    assert seed['restaurants'][0]['ai_reviewed'][0]['extraction_id'] == 'x_int'


def test_review_regression_guard_ai_review_text():
    assert entertainment_reason(None, TEXT) is None              # one 드라마 sentence among real facts still goes to the model


# ───────────── C. collect-stage integration ─────────────
def bnt_doc(text, suffix='1'):
    d = doc(text, suffix)
    d.url = d.canonical_url = BNT_COMP
    d.host = 'bntnews.co.kr'
    return d


def test_collect_competition_no_rows_one_audit(world):
    c = world
    r = process_doc(c, bnt_doc(COMP), Gazetteer(c), tier='press')
    assert r['skipped_entertainment'] == 1 and r['new'] == 0 and r['total'] == 0
    assert c.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference'").fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM doc_target').fetchone()[0] == 0      # 서하람 mention must not be counted
    rows = c.execute("SELECT * FROM review_queue WHERE reason='skipped_entertainment'").fetchall()
    assert len(rows) == 1
    q = rows[0]
    assert q['state'] == 'dismissed' and q['subject_type'] == 'fetch' and q['subject_id'] == 'f' and q['created_at']
    p = json.loads(q['payload_json'])
    assert p['skipped_texts'] == 1 and p['url'] == BNT_COMP and len(p['detail']['cues']) >= 2
    process_doc(c, bnt_doc(COMP), Gazetteer(c), tier='press')                   # idempotent
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='skipped_entertainment'").fetchone()[0] == 1
    assert c.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] == 0


def test_collect_chef_interview_unchanged(world):
    c = world
    r = process_doc(c, doc(CHEF_TEXT), Gazetteer(c), tier='press')
    assert r['skipped_entertainment'] == 0 and r['new'] >= 1
    assert c.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] >= 1
    assert c.execute('SELECT COUNT(*) FROM doc_target').fetchone()[0] >= 1
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='skipped_entertainment'").fetchone()[0] == 0


def test_collect_chef_paid_extraction_and_queueing_unchanged(world):
    c = world
    text = '달빛정원의 운영에 대한 중요한 소식입니다.'
    a = adapter(c, {'findings': [finding(text)]})
    r = process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)
    assert r['new'] == 1 and r['skipped_entertainment'] == 0 and a.stats['called'] == 1
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference'").fetchone()[0] == 1


def test_collect_feed_partial_skip(world):
    c = world
    d = doc('')
    d.items = [
        {'title': '파이널 테이블 첫 방송', 'summary': 'JTBC 새 토일드라마가 첫 방송된다. 극 중 셰프 서하람의 서바이벌 이야기.', 'date': '2026-10-16'},
        {'title': '달빛정원 오픈', 'summary': '달빛정원은 2026-10-01 문을 열었다. 한도윤 셰프가 총괄 셰프를 맡는다.', 'date': '2026-10-02'},
    ]
    r = process_doc(c, d, Gazetteer(c), tier='press')
    assert r['skipped_entertainment'] == 1 and r['new'] >= 1
    quotes = [row['quote'] for row in c.execute('SELECT quote FROM extraction').fetchall()]
    assert all('달빛정원' in q for q in quotes) and not any('파이널' in q for q in quotes)
    q = c.execute("SELECT payload_json FROM review_queue WHERE reason='skipped_entertainment'").fetchone()
    assert json.loads(q['payload_json'])['skipped_texts'] == 1
    tids = {row['target_id'] for row in c.execute('SELECT target_id FROM doc_target').fetchall()}
    seo = c.execute("SELECT target_id FROM target WHERE name_ko='서하람'").fetchone()[0]
    assert seo not in tids and tids                              # mentions only from the kept item


def test_collect_adapter_gate_no_paid_call(world):
    c = world
    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=lambda *args: pytest.fail('paid call on entertainment text'),
                        balance_transport=healthy_balance))
    assert a.extract(bnt_doc(COMP), [], Gazetteer(c)) == []
    assert a.stats['skipped_entertainment'] == 1 and a.stats['called'] == 0
    assert c.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0] == 0      # no budget reservation
    assert c.execute('SELECT COUNT(*) FROM ai_review_hold').fetchone()[0] == 0  # no recheck hold


def test_collect_adapter_multi_window_counts_once(world):
    c = world
    long_ent = '예능 프로그램에서 생존팀이 공개되고 탈락 팀이 결정된다. ' * 400   # > 12000 chars, cues in every window
    assert len(long_ent) > 12000
    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=lambda *args: pytest.fail('paid call on entertainment text'),
                        balance_transport=healthy_balance))
    assert a.extract(bnt_doc(long_ent, '2'), [], Gazetteer(c)) == []
    assert a.stats['skipped_entertainment'] == 1 and a.stats['considered'] == 1 and a.stats['called'] == 0


def test_collect_recheck_completes_not_partial_inspection(world):
    c = world
    budget, seeds = setup_due(c)
    recheck.admit(c, seeds, budget, now=NOW)
    link = c.execute('SELECT * FROM restaurant_recheck_job').fetchone()
    c.execute("UPDATE fetch_record SET job_id=? WHERE fetch_id='f'", (link['job_id'],)); c.commit()
    text = COMP + ' 달빛정원 소식도 전했다.'                       # mentions the due restaurant's alias
    process_doc(c, bnt_doc(text), Gazetteer(c), tier='official')
    assert c.execute("SELECT 1 FROM restaurant_recheck_job WHERE fetch_id='f' AND extraction_id IS NULL").fetchone()  # inspected marker
    c.execute("UPDATE crawl_job SET state='done' WHERE job_id=?", (link['job_id'],))
    c.execute("UPDATE fetch_record SET extracted_at=? WHERE fetch_id='f'", (db.utcnow(),)); c.commit()
    recheck.reconcile(c)
    row = c.execute("SELECT * FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()
    assert row['last_checked_at'] and row['last_outcome'] == 'reviewed_or_no_change'
    assert c.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0] == 0


def test_collect_skipped_doc_no_per_url_alert(world, tmp_path):
    c = world
    raw = RawStore(str(tmp_path / 'raw'))
    ref = raw.put_text('bntnews.co.kr', COMP)
    c.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_chars,content_hash,fetched_at,extracted_at,fetcher_version,extractor_version)
                 VALUES ('old',?,?,?,'html','ok',1000,'oldhash','2026-11-01T00:00:00Z',?,'fixture',?)""",
              (BNT_COMP, BNT_COMP, 'bntnews.co.kr', db.utcnow(), cycle.EXTRACTOR_VERSION))
    for i in range(2):
        c.execute("""INSERT INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,span_start,span_end,quote,extractor,extractor_version,created_at)
                     VALUES (?,?,?,?,'career','claim',0,1,'q','rule:t','t','x')""", (f'ext_old_{i}', 'old', BNT_COMP, 'bntnews.co.kr'))
    c.execute("UPDATE fetch_record SET url=?,canonical_url=?,host=?,text_ref=?,content_hash='newhash',text_chars=? WHERE fetch_id='f'",
              (BNT_COMP, BNT_COMP, 'bntnews.co.kr', ref, len(COMP)))
    c.commit()
    bot = cycle.Bot(c, data_dir=str(tmp_path / 'data'), deepseek=False)
    bot.raw = raw
    res = cycle.stage_extract(SimpleNamespace(env={'bot': bot}, cost=meter(c)))
    assert res.output['skipped_entertainment'] == 1
    assert c.execute("SELECT COUNT(*) FROM extractor_alert WHERE kind IN ('yield_drop','structure_change')").fetchone()[0] == 0


def test_collect_host_window_excludes_skipped_fetches(world):
    c = world
    seo = c.execute("SELECT target_id FROM target WHERE name_ko='서하람'").fetchone()[0]
    fids = []
    for i in range(6):
        fid = f'ent_{i}'
        url = f'https://ent.test/a/{i}'
        c.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_chars,fetched_at,fetcher_version,extractor_version)
                     VALUES (?,?,?,'ent.test','html','ok',500,?,'t','t')""", (fid, url, url, f'2026-11-0{i + 1}T00:00:00Z'))
        c.execute('INSERT INTO doc_target(fetch_id,target_id,mentions) VALUES (?,?,2)', (fid, seo))
        fids.append(fid)
    c.commit()
    # six silent docs that mention a target WOULD raise one host alert ...
    assert len(host_yield_check(c, 'ent.test')) == 1
    # ... but marking them entertainment-skipped removes them from the window and auto-acknowledges it
    for fid in fids:
        rid = queue_review(c, 'skipped_entertainment', 'fetch', fid, {'url': f'https://ent.test/{fid}'}, dedupe_key='ent:fetch:' + fid)
        c.execute("UPDATE review_queue SET state='dismissed' WHERE review_id=?", (rid,))
    c.commit()
    assert host_yield_check(c, 'ent.test') == []
    assert c.execute("SELECT COUNT(*) FROM extractor_alert WHERE host='ent.test' AND state='open'").fetchone()[0] == 0
    # a yielding doc keeps the host healthy
    c.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_chars,fetched_at,fetcher_version,extractor_version)
                 VALUES ('ok','https://ent.test/ok','https://ent.test/ok','ent.test','html','ok',500,'2026-11-09T00:00:00Z','t','t')""")
    c.execute("""INSERT INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,span_start,span_end,quote,extractor,extractor_version,created_at)
                 VALUES ('ext_ok','ok','https://ent.test/ok','ent.test','career','claim',0,1,'q','rule:t','t','x')""")
    c.commit()
    assert host_yield_check(c, 'ent.test') == []
    assert c.execute("SELECT COUNT(*) FROM extractor_alert WHERE host='ent.test' AND state='open'").fetchone()[0] == 0


def test_collect_gap_stays_not_extracted(world):
    c = world
    tid = c.execute("SELECT target_id FROM target WHERE name_ko='달빛정원'").fetchone()[0]
    c.execute("INSERT OR IGNORE INTO gap(gap_id,target_id,field,state,opened_at,attempts) VALUES ('g-ent',?,'opened','open',?,1)", (tid, db.utcnow()))
    c.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetched_at,fetcher_version,extractor_version)
                 VALUES ('f2',?,?,?,'html','ok',?,'fixture','rules-new')""", (BNT_COMP + '/2', BNT_COMP + '/2', 'bntnews.co.kr', db.utcnow()))
    c.commit()
    text = COMP + ' 달빛정원 소식도 전했다.'
    process_doc(c, bnt_doc(text), Gazetteer(c), tier='press')
    d2 = bnt_doc(text, '2'); d2.fetch_id = 'f2'; d2.url = d2.canonical_url = BNT_COMP + '/2'
    process_doc(c, d2, Gazetteer(c), tier='press')
    assert c.execute('SELECT COUNT(*) FROM doc_target WHERE target_id=?', (tid,)).fetchone()[0] == 0
    from kg_collect import gaps
    gaps.classify_gaps(c)
    assert c.execute("SELECT klass FROM gap WHERE gap_id='g-ent'").fetchone()[0] == 'not_extracted'

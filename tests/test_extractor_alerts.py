"""Host-level yield_drop alerts (offline): site boilerplate and non-prose docs are not 'mentions'; one open alert per host; auto-acknowledge."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

import pytest   # noqa: E402

from kg_daemon import db as kgdb   # noqa: E402
from kg_collect import pipeline   # noqa: E402
from kg_collect.rawstore import RawStore   # noqa: E402
from kg_collect.targets import Gazetteer, upsert_target   # noqa: E402

SIDEBAR = ' 많이 본 뉴스 뮤지컬 배우 서하람과 가수 최유정이 새 예능 마스터로 합류한다. 서하람 서하람 관련기사 '


@pytest.fixture()
def env(tmp_path):
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    upsert_target(c, 'chef', '서하람', 'Haram Seo', now='2026-10-27T00:00:00Z')
    upsert_target(c, 'restaurant', '모래시계', 'Hourglass', now='2026-10-27T00:00:00Z')
    c.commit()
    pipeline._CTX_CACHE.clear()
    return c, RawStore(str(tmp_path / 'raw')), [0]


def add_doc(c, raw, seq, host, text, method='html', extractions=0):
    seq[0] += 1
    fid = f'fch_{seq[0]:04d}'
    url = f'https://{host}/a/{seq[0]}'
    ref = raw.put_text(host, text)
    c.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_ref,text_chars,fetcher_version,extractor_version,fetched_at)
                 VALUES (?,?,?,?,?,'ok',?,?,'t','t',?)""", (fid, url, url, host, method, ref, len(text), f'2026-10-28T00:{seq[0]:02d}:00Z'))
    for t, n in pipeline.mentions_of(Gazetteer(c), text).items():
        c.execute('INSERT INTO doc_target(fetch_id,target_id,mentions) VALUES (?,?,?)', (fid, t, n))
    for i in range(extractions):
        c.execute("""INSERT INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,span_start,span_end,quote,extractor,extractor_version,created_at)
                     VALUES (?,?,?,?,'career','claim',0,1,'q','rule:t','t','x')""", (f'ext_{fid}_{i}', fid, url, host))
    c.commit()
    return fid, url


def open_alerts(c, host):
    return c.execute("SELECT * FROM extractor_alert WHERE host=? AND state='open'", (host,)).fetchall()


def test_sidebar_boilerplate_mentions_never_raise_a_host_alert(env):
    c, raw, seq = env
    gaz = Gazetteer(c)
    for i in range(8):
        add_doc(c, raw, seq, 'ent.test', f'연예 기사 {i}번 본문입니다. 배우 아무개가 드라마에 출연한다.' + SIDEBAR + f'꼬리 {i}')
        assert pipeline.host_yield_check(c, 'ent.test', raw, gaz) == []
    assert open_alerts(c, 'ent.test') == []


def test_sitemaps_are_not_prose_and_never_count(env):
    c, raw, seq = env
    gaz = Gazetteer(c)
    for i in range(8):
        add_doc(c, raw, seq, 'news.test', f'https://news.test/2026/{i}/moraesigye-on-the-menu 모래시계 서하람 서하람 {i}', method='sitemap')
        assert pipeline.host_yield_check(c, 'news.test', raw, gaz) == []
    assert open_alerts(c, 'news.test') == []


def test_real_silent_host_gets_exactly_one_alert_then_auto_acknowledges(env):
    c, raw, seq = env
    gaz = Gazetteer(c)
    texts = ['가을 메뉴 발표회에서 서하람 셰프가 버섯 요리를 선보였다. 현장 반응은 뜨거웠고 서하람도 만족했다.',
             '서하람이 직접 고른 와인 리스트가 화제다. 한남동 모래시계 손님들이 서하람의 추천을 따라 줄을 섰다는 후문이다.',
             '요리 경연 심사위원으로 나선 서하람, 냉정한 평가로 눈길. 참가자들은 서하람 앞에서 긴장한 표정이었다.',
             '주말 장터에 깜짝 등장한 서하람 셰프는 제철 채소를 한아름 샀다. 모래시계 직원도 동행했다.',
             '모래시계의 겨울 코스 예약이 열리자마자 마감됐다. 서하람은 디저트에 공을 들였다고 밝혔다.',
             '유튜브 채널을 연 서하람, 첫 영상에서 칼 가는 법을 알려줬다. 조회수는 하루 만에 십만을 넘겼고 서하람은 감사 인사를 남겼다.',
             '지역 농가와 협업한 서하람 셰프의 도시락이 출시됐다. 서하람에 따르면 수익 일부는 기부된다고 한다.',
             '해외 미식 행사 초청을 받은 서하람이 출국했다. 모래시계는 그동안 임시 휴무에 들어간다.',
             '서하람 셰프가 쓴 첫 에세이가 서점가에 나왔다. 책에는 서하람의 어린 시절 부엌 이야기가 담겼다.']
    fired = []
    for t in texts:
        add_doc(c, raw, seq, 'mag.test', t)
        fired += pipeline.host_yield_check(c, 'mag.test', raw, gaz, now=f'2026-10-28T01:{seq[0]:02d}:00Z')
    assert len(fired) == 1 and len(open_alerts(c, 'mag.test')) == 1       # 9 silent docs -> one alert, later docs only update it
    a = open_alerts(c, 'mag.test')[0]
    assert json.loads(a['baseline_json']) == {'scope': 'host'} and json.loads(a['observed_json'])['docs'] == 6
    add_doc(c, raw, seq, 'mag.test', '서하람 셰프는 모래시계 출신이다. 서하람은 독립했다.', extractions=2)
    assert pipeline.host_yield_check(c, 'mag.test', raw, gaz) == []
    assert open_alerts(c, 'mag.test') == []
    ack = c.execute("SELECT * FROM extractor_alert WHERE host='mag.test'").fetchone()
    assert ack['state'] == 'acknowledged' and 'yields labelled extractions again' in json.loads(ack['observed_json'])['reason']


def test_reconcile_collapses_legacy_per_document_alert_flood(env):
    c, raw, seq = env
    gaz = Gazetteer(c)
    for i in range(7):
        _, url = add_doc(c, raw, seq, 'ent.test', f'연예 기사 {i}번 본문.' + SIDEBAR)
        c.execute("""INSERT INTO extractor_alert(alert_id,host,canonical_url,kind,detail,baseline_json,observed_json,created_at)
                     VALUES (?,?,?,'yield_drop','last 6 documents on ent.test mention targets but produced no labelled extraction','{}','{"docs": 6}','2026-10-28T00:00:00Z')""",
                  (f'alr_{i}', 'ent.test', url))
    # an unrelated per-URL yield_drop (same URL went from >=2 extractions to 0) is NOT host-level and must stay open
    c.execute("""INSERT INTO extractor_alert(alert_id,host,canonical_url,kind,detail,created_at)
                 VALUES ('alr_url','ent.test','https://ent.test/a/1','yield_drop','previously 3 labelled extractions on this URL, now 0','2026-10-28T00:00:00Z')""")
    c.commit()
    rep = pipeline.reconcile_host_yield_alerts(c, raw, gaz)
    assert rep == {'hosts': 1, 'open_before': 8, 'open_after': 1}
    assert [r['alert_id'] for r in open_alerts(c, 'ent.test')] == ['alr_url']
    assert pipeline.reconcile_host_yield_alerts(c, raw, gaz)['open_after'] == 1   # idempotent


def test_reconcile_keeps_one_when_the_host_really_is_silent(env):
    c, raw, seq = env
    gaz = Gazetteer(c)
    for i in range(6):
        _, url = add_doc(c, raw, seq, 'mag.test', f'{i}호 특집: 서하람 셰프와 모래시계, 서하람의 하루 {i * 11}.')
        c.execute("""INSERT INTO extractor_alert(alert_id,host,canonical_url,kind,detail,baseline_json,observed_json,created_at)
                     VALUES (?,?,?,'yield_drop','last 6 documents on mag.test mention targets but produced no labelled extraction','{}','{"docs": 6}',?)""",
                  (f'alr_{i}', 'mag.test', url, f'2026-10-28T00:0{i}:00Z'))
    c.commit()
    assert pipeline.reconcile_host_yield_alerts(c, raw, gaz) == {'hosts': 1, 'open_before': 6, 'open_after': 1}
    assert open_alerts(c, 'mag.test')[0]['alert_id'] == 'alr_0'

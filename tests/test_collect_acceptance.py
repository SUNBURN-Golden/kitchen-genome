"""Acceptance tests of the ACTIVE collection bot against a SYNTHETIC web (tests/collect_world.py).
They prove behaviour (discovery, evaluation, html/pdf/image fetch, gaps, change detection, extractor alerts, resume, idempotency).
They do not measure accuracy on real press; real-run evidence lives in docs/data/COLLECTION_BOT.md."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from collect_world import SEED, TODAY, FakeWeb, html, seeds_for_world   # noqa: E402
from kg_collect import cycle                                           # noqa: E402
from kg_collect.scoreboard import scoreboard                           # noqa: E402
from kg_daemon import db as kgdb                                       # noqa: E402


@pytest.fixture()
def world(tmp_path):
    conn = kgdb.connect(str(tmp_path / 'kg.db'))
    web = FakeWeb()
    cycle.init_db(conn, seed=SEED)
    bot = cycle.Bot(conn, data_dir=str(tmp_path), net=web.net(), seeds=seeds_for_world(), today=TODAY, units=80, registry_path=str(tmp_path / 'registry.discovered.yaml'), ocr=True)
    return conn, web, bot, tmp_path


def run(bot, **kw):
    return cycle.run_cycle(bot, **kw)


def q(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


def test_init_builds_targets_and_gaps(world):
    conn, *_ = world
    assert q(conn, "SELECT COUNT(*) n FROM target WHERE kind='restaurant'")[0]['n'] == 3
    assert q(conn, "SELECT COUNT(*) n FROM gap WHERE state='open'")[0]['n'] > 0


def test_cycle_discovers_new_sources_and_targets(world):
    conn, web, bot, tmp = world
    for _ in range(3):
        rep = run(bot)
        assert rep['status'] == 'succeeded', rep
    hosts = {r['host'] for r in q(conn, "SELECT DISTINCT host FROM fetch_record WHERE outcome='ok'")}
    assert {'press1.test', 'press2.test', 'dir.test'} <= hosts                       # hosts that were in NO initial list
    prof = {r['host']: r for r in q(conn, 'SELECT * FROM domain_profile')}
    assert prof['press1.test']['state'] == 'promoted' and not prof['press1.test']['in_initial_registry']
    # growth path 3: a new domain advertised its own feed and the bot followed it
    assert any(u.endswith('/feed.xml') for u in web.hits)
    assert os.path.exists(bot.registry_path) and 'press1.test' in open(bot.registry_path, encoding='utf-8').read()
    # candidate chef that was not in the target list
    new = q(conn, "SELECT * FROM target WHERE origin='discovered'")
    assert any(t['name_ko'] == '오세준' for t in new), [t['name_ko'] for t in new]
    sb = scoreboard(conn)
    assert sb['new_valid_sources']['count'] >= 1 and sb['new_targets']['total_discovered'] >= 1


def test_multipage_pdf_image_and_failures(world):
    conn, web, bot, tmp = world
    for _ in range(3):
        run(bot)
    urls = {r['url']: r for r in q(conn, "SELECT * FROM fetch_record WHERE outcome='ok'")}
    assert 'https://dir.test/list?p=1' in urls and 'https://dir.test/list?p=2' in urls         # pagination followed
    pdf = urls.get('https://dalbit.test/files/menu.pdf')
    img = urls.get('https://dalbit.test/img/menu.png')
    assert pdf and pdf['method'] == 'ocr' and img and img['method'] == 'ocr'                   # scanned PDF + menu image
    assert '달' in bot.raw.read_text(pdf['text_ref']) and '메뉴' in bot.raw.read_text(img['text_ref'])
    for r in (pdf, img):
        assert r['raw_ref'] and r['content_hash'] and r['fetched_at'] and r['extractor_version']
        assert os.path.exists(os.path.join(tmp, 'raw', r['raw_ref']))


def test_login_200_and_empty_200_are_failures(world):
    conn, web, bot, tmp = world
    from kg_collect.fetch import Fetcher
    f = Fetcher(conn, bot.net, bot.raw)
    d1 = f.fetch('https://dalbit.test/members')[0]
    d2 = f.fetch('https://dalbit.test/blank')[0]
    assert (d1.outcome, d2.outcome) == ('fail_login_page', 'fail_empty')


def converge(bot, n=6):
    for _ in range(n):
        rep = run(bot)
        assert rep['status'] == 'succeeded', rep
        if rep['outputs']['fetch']['jobs_run'] == 0:
            break


def test_gaps_filled_with_evidence_and_never_by_a_model(world):
    conn, web, bot, tmp = world
    before = scoreboard(conn)['required_field_coverage']['coverage_now']
    converge(bot)
    sb = scoreboard(conn)
    assert sb['required_field_coverage']['coverage_now'] > before
    assert sb['gap_resolution']['resolved_with_evidence'] >= 1
    for g in q(conn, "SELECT * FROM gap WHERE state='resolved'"):
        e = conn.execute('SELECT * FROM extraction WHERE extraction_id=?', (g['resolved_by_extraction'],)).fetchone()
        assert e is not None and e['quote'] and e['canonical_url'] and e['extractor'].startswith('rule:')     # evidence, produced by a rule, never by a model
        assert e['epistemic'] in ('claim', 'fact')
    # a bot claim is never 'verified' on its own
    assert not q(conn, "SELECT 1 FROM claim WHERE created_by LIKE 'collect:%' AND status IN ('verified','ai_inference')")


COUNT_TABLES = ('extraction', 'claim', 'evidence', 'restaurant_status_event', 'employment', 'field_history')


def counts(conn):
    return {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in COUNT_TABLES}


def test_rerun_is_idempotent(world):
    conn, web, bot, tmp = world
    converge(bot, 8)
    first = counts(conn)
    assert first['extraction'] > 0
    conn.execute("UPDATE crawl_job SET state='queued', attempts=0 WHERE state IN ('done','failed')")     # force a full re-fetch of everything
    conn.commit()
    converge(bot, 3)
    assert counts(conn) == first
    assert q(conn, 'SELECT COUNT(*) n FROM fetch_record')[0]['n'] > 0


def test_affiliation_change_old_vs_new_detected(world):
    import datetime as dt
    conn, web, bot, tmp = world
    converge(bot, 8)
    chef = conn.execute("SELECT chef_id FROM chef WHERE display_name='한도윤'").fetchone()['chef_id']
    open_before = q(conn, "SELECT b.branch_id, r.name FROM employment e JOIN branch b USING(branch_id) JOIN restaurant r USING(restaurant_id) WHERE e.chef_id=? AND e.state='active' AND e.valid_to_kind='open'", chef)
    assert [r['name'] for r in open_before] == ['달빛정원']
    today = dt.date.today().isoformat()
    web.pages['https://press1.test/a/4'] = ('text/html', html('셰프 이동', '<p>한도윤 셰프는 모래시계로 옮겼다. 모래시계는 한남동에 있는 프렌치 레스토랑이다. 이 문서는 수집 봇 시험을 위한 가상의 안내문이며 실제 식당과 무관하다. '
                                                          '모든 이름과 주소는 지어낸 것이다. 문서의 길이를 채우기 위한 문장이 이어진다. 시험용 문서는 여기서 끝난다.</p>', pub=today))
    web.search_items.append(('한도윤 셰프 모래시계 이적', 'https://press1.test/a/4', '한도윤 셰프 모래시계로 옮겼다'))
    conn.execute("UPDATE crawl_job SET state='queued', attempts=0 WHERE path IN ('keyword_search','reverse_gap')")
    conn.commit()
    converge(bot, 6)
    h = q(conn, "SELECT * FROM field_history WHERE field='affiliation'")
    assert len(h) == 1 and h[0]['old_value'] == '달빛정원' and h[0]['new_value'] == '모래시계'
    assert h[0]['latency_hours'] is not None and 0 <= h[0]['latency_hours'] <= 48
    # the move is recorded as UNVERIFIED (pending verification), with evidence; the old employment is closed, not deleted
    mv = q(conn, "SELECT e.state, e.valid_to_kind, c.status FROM employment e JOIN claim c USING(claim_id) WHERE e.chef_id=? ORDER BY e.rowid", chef)
    assert any(r['status'] == 'unverified' for r in mv)
    assert scoreboard(conn)['change_latency_hours']['changes_detected'] >= 1


def test_broken_extractor_is_detected(world):
    conn, web, bot, tmp = world
    long_text = ' '.join(f'서하람 셰프는 가온재 출신이다. 서하람 셰프는 모래시계의 헤드셰프로 있다. 모래시계는 한남동에 있는 프렌치 레스토랑이다. {i}번째 문단의 시험용 문장이 이어진다.' for i in range(8))
    web.pages['https://press1.test/a/5'] = ('text/html', html('긴 기사', f'<p>{long_text}</p>'))
    web.search_items.append(('서하람 셰프 인터뷰', 'https://press1.test/a/5', '서하람 셰프 모래시계'))
    converge(bot, 6)
    assert q(conn, "SELECT COUNT(*) n FROM extractor_alert")[0]['n'] == 0
    # the site is redesigned: same URL, article body now lives in a script/JS shell -> only boilerplate text remains
    web.pages['https://press1.test/a/5'] = ('text/html', html('긴 기사', '<p>기사 본문은 새 사이트 개편으로 인해 이동하였습니다. 새 주소에서 확인하시기 바랍니다. 불편을 드려 죄송합니다. 감사합니다 감사합니다 감사합니다.</p>'))
    conn.execute("UPDATE crawl_job SET state='queued', attempts=0 WHERE url='https://press1.test/a/5'")
    conn.commit()
    rep = run(bot)
    kinds = {r['kind'] for r in q(conn, 'SELECT kind FROM extractor_alert')}
    assert kinds & {'text_collapse', 'yield_drop', 'structure_change', 'login_wall'}, kinds
    assert rep['outputs']['quality']['open_extractor_alerts'] >= 1


def test_resume_from_checkpoint_after_cost_cap(world):
    conn, web, bot, tmp = world
    r1 = run(bot, cost_cap=4)
    assert r1['status'] == 'paused_cost_cap', r1['status']
    done_before = q(conn, "SELECT COUNT(*) n FROM crawl_job WHERE state IN ('done','failed','blocked')")[0]['n']
    fetched = {r['url'] for r in q(conn, 'SELECT url FROM fetch_record')}
    r2 = run(bot)
    assert r2['resumed_from'] == r1['run_id'] and r2['status'] == 'succeeded'
    # nothing that was already fetched in run 1 was fetched again as part of the resumed fetch stage
    again = [r for r in q(conn, 'SELECT url, COUNT(*) n FROM fetch_record WHERE job_id IN (SELECT job_id FROM crawl_job) GROUP BY job_id, url HAVING n>1')]
    assert not [a for a in again if a['url'] in fetched]
    assert q(conn, "SELECT COUNT(*) n FROM crawl_job WHERE state IN ('done','failed','blocked')")[0]['n'] > done_before

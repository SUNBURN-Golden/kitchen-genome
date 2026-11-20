"""KST search rotation with migrated temporary state and stored article fixtures."""
from collections import Counter
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from test_site_publication import NOW, FIRST, SECOND, PRESS, article, process, publication_world  # noqa: F401
from kg_collect import cycle, discover, scheduler
from kg_collect.net import PoliteNet, Response
from kg_collect.targets import load_seed_targets, tid, upsert_target
from kg_daemon.db import connect

ROOT = Path(__file__).resolve().parents[1]
NOW_DT = dt.datetime.fromisoformat(NOW.replace('Z', '+00:00'))


def test_full_backlog_prioritizes_kr_en_for_four_empty_kitchens(publication_world):
    w = publication_world
    seed = json.loads((ROOT / 'data/seed.json').read_text())
    load_seed_targets(w.conn, seed)
    seeds = discover.load_seeds()
    caps = seeds['caps']
    for i in range(469):
        q = f'오래된 일반 검색 {i}'
        assert discover.enqueue(w.conn, path='keyword_search', pool='backfill', url=discover.search_url(seeds, q), query=q, caps=caps)
    counts, due, n = discover.reconcile_mother_jobs(w.conn, seed, NOW_DT.date(), seeds, caps)
    assert n == 11
    plan = scheduler.plan(w.conn, 60, NOW_DT, mother_counts=counts, due_mother=due)
    first = plan['reserved_queries'][:8]
    ordered = [w.conn.execute('SELECT * FROM crawl_job WHERE job_id=?', (jid,)).fetchone() for jid in first]
    assert len(first) == 8 and all(discover.query_family(j, counts) == 'mother' for j in ordered)
    assert len({j['target_id'] for j in ordered[:4]}) == 4
    assert Counter(j['target_id'] for j in ordered) == Counter({tid('restaurant', k['name']): 2 for k in seed['kitchens']
                                                               if k['id'] in ('layeon', 'mitou', 'allen', 'allaprima')})
    assert {discover.mother_language(j['query']) for j in ordered if j['target_id'] == tid('restaurant', '라연')} == {'ko', 'en'}
    assert all('신라호텔' in j['query'] for j in ordered if '라연' in j['query'])
    for days in range(3):
        day = NOW_DT.date() + dt.timedelta(days=days)
        lang = {discover.mother_language(j['query']) for j in discover.mother_kitchen_jobs(seed, day)}
        assert lang == {'ko', 'en'}
    assert len({j['query'] for i in range(3) for j in discover.mother_kitchen_jobs(seed, NOW_DT.date() + dt.timedelta(days=i))
                if j['target_id'] == tid('restaurant', '라연')}) == 6

    seed['restaurants'] += [{'id': f'test-{i}', 'type': 'alumni', 'kitchen': 'layeon'} for i in range(3)]
    counts, due, _ = discover.reconcile_mother_jobs(w.conn, seed, NOW_DT.date(), seeds, caps)
    target = tid('restaurant', '라연')
    assert counts[target] == 3
    assert {r[0] for r in w.conn.execute("SELECT state FROM crawl_job WHERE target_id=?", (target,))} == {'dropped'}
    seed['restaurants'].pop()
    discover.reconcile_mother_jobs(w.conn, seed, NOW_DT.date(), seeds, caps)
    assert {r[0] for r in w.conn.execute("SELECT state FROM crawl_job WHERE target_id=?", (target,))} == {'queued'}


def test_promoted_entities_and_explicit_former_venue_snowball(publication_world):
    w = publication_world
    upsert_target(w.conn, 'chef', '최근', origin='discovered', status='active')
    upsert_target(w.conn, 'chef', '고영범', origin='discovered', status='active')
    article(w, PRESS[0], FIRST)
    article(w, PRESS[1], SECOND)
    article(w, PRESS[2], '레스토랑 ‘가람정’ 출신 김도훈 셰프.')
    article(w, PRESS[2], '김도훈 셰프와 레스토랑 ‘다온당’이 함께 일했다.')
    process(w)
    assert {r['status'] for r in w.conn.execute("SELECT status FROM target WHERE name_ko IN ('오닐','김도훈')")} == {'active'}
    seeds = discover.load_seeds()
    bot = cycle.Bot(w.conn, data_dir=str(w.raw.root.parent), seeds=seeds, today=NOW_DT.date(), clock=lambda: NOW_DT)
    plan = cycle.stage_plan(SimpleNamespace(env={'bot': bot})).output
    all_queries = {r['query'] for r in w.conn.execute('SELECT query FROM crawl_job WHERE query IS NOT NULL')}
    assert {'"가람정" 출신 셰프', '"가람정" 수셰프 인터뷰', '"김도훈" 셰프 경력 인터뷰',
            '"김도훈" "오닐" 서울 오픈', '"최가람" 셰프 경력 인터뷰'} <= all_queries
    assert not any(s in q for s in ('고영범', '"최근"', '다온당') for q in all_queries)
    assert plan['new_jobs']['mother_kitchen'] == 4

    e = w.conn.execute("SELECT * FROM extraction WHERE org_text='가람정' AND ambiguity IS NULL LIMIT 1").fetchone()
    planned = discover.followup_jobs(w.conn, seeds, [e], parent_depth=2)
    assert planned and all(r['depth'] < 2 for r in planned)
    assert not discover.enqueue(w.conn, path='entity_followup', pool='backfill', url='https://fixture.test/too-deep', depth=3)


def test_query_ledger_caps_cooldown_restarts_and_lookback(publication_world):
    w = publication_world
    seeds = discover.load_seeds()
    limits = seeds['keyword_search']['daily_allowances']
    target = tid('chef', '최가람')
    other = tid('restaurant', '기존식당')
    all_targets = [upsert_target(w.conn, 'chef', f'테스트{i}') for i in range(10)]
    mothers = {tid('restaurant', k['name']): 0 for k in w.seed['kitchens']}
    for i in range(9):
        job = {'query': f'query {i}', 'target_id': None, 'job_id': f'q{i}'}
        assert discover.claim_query(w.conn, job, 'general', NOW_DT, limits) == (i < 8)
    for i, t in enumerate([target, target, target, other, *all_targets]):
        job = {'query': f'entity {i}', 'target_id': t, 'job_id': f'e{i}'}
        assert discover.claim_query(w.conn, job, 'entity', NOW_DT, limits, reserve=8) == (i < 9 and i != 2)
    with connect(str(w.db_path)) as restarted:
        assert not discover.claim_query(restarted, {'query': ' QUERY   0 ', 'target_id': target, 'job_id': 'different'}, 'entity', NOW_DT)
        assert discover.claim_query(restarted, {'query': 'query 0', 'target_id': target, 'job_id': 'new-day'}, 'general',
                                    NOW_DT + dt.timedelta(minutes=2))
        for i, family in enumerate(('general', 'entity')):
            job = {'query': f'entity spare {i}', 'target_id': all_targets[-1], 'job_id': f'spare{i}'}
            assert discover.claim_query(restarted, job, family, NOW_DT, limits) == (family == 'entity')

    today = NOW_DT.date()
    mother = discover.mother_kitchen_jobs(w.seed, today, seeds)[0]
    discover.enqueue(w.conn, **mother)
    jid = discover.job_id(mother['path'], mother['url'])
    def finish(at, error):
        w.conn.execute("UPDATE crawl_job SET state='done',last_error=?,finished_at=? WHERE job_id=?", (error, at.isoformat(), jid))
        w.conn.commit()
    finish(NOW_DT - dt.timedelta(hours=71), 'no_results')
    assert scheduler.requeue_due(w.conn, NOW_DT, mother_counts=mothers) == 0
    assert scheduler.requeue_due(w.conn, NOW_DT + dt.timedelta(hours=1), mother_counts=mothers) == 1
    finish(NOW_DT - dt.timedelta(hours=24), None)
    assert scheduler.requeue_due(w.conn, NOW_DT, mother_counts=mothers) == 1

    # The same 200-day-old item is admitted only under a mother-kitchen root.
    body = ('<rss><channel><item><title>최가람 셰프 레스토랑</title><description>셰프</description>'
            '<pubDate>Mon, 04 May 2026 00:00:00 GMT</pubDate><link>https://foodnews.news/food/older</link></item></channel></rss>').encode()
    sent = []
    def transport(url, *_):
        if url.endswith('/robots.txt'):
            return Response(url, 200, {}, b'User-agent: *\nAllow: /')
        sent.append(url)
        assert parse_qs(urlsplit(url).query)['q'][0] == mother['query']
        return Response(url, 200, {'content-type': 'application/rss+xml'}, body)
    net = PoliteNet(transport=transport, min_delay=0, sleep=lambda _: None)
    bot = cycle.Bot(w.conn, data_dir=str(w.raw.root.parent), net=net, seeds=seeds, today=today, clock=lambda: NOW_DT)
    ctx = SimpleNamespace(env={'bot': bot}, prev={'plan': {'order': [jid], 'pool_of': {jid: 'backfill'}, 'mother_counts': mothers}},
                          run_id='fixture', cursor={}, save_cursor=lambda _: None, cost=SimpleNamespace(charge=lambda *_: None))
    stats = cycle.stage_fetch(ctx).output
    assert len(sent) == stats['rss_queries']['mother'] == 1
    assert w.conn.execute("SELECT depth FROM crawl_job WHERE url='https://foodnews.news/food/older'").fetchone()[0] == 1

"""Fixture coverage for the private G2 roster, tier gate and leased checkpoints."""
import datetime as dt
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from kg_collect import cycle, discover, global_queue as queue, heavy, michelin
from kg_collect.net import PoliteNet, Response, UA
from kg_collect.rawstore import RawStore
from kg_daemon.db import connect, migrate

FIXTURES = Path(__file__).parent / 'fixtures' / 'michelin'
HOST = 'https://guide.michelin.com'
FRANCE = HOST + '/fr/en/selection/france/restaurants'
KOREA = HOST + '/kr/en/selection/south-korea/restaurants'
PRESS = 'https://news.test/france-2026'
NO_RESTAURANTS = b'<main><div class="no-results">No restaurants found</div></main>'


def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def wiki(contents, title, rev=9126):
    return {'query': {'redirects': [{'from': 'Old title', 'to': title}], 'pages': [{'title': title, 'revisions': [
        {'revid': rev, 'slots': {'main': {'content': contents}}}]}]}}


def http(url, body, *, ctype='text/html', status=200, headers=None):
    return Response(url, status, {'Content-Type': ctype, **(headers or {})}, body.encode() if isinstance(body, str) else body)


def rss(url, title):
    return f'<rss><channel><item><title>{title}</title><link>{url}</link></item></channel></rss>'


@pytest.fixture
def world(tmp_path):
    conn = connect(str(tmp_path / 'service.db'))
    migrate(conn)
    clock = [dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc).timestamp()]
    config = michelin.load_config()
    seeds = discover.load_seeds()
    return conn, clock, config, seeds, RawStore(str(tmp_path / 'raw'))


def transport(calls):
    def send(url, headers, timeout):
        assert headers['User-Agent'] == UA
        assert '/robots.txt' not in url
        calls.append((url, headers))
        path = urlsplit(url).path
        params = parse_qs(urlsplit(url).query)
        if url == HOST + '/en/restaurants':
            if headers.get('If-None-Match') == '"directory"':
                return http(url, b'', status=304)
            return http(url, fixture('directory.html'), headers={'ETag': '"directory"'})
        if '/france-and-monaco/restaurants' in path:
            return http(url, fixture('france_monaco.html'))
        if url == FRANCE or path.endswith('/france/restaurants'):
            if params.get('page') == ['2']:
                return http(url, fixture('france_page2.html'))
            if params.get('filter') == ['3-stars-michelin']:
                return http(url, '<link rel="next" href="?filter=3-stars-michelin&amp;page=2">' + fixture('france.html'))
            if headers.get('If-None-Match') == '"fr"':
                return http(url, b'', status=304)
            return http(url, fixture('france.html'), headers={'ETag': '"fr"', 'Last-Modified': 'Fri, 20 Nov 2026 00:00:00 GMT'})
        if url == KOREA:
            return http(url, fixture('south_korea.html'))
        if url.startswith(HOST) and '/restaurant/' not in path:
            return http(url, NO_RESTAURANTS)
        if url.startswith(HOST):
            return http(url, '<main><h1>Restaurant</h1><a href="https://venue.test/about-chef">Official website</a>'
                             '<div>Chef information and interviews</div></main>')
        if urlsplit(url).netloc.endswith('.wikipedia.org'):
            assert params['action'] == ['query'] and params['prop'] == ['revisions'] and params['rvslots'] == ['main']
            title = params['titles'][0]
            if title.endswith('in France'):
                return http(url, json.dumps(wiki(fixture('france.wikitext'), title)), ctype='application/json')
            return http(url, json.dumps({'query': {'pages': [{'title': title, 'missing': True}]}}), ctype='application/json')
        if 'bing.com' in url:
            query = params.get('q', [''])[0]
            if 'Guide Michelin France' in query:
                return http(url, rss(PRESS, 'Guide Michelin France 2026 unveiled'), ctype='application/rss+xml')
            if 'Le Cèdre' in query:
                return http(url, rss('https://venue.test/about-chef', 'Le Cèdre chef interview'), ctype='application/rss+xml')
            return http(url, '<rss><channel/></rss>', ctype='application/rss+xml')
        if url == PRESS:
            return http(url, fixture('announcement.html'))
        return http(url, '<article><h1>Chef interview at Le Cèdre</h1><p>Chef biography at Le Cèdre.</p></article>')
    return send


def drain(roster, now, *, stop=None, daily=False):
    order = []
    while job := queue.claim(roster.conn, now(), daily=daily):
        order.append(job)
        roster.work(job)
        if stop and stop(job):
            break
        assert len(order) < 1000
    return order


def test_directory_mirrors_uncovered_and_trap_scope():
    cfg = michelin.load_config()
    assert set(cfg['countries']) == {key for key in cfg['countries'] if isinstance(key, str) and len(key) == 2}
    entries, unknown = michelin.parse_directory(fixture('directory.html'), cfg['directory'], cfg)
    scopes = {entry['scope']: entry for entry in entries}
    assert not unknown
    assert scopes['france']['url'] == FRANCE
    assert scopes['france-and-monaco']['countries'] == ['FR', 'MC']
    assert scopes['tokyo']['city'] == 'Tokyo'
    assert scopes['iceland']['url'] is None
    assert 'mexico' not in scopes
    assert not heavy.excluded(FRANCE + '?page=2&filter=3-stars-michelin&sort=name', heavy.load_cfg())
    for url in ('https://publisher.test/restaurants?page=2', HOST + '/en/paris/restaurant/abc?page=2',
                FRANCE + '?page=2&year=2026', FRANCE + '?page=2&next=login',
                'https://guide.michelin.com.evil.test/en/restaurants?page=2'):
        assert heavy.excluded(url, heavy.load_cfg()), url


def test_wikitext_and_press_explicit_editions():
    rows, link = michelin.parse_wikipedia(wiki(fixture('france.wikitext'), 'List of French restaurants'), 3, 'FR')
    assert not rows.errors
    assert {(r.name, r.city, r.year) for r in rows.rows} == {
        ('Le Cèdre', 'Paris', 2025), ('Le Cèdre', 'Lyon', 2026), ("L'Écorce", 'Nice', 2026)}
    assert 'oldid=9126' in link and all('revision 9126, line ' in r.locator for r in rows.rows)
    rows, _ = michelin.parse_wikipedia(wiki(fixture('tokyo.wikitext'), '東京'), 3, 'JP')
    assert not rows.errors
    assert [(r.name, r.year) for r in rows.rows] == [('架空料理店', 2026)]
    rows, _ = michelin.parse_wikipedia(wiki('Unknown layout', 'Example'), 3, 'JP')
    assert not rows.rows and rows.errors
    cfg = michelin.load_config()
    html = fixture('announcement.html')
    parsed, source, year = michelin.parse_announcement(html, PRESS, {'countries': ['FR'], 'city': ''}, cfg)
    assert source == 'press_original' and year == 2026 and len(parsed.rows) == 2
    _, source, _ = michelin.parse_announcement(html.replace('Anne Leroy', 'Courtesy of Michelin'), PRESS, {'countries': ['FR'], 'city': ''}, cfg)
    assert source == 'press_mirror'


def test_worldwide_resumes_mid_page_pass_and_retains_observations(world):
    conn, clock, cfg, seeds, raw = world
    now = lambda: clock[0]
    calls = []
    net = PoliteNet(transport(calls), conn=conn, clock=now)
    net.start_run(expires_at=now() + 1800)
    michelin.bootstrap(conn, now(), cfg, seeds)
    roster = michelin.Roster(conn, net, raw, cfg, seeds)
    first = drain(roster, now, stop=lambda job: 'filter=3-stars-michelin' in job['target'])
    assert first and all(job['tier'] == 3 for job in first)
    assert queue.cursor(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM global_work WHERE kind='list' AND target LIKE '%page=2'").fetchone()[0] == 1
    net.finish_run()
    resumed = PoliteNet(transport(calls), conn=conn, clock=now)
    resumed.start_run(expires_at=now() + 1800)
    order = drain(michelin.Roster(conn, resumed, raw, cfg, seeds), now)
    assert [job['tier'] for job in order] == sorted((job['tier'] for job in order), reverse=True)
    assert {job['tier'] for job in order} == {1, 2, 3}
    assert queue.cursor(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM global_tier_pass WHERE scope='iceland' AND disposition='no_list'").fetchone()[0] == 3
    assert len([url for url, _ in calls if url.endswith('/peru/restaurants')]) == 1
    assert any('page=2' in url for url, _ in calls)
    assert len([url for url, _ in calls if url == 'https://venue.test/about-chef']) == 1
    assert conn.execute("SELECT COUNT(*) FROM global_work_origin o JOIN global_work w USING (job_key) WHERE w.target='https://venue.test/about-chef' AND w.kind='article'").fetchone()[0] >= 4
    rows = conn.execute("""SELECT b.city,b.country_code,r.name,o.guide_year,o.stars,o.source_kind,o.disposition
        FROM guide_roster_observation o JOIN branch b USING(branch_id) JOIN restaurant r USING(restaurant_id)""").fetchall()
    assert ('Paris', 'FR', 'Le Cèdre', 2026, 2, 'wikipedia', 'discrepancy') in map(tuple, rows)
    assert ('Paris', 'FR', 'Le Cèdre', 2025, 3, 'wikipedia', 'provisional') in map(tuple, rows)
    assert ('Paris', 'FR', 'Le Cèdre', 2026, 3, 'press_original', 'corroborates') in map(tuple, rows)
    assert ('Nice', 'FR', "L'Écorce", 2026, 3, 'press_original', 'provisional') in map(tuple, rows)
    assert ('Seoul', 'KR', '가상식당1', None, 3, 'michelin', 'official') in map(tuple, rows)
    assert {r['city'] for r in rows if r['name'] == 'Le Cèdre'} == {'Paris', 'Lyon'}
    assert len({r['name'] for r in rows if r['source_kind'] == 'michelin'}) == 6
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='guide_list_discrepancy'").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='guide_edition_unknown'").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM global_work WHERE kind='extract' AND reason='extractor_unavailable'").fetchone()[0] > 0
    assert conn.execute('SELECT COUNT(*) FROM employment').fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fetch_record WHERE fetcher_version=? AND extracted_at IS NOT NULL", (michelin.VERSION,)).fetchone()[0] == 0


@pytest.mark.parametrize(('status', 'body'), [
    (403, b'Forbidden'), (429, b'Too many requests'),
    (200, b'<title>Checking your browser</title>'),
    (200, b'<html><title>Sign in</title><form action="/login"><input type="password"></form></html>'),
])
def test_host_block_advances_tiers_without_bypass(world, status, body):
    conn, clock, cfg, seeds, raw = world
    now = lambda: clock[0]
    calls = []
    send = transport(calls)

    def blocked(url, headers, timeout):
        if url == cfg['directory']:
            return send(url, headers, timeout)
        if url.startswith(HOST):
            calls.append((url, headers))
            return http(url, body, status=status)
        return send(url, headers, timeout)

    net = PoliteNet(blocked, conn=conn, clock=now)
    net.start_run(expires_at=now() + 3600)
    michelin.bootstrap(conn, now(), cfg, seeds)
    order = drain(michelin.Roster(conn, net, raw, cfg, seeds), now)
    assert [job['tier'] for job in order] == sorted((job['tier'] for job in order), reverse=True)
    assert queue.cursor(conn) == 0
    assert len([url for url, _ in calls if url.startswith(HOST)]) == 2
    assert any('wikipedia.org' in url for url, _ in calls)
    assert conn.execute("SELECT COUNT(*) FROM global_tier_pass WHERE scope='peru' AND disposition='deferred'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM global_work WHERE reason LIKE 'fail_%'").fetchone()[0] > 1


def test_selection_redirect_stays_on_michelin(world):
    conn, clock, cfg, seeds, raw = world
    calls = []

    def send(url, headers, timeout):
        calls.append(url)
        return http(url, b'', status=302, headers={'Location': 'https://elsewhere.test/restaurants'})

    now = lambda: clock[0]
    net = PoliteNet(send, conn=conn, clock=now)
    net.start_run()
    michelin.bootstrap(conn, now(), cfg, seeds)
    job = queue.claim(conn, now())
    assert michelin.Roster(conn, net, raw, cfg, seeds).work(job) == 'redirect_outside_host'
    assert calls == [cfg['directory']]


def test_lease_expiry_before_and_after_checkpoint(world, monkeypatch):
    conn, clock, cfg, seeds, raw = world
    calls = []
    now = lambda: clock[0]
    net = PoliteNet(transport(calls), conn=conn, clock=now)
    net.start_run(expires_at=now() + 1800)
    cfg['daily_scopes'] = []
    michelin.bootstrap(conn, now(), cfg, seeds)
    previous = queue.claim(conn, now())
    assert not queue.claim(conn, now())
    clock[0] += queue.LEASE_SECONDS + 1
    current = queue.claim(conn, now())
    assert previous['job_key'] == current['job_key']
    assert previous['lease_token'] != current['lease_token']
    assert current['attempts'] == 2
    roster = michelin.Roster(conn, net, raw, cfg, seeds)
    assert roster.work(previous) == 'lease_lost'
    assert not queue.acknowledge(conn, previous, now())
    assert conn.execute('SELECT COUNT(*) FROM fetch_record').fetchone()[0] == 0

    class Interrupted(BaseException):
        pass

    def interrupt(*_, **_kwargs):
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'checkpoint', interrupt)
        with pytest.raises(Interrupted):
            roster.work(current)
    assert conn.execute('SELECT COUNT(*) FROM fetch_record').fetchone()[0] == 0
    assert not conn.execute("SELECT 1 FROM global_scope WHERE scope='france'").fetchone()
    clock[0] += queue.LEASE_SECONDS + 1
    current = queue.claim(conn, now())
    with monkeypatch.context() as patch:
        patch.setattr(queue, 'acknowledge', interrupt)
        with pytest.raises(Interrupted):
            roster.work(current)
    assert conn.execute('SELECT COUNT(*) FROM fetch_record').fetchone()[0] == 1
    assert conn.execute("SELECT 1 FROM global_work WHERE scope='france'").fetchone()
    assert conn.execute('SELECT state FROM global_work WHERE job_key=?', (current['job_key'],)).fetchone()[0] == 'checkpointed'
    clock[0] += queue.LEASE_SECONDS + 1
    successor = queue.claim(conn, now())
    assert successor['job_key'] != current['job_key']
    assert not queue.acknowledge(conn, current, now())
    assert conn.execute('SELECT state FROM global_work WHERE job_key=?', (current['job_key'],)).fetchone()[0] == 'pending'
    assert [url for url, _ in calls] == [cfg['directory'], cfg['directory']]


def test_daily_and_heavy_claim_distinct_work_under_one_host_lease(world):
    conn, clock, cfg, seeds, raw = world
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    now = clock[0]
    with queue.write(conn):
        conn.execute('INSERT INTO global_sweep VALUES (?,3)', (queue.SWEEP,))
        conn.execute("INSERT INTO global_scope VALUES ('korea','Korea',NULL,'ko','[\"KR\"]',NULL,'[]',NULL)")
        for n in range(900):
            queue.enqueue(conn, tier=3, kind='list', scope='korea', target=f'https://busy.test/{n}', now=now, priority=100)
        key = queue.enqueue(conn, tier=3, kind='list', scope='korea', target='https://ready.test/selection', now=now)
        queue.enqueue(conn, tier=2, kind='list', scope='korea', target='https://lower.test/selection', now=now)

    def claim(daily):
        with closing(connect(path)) as worker:
            return queue.claim(worker, now, busy={'busy.test'}, daily=daily)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, (True, False)))
    assert [job['job_key'] for job in results if job] == [key]
    busy_job = queue.claim(conn, now)
    assert busy_job['host'] == 'busy.test'
    assert not queue.claim(conn, now)


def test_weekly_304_and_new_year_leave_previous_awards_intact(world):
    conn, clock, cfg, seeds, raw = world
    calls = []
    now = lambda: clock[0]
    send = transport(calls)
    next_edition = [False]

    def transport_with_edition(url, headers, timeout):
        if next_edition[0] and url == FRANCE:
            calls.append((url, headers))
            text = fixture('france.html').replace('France 2026', 'France 2027')
            text = text.replace('3 Stars MICHELIN', '2 Stars MICHELIN')
            return http(url, text, headers={'ETag': '"fr-new"'})
        return send(url, headers, timeout)

    net = PoliteNet(transport_with_edition, conn=conn, clock=now)
    net.start_run(expires_at=now() + 400_000)
    michelin.bootstrap(conn, now(), cfg, seeds)
    roster = michelin.Roster(conn, net, raw, cfg, seeds)
    drain(roster, now)
    total = conn.execute('SELECT COUNT(*) FROM guide_listing').fetchone()[0]
    snapshot = conn.execute('SELECT fetch_id FROM global_page WHERE url=?', (FRANCE,)).fetchone()[0]
    clock[0] += 86_400
    with conn:
        conn.execute("UPDATE global_work SET available_at=? WHERE state IN ('pending','deferred')", (now() + 700_000,))
        conn.execute("UPDATE global_work SET available_at=? WHERE tier=3 AND target=?", (now(), FRANCE))
    job = queue.claim(conn, now(), daily=True)
    assert roster.work(job) == 'not_modified'
    assert calls[-1][1]['If-None-Match'] == '"fr"'
    assert 'If-Modified-Since' in calls[-1][1]
    assert conn.execute('SELECT fetch_id FROM global_page WHERE url=?', (FRANCE,)).fetchone()[0] == snapshot
    assert conn.execute('SELECT COUNT(*) FROM guide_listing').fetchone()[0] == total

    clock[0] += 86_400
    next_edition[0] = True
    with conn:
        conn.execute("UPDATE global_work SET available_at=? WHERE tier=2 AND target=?", (now(), FRANCE))
    assert roster.work(queue.claim(conn, now(), daily=True)) == 'ok'
    paris = conn.execute("""SELECT DISTINCT guide_year,stars FROM guide_roster_observation o
        JOIN branch b USING(branch_id) JOIN restaurant r USING(restaurant_id)
        WHERE r.name='Le Cèdre' AND b.city='Paris' AND o.source_kind='michelin'""").fetchall()
    assert set(map(tuple, paris)) == {(2026, 3), (2027, 2)}


def test_existing_heavy_windows_and_daily_incremental(world, tmp_path):
    conn, clock, cfg, seeds, raw = world
    calls = []
    now = lambda: clock[0]
    cfg['daily_scopes'] = []
    legacy = heavy.load_cfg()
    legacy.update(search=[], feeds=[], guides=[])
    send = transport(calls)
    stop_on_directory = [True]

    def wire(url, headers, timeout):
        response = send(url, headers, timeout)
        if url == cfg['directory'] and stop_on_directory[0]:
            crawler.terminating = True
        return response.status, response.headers, response.body, response.error

    def make():
        return heavy.HeavyCrawler(str(tmp_path / 'heavy.db'), conn, cfg=legacy, clock=now, transport=wire,
                                  roster_config=cfg, data_dir=str(tmp_path))

    def set_time(hour):
        clock[0] = dt.datetime(2026, 11, 20, hour, tzinfo=heavy.KST).timestamp()

    set_time(0)
    crawler = make()
    report = crawler.run(heavy.deadline_for('02:59', now), 'night')
    assert report['reason'] == 'sigterm'
    assert report['michelin']['jobs'] == 1 and report['michelin']['tier'] == 3
    assert conn.execute("SELECT COUNT(*) FROM global_work WHERE state='leased'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM global_scope WHERE scope<>'worldwide'").fetchone()[0] == 6
    crawler.q.close()

    set_time(7)
    net = PoliteNet(send, conn=conn, clock=now)
    net.start_run()
    bot = cycle.Bot(conn, data_dir=str(tmp_path), net=net, roster_config=cfg, fetch_deadline=time.monotonic() + 180)
    daily_start = len(calls)
    assert cycle.refresh_roster(bot, meter=None)
    assert calls[daily_start][0] == KOREA
    assert not any(url == FRANCE for url, _ in calls[daily_start:])
    net.finish_run()

    set_time(11)
    stop_on_directory[0] = False
    crawler = make()
    report = crawler.run(heavy.deadline_for('13:59', now), 'midday')
    assert report['michelin']['tier'] == 0
    assert report['michelin']['jobs'] > 0
    assert not {'OperationalError', 'TypeError', 'IntegrityError'} & report['michelin']['results'].keys()
    assert any(url == FRANCE for url, _ in calls)
    crawler.q.close()

    set_time(22)
    with conn:
        conn.execute("UPDATE global_work SET available_at=? WHERE state IN ('pending','deferred')", (now() + 700_000,))
        conn.execute('UPDATE global_work SET available_at=? WHERE target=?', (now(), cfg['directory']))
    crawler = make()
    report = crawler.run(heavy.deadline_for('23:59', now), 'evening', optional=True)
    assert report['michelin']['jobs'] == 1 and report['michelin']['results'] == {'not_modified': 1}
    assert crawler.run(heavy.deadline_for('23:59', now), 'evening', optional=True)['reason'] == 'backlog_small'


def test_concurrent_schema_upgrade_applies_each_migration_once(tmp_path):
    path = str(tmp_path / 'schema.db')
    def start(_):
        with closing(connect(path)) as conn:
            return migrate(conn)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(start, range(2)))
    versions = [version for result in results for version in result]
    assert len(versions) == len(set(versions)) == 15
    with closing(connect(path)) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_heavy_extracts_eligible_korean_snapshot_via_shared_adapter(world, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kg_collect.deepseek import Adapters

    conn, clock, cfg, seeds, raw = world
    cycle.init_db(conn, seed=json.loads((FIXTURES.parent / 'seed_minimal.json').read_text()))
    now = lambda: clock[0]
    send = transport([])
    legacy = heavy.load_cfg()
    legacy.update(search=[], feeds=[], guides=[])
    seen = []

    def adapter(meter):
        assert meter.stage == 'extract'
        assert meter.run_id.startswith('hv_')
        client = SimpleNamespace(status='enabled', use_task=lambda task: None,
                                 budget=SimpleNamespace(), last_meta={})
        stats = dict(failed=0, called=0, findings=0)

        def extract(doc, *args, **kwargs):
            from kg_collect.targets import eligibility

            if eligibility(args[1], doc.text)['target_ids']:
                seen.append(('rules', doc.url))
            return []

        def extract_lineage(doc, *args):
            seen.append(('lineage', doc.url))
            return []

        return SimpleNamespace(client=client, stats=stats, extract=extract, extract_lineage=extract_lineage)

    def wire(url, headers, timeout):
        if '/restaurant/ga-sikdang' in url:
            content = '테스트셰프B 셰프는 가상식당1에서 근무했다. 서울 가상동에서 식당을 운영한다. '
            response = http(url, '<article><h1>가상식당1 셰프 인터뷰</h1><p>' + content * 8 + '</p></article>')
        else:
            response = send(url, headers, timeout)
        return response.status, response.headers, response.body, response.error

    monkeypatch.setattr(Adapters, 'for_meter', staticmethod(adapter))
    crawler = heavy.HeavyCrawler(str(tmp_path / 'heavy.db'), conn, cfg=legacy, clock=now, transport=wire,
                                 roster_config=cfg, data_dir=str(tmp_path))
    report = crawler.run(now() + 1800)
    assert report['michelin']['tier'] == 0
    url = HOST + '/kr/en/seoul/restaurant/ga-sikdang'
    assert ('rules', url) in seen and ('lineage', url) in seen
    processed = conn.execute('SELECT url FROM fetch_record WHERE fetcher_version=? AND extracted_at IS NOT NULL',
                             (michelin.VERSION,)).fetchall()
    assert url in [row[0] for row in processed]
    assert not conn.execute("SELECT 1 FROM target WHERE name_ko='Le Cèdre'").fetchone()

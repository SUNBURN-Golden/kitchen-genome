"""G1 regression coverage with temporary state and injected transports."""
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from kg_ai.budget import Budget
from kg_ai.deepseek import Client
from kg_collect import cycle, discover, gold, scoreboard
from kg_collect.deepseek import Adapters
from kg_collect.guard import Guard
from kg_collect.net import PoliteNet, Response
from kg_daemon import db, runner
from test_deepseek_client import meter, response


@pytest.fixture
def conn(tmp_path):
    with db.connect(str(tmp_path / 'service.sqlite')) as conn:
        db.migrate(conn)
        yield conn


def test_web_dispatch_crosses_all_three_former_daily_ceilings(conn):
    hosts = {f'publisher{i}.test' for i in range(205)}
    now = [dt.datetime(2026, 11, 20, 0, tzinfo=dt.timezone.utc).timestamp()]
    calls = []

    def send(url, headers, timeout):
        calls.append(url)
        return Response(url, 200, {}, b'User-agent: *\nAllow: /\n' if url.endswith('/robots.txt') else b'fixture')

    net = PoliteNet(send, lambda seconds: now.__setitem__(0, now[0] + seconds),
                    lambda: now[0], conn=conn, korean_press_hosts=hosts)
    for i, host in enumerate(sorted(hosts)):
        assert net.get(f'https://{host}/venue/{i}').status == 200
    for i in range(310):
        assert net.get(f'https://directory.test/venue/{i}').status == 200
    day = '2026-11-20'
    assert tuple(conn.execute('SELECT attempts,robots FROM web_daily_fetch WHERE day=?', (day,)).fetchone()) == (515, 205)
    assert conn.execute('SELECT COUNT(*) FROM web_daily_url WHERE day=?', (day,)).fetchone()[0] == 515
    assert len(calls) == 720


def test_fetch_guard_stops_one_host_and_keeps_other_work_runnable(conn, tmp_path):
    bad, good = 'broken.test', 'working.test'
    clock = [dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)]
    seen = []

    def send(url, headers, timeout):
        seen.append(url)
        if bad in url:
            return Response(url, 503, {}, b'failure')
        return Response(url, 200, {'content-type': 'text/html'},
                        ('<article>' + 'Fixture chef article. ' * 12 + '</article>').encode())

    bot = cycle.Bot(conn, data_dir=str(tmp_path), clock=lambda: clock[0],
                    net=PoliteNet(send, conn=conn, clock=lambda: clock[0].timestamp()))
    jobs = [f'https://{bad}/p/{i}' for i in range(8)] + [f'https://{good}/p']
    for url in jobs:
        discover.enqueue(conn, path='feed', pool='discovery', url=url)
    cost = meter(conn)
    order = [discover.job_id('feed', url) for url in jobs]
    ctx = runner.StageContext(conn, cost.run_id, 'fetch', {}, cost, {'plan': {'order': order}}, {}, {'bot': bot})
    ctx.save_cursor = lambda value: None
    stats = cycle.stage_fetch(ctx).output
    assert stats['guard_stops'] == {bad: 'identical_failures:http_503'}
    assert seen == jobs[:5] + jobs[-1:]
    lane = conn.execute('SELECT last_committed FROM guard_lane WHERE lane=?', ('collector:fetch:' + good,)).fetchone()
    assert lane[0] == order[-1]

    # New run IDs and a lease takeover preserve the original item's failed attempts.
    for _ in range(2):
        clock[0] += dt.timedelta(minutes=3)
        cost = meter(conn)
        ctx.cost, ctx.run_id = cost, cost.run_id
        ctx.prev['plan']['order'] = order[:1]
        cycle.stage_fetch(ctx)
    row = conn.execute('SELECT state,last_error FROM crawl_job WHERE job_id=?', (order[0],)).fetchone()
    assert tuple(row) == ('failed', 'attempts_exhausted')
    assert conn.execute("SELECT 1 FROM review_queue WHERE subject_id=? AND reason='guard_attempts_exhausted'",
                        (order[0],)).fetchone()


def test_daemon_stalled_cursor_breaks_but_moving_cursor_does_not(conn):
    calls = []

    def handle(ctx):
        n = ctx.cursor.get('last', 0)
        calls.append(n)
        if n < 14:
            return runner.StageResult(cursor={'last': n + 1})
        return runner.StageResult(cursor=ctx.cursor)

    daemon = runner.Daemon(conn, {'test': handle}, stages=('test',), sleep=lambda _: None)
    result = daemon.run()
    assert result['status'] == 'failed'
    lane = conn.execute('SELECT * FROM guard_lane').fetchone()
    assert lane['reason'] == 'no_progress' and lane['last_committed']
    assert calls[-1] == 14 and len(calls) == 23


def test_qwen_only_cycle_reports_enabled_without_missing_key(conn, tmp_path, monkeypatch):
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'kitchens': [], 'restaurants': []}))
    client = Client(Budget(meter(conn)), key='', transport=lambda *args: pytest.fail('primary dispatched'),
                    fallback_key='fixture', fallback_transport=lambda *args: response())
    net = PoliteNet(lambda url, *_: Response(url, 404, {}, b'fixture'), conn=conn)
    bot = cycle.Bot(conn, data_dir=str(tmp_path), seed_path=seed, net=net, deepseek=True,
                    registry_path=tmp_path / 'registry.yaml')
    bot.ai = Adapters(client)
    monkeypatch.setattr(discover, 'directory_and_feed_jobs', lambda _: [])
    result = cycle.run_cycle(bot)
    assert result['status'] == 'succeeded' and result['ai'] == 'enabled'
    assert result.get('ai_reason') != 'missing_key'


def test_closed_kst_summary_counts_first_transitions_and_prices_only_known_usage(conn):
    day = '2026-11-19'
    now = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
    budget = Budget(meter(conn), day=lambda: day)
    assert budget.reserve('qwen', 'extract', 'qwen4-max-preview', 5000, provider='qwen')
    budget.settle('qwen', {'prompt_tokens': 4000, 'completion_tokens': 1000, 'total_tokens': 5000,
                           'prompt_cache_hit_tokens': 2000}, result={})
    budget.cache_hit('qwen')
    scoreboard.record_first(conn, 'resolved_chef', 'chef-1', '2026-11-19T14:59:00Z')
    scoreboard.record_first(conn, 'resolved_chef', 'chef-1', '2026-11-19T22:05:00Z')
    scoreboard.record_first(conn, 'verified_link', 'chef-1:branch-1', '2026-11-19T15:00:00Z')
    data = scoreboard.daily_summary(conn, now=now)
    assert data['day_kst'] == day
    assert data['newly_resolved_chefs'] == 1 and data['verified_career_links'] == 0
    assert data['usd_per_newly_resolved_chef'] == pytest.approx(0.0105)
    assert data['usd_per_verified_career_link'] == 'n/a'
    assert data['models'][0]['cache_hits'] == 1 and data['models'][0]['cache_input_tokens'] == 2000
    assert data['accuracy']['korean_rules']['ko']['precision'] >= 0
    assert data['accuracy']['saved_model'] == 'n/a'
    budget.reserve('lost', 'extract', 'deepseek-chat', 5000, provider='deepseek')
    budget.fail('lost', 'transport_unknown')
    data = scoreboard.daily_summary(conn, now=now)
    assert data['models'][0]['missing_usage'] == 1
    assert data['usd_per_newly_resolved_chef'] == 'unknown'


def test_gold_scores_saved_model_output_and_prefilter_misses(conn, tmp_path):
    conn.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetched_at,fetcher_version,extractor_version)
        VALUES ('f','https://fixture.test/p','https://fixture.test/p','fixture.test','html','ok','2026-11-19','fixture','rules')""")
    conn.execute("""INSERT INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,chef_text,org_text,
          span_start,span_end,quote,extractor,extractor_version,created_at,asserted_by,pending_only)
        VALUES ('e','f','https://fixture.test/p','fixture.test','career','inference','Alix','La Table',0,17,
          'Alix at La Table.', 'deepseek:web','web-extract-2','2026-11-19','ai_inference',1)""")
    label = {'label': 'career', 'chef': 'Alix', 'org': 'La Table'}
    spec = {'version': 'fixture-1', 'articles': [
        {'language': 'en', 'fetch_id': 'f', 'text': 'Alix at La Table.', 'expect': [label]},
        {'language': 'fr', 'fetch_id': 'miss', 'text': 'Alix travaillait à La Table.',
         'expect': [label], 'prefilter_miss': True}]}
    path = tmp_path / 'heldout.json'
    path.write_text(json.dumps(spec))
    data = gold.daily_accuracy(conn, None, path)['saved_model']
    assert data['version'] == 'fixture-1' and data['overall']['samples'] == 2
    assert data['overall']['precision'] == 1 and data['overall']['recall'] == .5
    assert data['en']['quote_fidelity'] == 1 and data['fr']['recall'] == 0


def test_global_migration_preserves_pr_a_ids_and_guide_observations(tmp_path):
    legacy = tmp_path / 'migrations'
    legacy.mkdir()
    for path in Path(db.SCHEMA_DIR).glob('*.sql'):
        if path.name < '0012':
            shutil.copy(path, legacy / path.name)
    with db.connect(str(tmp_path / 'legacy.sqlite')) as conn:
        db.migrate(conn, str(legacy))
        conn.execute("INSERT INTO restaurant(restaurant_id,name,created_at) VALUES ('r','Fixture venue','t')")
        conn.execute("INSERT INTO branch(branch_id,restaurant_id,city,created_at) VALUES ('b','r','서울','t'),('u','r','Unknown','t')")
        conn.commit()
        assert db.migrate(conn) == ['0012_web_access_guard.sql', '0013_global_ontology.sql', '0014_global_queue.sql']
        assert dict(conn.execute('SELECT branch_id,country_code FROM branch')) == {'b': 'KR', 'u': None}
        conn.execute("INSERT INTO guide_selection VALUES ('s','fixture','KR','live',NULL,'2026-11-19')")
        listing = "INSERT OR IGNORE INTO guide_listing VALUES ('s','b',?,'publisher','https://guide.test/kr',NULL,'list:row',?,?)"
        for stars, at, fingerprint in [(1, '2026-11-19', 'a'), (1, '2026-11-19', 'a'), (3, '2026-11-20', 'b')]:
            conn.execute(listing, (stars, at, fingerprint))
        assert conn.execute('SELECT COUNT(*) FROM guide_listing').fetchone()[0] == 2
        assert tuple(conn.execute('SELECT stars,tier FROM current_guide_listing').fetchone()) == (3, 3)
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute('UPDATE guide_listing SET stars=2')


def test_guard_items_survive_reopen_and_success_resets_only_that_item(conn):
    guard = Guard(conn, 'run-1', 'fixture')
    assert guard.attempt('doc:v2')[0] == 1
    conn.commit()
    other = Guard(conn, 'run-2', 'fixture')
    assert other.attempt('doc:v2')[0] == 2
    assert other.attempt('doc:v2')[0] == 3
    assert other.attempt('doc:v2') is None
    assert other.item('doc:v2')['reason'] == 'attempts_exhausted'
    assert other.attempt('doc:v3')[0] == 1
    other.complete('doc:v3')
    assert other.item('doc:v3') is None and other.item('doc:v2')['attempts'] == 3

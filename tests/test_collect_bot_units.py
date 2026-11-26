"""Active-bot unit tests: discovery jobs, scheduler, discovered-registry validation (offline)."""
import datetime as dt
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from collect_world import SEED   # noqa: E402
from kg_collect import cycle, discover, scheduler   # noqa: E402
from kg_daemon import db as kgdb   # noqa: E402


@pytest.fixture()
def conn():
    c = kgdb.connect(':memory:')
    cycle.init_db(c, seed=SEED)
    return c


# ── discover / scheduler ──
def test_enqueue_deduplicates_unseen_urls_at_any_depth(conn):
    for i in range(525):
        url = f'https://fixture.test/route/{i}'
        assert discover.enqueue(conn, path='feed', pool='discovery', url=url, depth=i)
        assert not discover.enqueue(conn, path='feed', pool='discovery', url=url + '/#ref', depth=i)
    assert conn.execute("SELECT COUNT(*) FROM crawl_job").fetchone()[0] == 525


def test_keyword_jobs_combine_keyword_region_year(conn):
    seeds = discover.load_seeds()
    qs = [j['query'] for j in discover.keyword_jobs(conn, seeds, today=dt.date(2026, 10, 27))]
    assert '성수 폐업 셰프 2026' in qs and '한남 오픈 셰프 2025' in qs


def test_discovery_rotates_with_a_large_backfill_backlog(conn):
    for i in range(500):
        discover.enqueue(conn, path='feed', pool='backfill', url=f'https://fixture.test/backfill/{i}')
    discover.enqueue(conn, path='directory', pool='discovery', url='https://directory.test/list')
    plan = scheduler.plan(conn)
    assert len(plan['order']) == 501
    assert plan['chosen']['discovery'][0] in plan['order'][:2]


def test_priority_penalises_failures_and_known_duplicates(conn):
    caps = {}
    discover.enqueue(conn, path='feed', pool='discovery', url='https://fresh.test/a', relevance=0.8, caps=caps)
    discover.enqueue(conn, path='feed', pool='discovery', url='https://flaky.test/a', relevance=0.8, caps=caps)
    conn.execute("UPDATE crawl_job SET attempts=2 WHERE url='https://flaky.test/a'")
    now = dt.datetime.now(dt.timezone.utc)
    p = {r['url']: scheduler.job_priority(conn, r, now)[0] for r in conn.execute('SELECT * FROM crawl_job')}
    assert p['https://fresh.test/a'] > p['https://flaky.test/a']


def test_discovered_registry_validates_against_the_registry_validator(tmp_path):
    from kg_collect import evaluate
    from kg_daemon.registry import load_registry, validate_registry
    c = kgdb.connect(':memory:')
    cycle.init_db(c, seed=SEED)
    evaluate.ensure_profile(c, 'new.test')
    c.execute("UPDATE domain_profile SET state='promoted', tier_guess='press', score=0.7 WHERE host='new.test'")
    p = str(tmp_path / 'r.yaml')
    assert evaluate.grow_registry(c, p) == ['disc_new_test']
    merged = load_registry()                                 # curated registry (valid on its own) + the machine-written one must validate TOGETHER
    merged['sources'].update(yaml.safe_load(open(p, encoding='utf-8'))['sources'])
    validate_registry(merged)                                # raises RegistryError if any limit/permission field is missing or unsafe
    assert 'disc_new_test' in merged['sources']


def test_directory_links_skip_wiki_chrome(conn):
    from kg_collect.fetch import Doc
    seeds = discover.load_seeds()
    caps = seeds['caps']
    doc = Doc(fetch_id='f', url='https://ko.wikipedia.org/wiki/x', canonical_url='https://ko.wikipedia.org/wiki/x', host='ko.wikipedia.org', method='html', outcome='ok', text='x' * 400, links=[
        ('https://de.wikipedia.org/wiki/Koch', 'de', ''), ('https://donate.wikimedia.org/', 'donate', ''),
        ('https://news.example-press.test/a/1', 'article', '')])
    parent = {'job_id': 'job_p', 'depth': 0, 'target_id': None}
    conn.execute("INSERT INTO crawl_job(job_id,path,pool,state,created_at) VALUES('job_p','directory','discovery','done','2026-01-01T00:00:00Z')")
    n = discover.directory_link_jobs(conn, doc, parent, seeds, caps)
    urls = [r['url'] for r in conn.execute("SELECT url FROM crawl_job WHERE parent_job_id='job_p'")]
    assert n == 1 and urls == ['https://news.example-press.test/a/1']


def test_candidate_review_drops_acronyms_and_institutions_and_queues_the_rest(conn):
    from kg_collect import review
    from kg_collect.targets import upsert_target
    assert review.reject_reason('restaurant', 'CIA') == 'acronym_not_a_venue'
    assert review.reject_reason('restaurant', 'C.I.A.') == 'acronym_not_a_venue'
    assert review.reject_reason('restaurant', '르 꼬르동 블루') == 'institution_not_a_venue'
    assert review.reject_reason('restaurant', '세옥') is None and review.reject_reason('chef', '여경옥') is None
    a = upsert_target(conn, 'restaurant', 'CIA', origin='discovered', via='t', status='candidate', attrs={})
    b = upsert_target(conn, 'chef', '여경옥', origin='discovered', via='t', status='candidate', attrs={})
    out = review.review_candidates(conn)
    st = {r['target_id']: r['status'] for r in conn.execute('SELECT target_id,status FROM target WHERE target_id IN (?,?)', (a, b))}
    assert st[a] == 'rejected' and st[b] == 'candidate'
    assert [p['name'] for p in out['queued_for_review']] == ['여경옥']
    assert review.review_candidates(conn)['rejected'] == []                     # idempotent


def test_plan_fetches_a_url_only_once_even_if_queued_in_two_pools(conn):
    caps = {'max_depth': 3, 'relevance_floor': 0.0}
    u = 'https://www.bing.com/news/search?format=rss&setlang=ko&q=%ED%95%80%EC%A6%88%20%ED%8F%90%EC%97%85'
    assert discover.enqueue(conn, path='keyword_search', pool='refresh', url=u, query='핀즈 폐업', relevance=0.7, caps=caps)
    assert discover.enqueue(conn, path='reverse_gap', pool='backfill', url=u, query='핀즈 폐업', relevance=0.7, caps=caps)
    conn.commit()
    pl = scheduler.plan(conn, dt.datetime.now(dt.timezone.utc))
    chosen = [j for p in pl['chosen'].values() for j in p]
    urls = [conn.execute('SELECT url FROM crawl_job WHERE job_id=?', (j,)).fetchone()['url'] for j in chosen]
    assert urls.count(u) == 1
    assert pl['skipped_same_url'] >= 1


def test_evaluate_skips_unchanged_hosts_until_stale(conn):
    """A host with no new fetches since its last decision is not re-scored (no robots.txt re-read) until the decision is a week old."""
    from kg_collect import evaluate
    from kg_collect.targets import Gazetteer
    calls = []

    class Net:
        def robots_info(self, url):
            calls.append(url)
            return {'allowed': True, 'crawl_delay': None}

    class Raw:
        def read_text(self, ref):
            return ''

    def add_fetch(fid, when):
        conn.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetcher_version,extractor_version,fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (fid, f'https://same.test/{fid}', f'https://same.test/{fid}', 'same.test', 'html', 'fail_empty', 't', 't', when))

    add_fetch('f1', '2026-11-01T00:00:00Z')
    g = Gazetteer(conn)
    evaluate.score_host(conn, 'same.test', Net(), Raw(), g, set(), now='2026-11-01T01:00:00Z')
    assert len(calls) == 1
    evaluate.score_host(conn, 'same.test', Net(), Raw(), g, set(), now='2026-11-02T01:00:00Z')
    assert len(calls) == 1                                   # unchanged and fresh: skipped
    add_fetch('f2', '2026-11-02T02:00:00Z')
    evaluate.score_host(conn, 'same.test', Net(), Raw(), g, set(), now='2026-11-02T03:00:00Z')
    assert len(calls) == 2                                   # new trial fetch: re-scored
    assert conn.execute("SELECT trial_fetches FROM domain_profile WHERE host='same.test'").fetchone()['trial_fetches'] == 2
    evaluate.score_host(conn, 'same.test', Net(), Raw(), g, set(), now='2026-11-09T03:00:00Z')
    assert len(calls) == 3                                   # a week old: re-scored even without new fetches


def _host_fetches(conn, host, n, outcome='ok'):
    for i in range(n):
        conn.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_ref,fetcher_version,extractor_version,fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""", (f'{host}-{i}', f'https://{host}/{i}', f'https://{host}/{i}', host, 'html', outcome,
                                                          f'{host}-{i}', 't', 't', f'2026-11-25T00:00:{i:02d}Z'))


class _Raw:
    def __init__(self):
        self.reads = []

    def read_text(self, ref):
        self.reads.append(ref)
        return f'셰프 레스토랑 오픈 {ref} ' * 40


def test_repost_refs_are_read_once_per_stage_not_per_document(conn):
    """Re-scoring a host with many documents must not re-read the 400 reference documents for every document."""
    from kg_collect import evaluate
    from kg_collect.targets import Gazetteer

    class Net:
        def robots_info(self, url):
            return {'allowed': True, 'crawl_delay': None}

    _host_fetches(conn, 'big.test', 30)
    _host_fetches(conn, 'other.test', 10)
    raw, g = _Raw(), Gazetteer(conn)
    refs = evaluate.RepostRefs(conn, raw)
    evaluate.score_host(conn, 'big.test', Net(), raw, g, set(), now='2026-11-26T00:00:00Z', refs=refs)
    evaluate.score_host(conn, 'other.test', Net(), raw, g, set(), now='2026-11-26T00:00:00Z', refs=refs)
    # 40 own documents + each reference document shingled once (shared across hosts)
    assert len(raw.reads) == 40 + 40
    # same answer as the uncached path
    text = raw.read_text('other.test-1')
    assert evaluate.is_repost(conn, 'big.test', text, raw) == evaluate.is_repost(conn, 'big.test', text, raw, refs=refs.for_host('big.test'))


def test_unread_robots_defers_host_instead_of_deprioritizing(conn):
    """A robots.txt not read because the run deadline passed is unknown, not a disallow (2026-11-24: 175 press hosts deprioritized)."""
    from kg_collect import evaluate
    from kg_collect.targets import Gazetteer

    class Net:
        def robots_info(self, url):
            return {'allowed': False, 'crawl_delay': None, 'unknown': True}

    _host_fetches(conn, 'press.test', 4)
    out = evaluate.score_host(conn, 'press.test', Net(), _Raw(), Gazetteer(conn), set(), now='2026-11-26T00:00:00Z')
    assert out.get('deferred')
    row = conn.execute("SELECT state,evaluated_at FROM domain_profile WHERE host='press.test'").fetchone()
    assert row['state'] != 'deprioritized' and row['evaluated_at'] is None


def test_robots_info_marks_block_end_unknown(conn):
    from kg_collect.net import PoliteNet
    net = PoliteNet(conn=conn, transport=lambda *a, **k: (_ for _ in ()).throw(AssertionError('no dispatch past deadline')),
                    korean_press_hosts=('press.test',), deadline=0)
    info = net.robots_info('https://press.test/')
    assert info['allowed'] is False and info.get('unknown') is True


def test_evaluate_stage_stops_at_deadline_and_leaves_hosts_due(conn, tmp_path):
    import time
    _host_fetches(conn, 'late.test', 3)
    conn.execute("UPDATE fetch_record SET extracted_at='2026-11-25T00:00:00Z'")
    bot = cycle.Bot(conn, data_dir=str(tmp_path), registry_path=str(tmp_path / 'reg.yaml'), deadline=time.monotonic() - 1)

    class Ctx:
        env = {'bot': bot}
    res = cycle.stage_evaluate(Ctx())
    out = res.output
    assert out['evaluated'] == 0 and out['deferred_hosts'] >= 1
    assert conn.execute("SELECT evaluated_at FROM domain_profile WHERE host='late.test'").fetchone() is None


def test_collect_windows_reserves_360s_on_default_wall():
    from kg_collect.cli import DEFAULT_COLLECT_TIMEOUT, EVALUATE_RESERVE_SEC, collect_windows
    assert DEFAULT_COLLECT_TIMEOUT == 1680 and EVALUATE_RESERVE_SEC == 360
    work, extract = collect_windows(DEFAULT_COLLECT_TIMEOUT)
    assert work == 1560 and extract == 1200 and (work - extract) == 360
    # 120s remains after the work window before the shell timeout, so scoring cannot cause exit 124.
    assert DEFAULT_COLLECT_TIMEOUT - work == 120


def test_collect_windows_scales_reserve_down_on_a_short_wall():
    from kg_collect.cli import collect_windows
    work, extract = collect_windows(30)
    assert work == 24 and extract == 18 and (work - extract) == 6


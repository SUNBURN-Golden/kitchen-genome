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
def test_enqueue_is_idempotent_and_capped(conn):
    caps = {'max_depth': 1, 'relevance_floor': 0.3, 'max_queued_per_pool': {'discovery': 2}}
    assert discover.enqueue(conn, path='feed', pool='discovery', url='https://a.test/1', relevance=0.9, caps=caps)
    assert not discover.enqueue(conn, path='feed', pool='discovery', url='https://a.test/1', relevance=0.9, caps=caps)       # same job id
    assert not discover.enqueue(conn, path='feed', pool='discovery', url='https://a.test/2', depth=2, relevance=0.9, caps=caps)   # depth cap
    assert not discover.enqueue(conn, path='feed', pool='discovery', url='https://a.test/3', relevance=0.1, caps=caps)             # relevance floor
    assert discover.enqueue(conn, path='feed', pool='discovery', url='https://a.test/4', relevance=0.9, caps=caps)
    assert not discover.enqueue(conn, path='feed', pool='discovery', url='https://a.test/5', relevance=0.9, caps=caps)             # queue cap
    assert discover.enqueue(conn, path='feed', pool='discovery', url='https://www.instagram.com/x', relevance=0.9, caps={}) and \
        conn.execute("SELECT state FROM crawl_job WHERE url='https://www.instagram.com/x'").fetchone()[0] == 'skipped'


def test_keyword_jobs_combine_keyword_region_year(conn):
    seeds = discover.load_seeds()
    qs = [j['query'] for j in discover.keyword_jobs(conn, seeds, today=dt.date(2026, 10, 27))]
    assert '성수 폐업 셰프 2026' in qs and '한남 오픈 셰프 2025' in qs


def test_discovery_budget_is_reserved_against_a_huge_backfill_backlog(conn):
    caps = {'max_queued_per_pool': {'backfill': 9999, 'discovery': 9999, 'refresh': 9999}}
    for i in range(300):
        discover.enqueue(conn, path='feed', pool='backfill', url=f'https://old{i % 7}.test/{i}', relevance=0.95, caps=caps)
    for i in range(30):
        discover.enqueue(conn, path='feed', pool='discovery', url=f'https://new{i}.test/{i}', relevance=0.5, caps=caps)
    pl = scheduler.plan(conn, 100, dt.datetime.now(dt.timezone.utc))
    assert len(pl['chosen']['discovery']) >= 30 * 1 and pl['budgets']['discovery'] >= 30


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


def test_zero_unit_plan_is_empty_not_a_crash(conn):
    pl = scheduler.plan(conn, 0)
    assert all(not v for v in pl['chosen'].values()) if isinstance(pl.get('chosen'), dict) else True

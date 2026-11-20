"""Publication acceptance against migrated SQLite, publisher text, and a throwaway site."""
import datetime as dt
import html
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon'))

from kg_ai import review
from kg_collect import EXTRACTOR_VERSION, cycle, official, site_publish
from kg_collect.extract import publication_assertions
from kg_collect.htmlx import parse_html
from kg_collect.rawstore import RawStore
from kg_collect.targets import Gazetteer, load_seed_targets, upsert_target
from kg_daemon.db import connect, migrate

NOW = '2026-11-20T14:59:30Z'
NEXT_DAY = '2026-11-20T15:01:00Z'
ARTICLE_DATE = '2026-11-01'
OLDER = '2022-01-01'
PRESS = ('foodnews.news', 'hankyung.com', 'newsis.com')
FIRST = '미토우 출신 김도훈 셰프가 서울에서 레스토랑 ‘오닐’을 열었다.'
SECOND = '김도훈 셰프는 미토우에서 수셰프로 일했다. 김도훈 셰프는 레스토랑 ‘오닐’의 헤드 셰프이다.'
SEED = {'generated': '2026-10-20', 'sources': {}, 'kitchens': [
    {'id': 'mitou', 'name': '미토우', 'en': 'Mitou'}, {'id': 'layeon', 'name': '라연', 'en': 'La Yeon'}],
    'restaurants': [{'id': 'existing', 'type': 'established', 'name': '기존식당', 'chef': '최가람', 'name_en': 'Existing',
                     'kitchen': None, 'city': '서울', 'verification_status': 'confirmed'}]}


@pytest.fixture
def publication_world(tmp_path, monkeypatch):
    db_path = tmp_path / 'collector.sqlite'
    monkeypatch.setenv('KG_SERVICE_DB', str(db_path))
    monkeypatch.setenv('KG_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('KG_DAILY_LOCK_FD', raising=False)
    c = connect(str(db_path))
    migrate(c)
    raw = RawStore(tmp_path / 'raw')
    repo = tmp_path / 'site'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    for folder in ('data', 'scripts', 'crawler', 'web'):
        (repo / folder).mkdir()
    shutil.copytree(ROOT / 'scripts' / 'templates', repo / 'scripts' / 'templates')
    for filename in ('data/rr.py', 'data/enrich_en.py', 'scripts/build_pages.py', 'scripts/build_pages_en.py',
                     'scripts/lineage_evidence.py', 'scripts/en_terms.py', 'scripts/build_feeds.py', 'web/app.js'):
        shutil.copyfile(ROOT / filename, repo / filename)
    seed_path = repo / 'data' / 'seed.json'
    seed_path.write_text(json.dumps(SEED, ensure_ascii=False), encoding='utf-8')
    seed = json.loads(seed_path.read_text())
    load_seed_targets(c, seed)
    monkeypatch.setattr(cycle, 'ROOT', str(repo))
    monkeypatch.setattr(cycle, 'utcnow', lambda: NOW)
    monkeypatch.setattr(review, 'utcnow', lambda: NOW)
    real_instant = site_publish.instant
    monkeypatch.setattr(site_publish, 'instant', lambda now=None: real_instant(now or NOW))
    return SimpleNamespace(conn=c, raw=raw, repo=repo, seed=seed, seed_path=seed_path, db_path=db_path,
                           log=repo / 'data' / 'ai_review_log.jsonl')


def article(world, host, body, *, slug=None, date=ARTICLE_DATE, method='html', visible=None, rules_version='rules/1.8'):
    c, raw = world.conn, world.raw
    idx = c.execute('SELECT COUNT(*) FROM fetch_record').fetchone()[0]
    url = f'https://{host}/food/{slug or idx}?a=1&b=2'
    fetch_id = f'f{idx}'
    paragraphs = ''.join(f'<p>{html.escape(s)}</p>' for s in (visible if visible is not None else body).splitlines())
    saved = f'<html><body><article>{paragraphs}</article></body></html>'
    raw_ref, content_hash = raw.put(host, saved.encode(), 'html')
    text = body if visible is not None else parse_html(saved, url)['text']
    text_ref = raw.put_text(host, text)
    c.execute("""INSERT INTO fetch_record
      (fetch_id,url,canonical_url,host,method,http_status,outcome,content_hash,raw_ref,text_ref,text_chars,
       fetcher_version,extractor_version,published_at,fetched_at,extracted_at)
      VALUES (?,?,?,?,?,200,'ok',?,?,?,?,?,?,?,?,?)""",
              (fetch_id, url, site_publish.canonicalize_url(url), site_publish.host_of(url), method, content_hash, raw_ref,
               text_ref, len(text), 'test', rules_version, date, NOW, NOW))
    c.commit()
    return fetch_id


def process(world):
    bot = cycle.Bot(world.conn, data_dir=str(world.raw.root.parent), today=dt.date(2026, 11, 20))
    ctx = SimpleNamespace(env={'bot': bot})
    extraction = cycle.stage_extract(ctx).output
    return extraction, cycle.stage_gaps(ctx).output['publication']


def app(world, monkeypatch):
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('legacy_apply_fixture', ROOT / 'crawler/apply.py')
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'H', str(world.repo / 'crawler'))
    monkeypatch.setattr(module, 'ROOT', str(world.repo))
    monkeypatch.setattr(module, 'SEED', str(world.seed_path))
    return module


def approved_reviews(world):
    return [dict(r) for r in world.conn.execute("SELECT * FROM review_queue WHERE reason='entity_publication' AND state='resolved'")]


def bundled_seeds(world):
    return [r for r in json.loads(world.seed_path.read_text())['restaurants'] if r.get('publication')]


def run_build(world):
    for script in ('data/enrich_en.py', 'scripts/build_pages.py', 'scripts/build_pages_en.py', 'scripts/build_feeds.py'):
        subprocess.run([sys.executable, script], cwd=world.repo, check=True, capture_output=True)


def test_extract_relabel_apply_and_render_two_reports(publication_world, monkeypatch):
    w = publication_world
    first = FIRST[:-1] + ' (& <제철>).'
    article(w, PRESS[0], first, date=ARTICLE_DATE)
    article(w, PRESS[1], SECOND, date='2026-10-17')
    assert not list(w.conn.execute('SELECT * FROM extraction'))
    extraction, stats = process(w)
    assert extraction['docs'] == 2
    assert {r[0] for r in w.conn.execute('SELECT extractor_version FROM extraction')} == {EXTRACTOR_VERSION}
    assert stats['verified'] == 1
    app(w, monkeypatch).main()
    row, = bundled_seeds(w)
    assert row['chef'] == '김도훈' and row['name'] == '오닐' and row['kitchen'] == 'mitou'
    assert row['checked'] == '2026-11-20' and row['lineage_status'] == 'verified'
    assert row['role'] == '출신' and row['price'] is None and row['opened'] == '미확인'
    assert row['lineage_evidence'][0]['quote'] == first
    assert not row.get('career_checking')
    assert json.loads(w.seed_path.read_text())['restaurants'][0] == SEED['restaurants'][0]
    run_build(w)
    for lang in ('', 'en'):
        directory = w.repo / 'web' / lang
        page = (directory / 'r' / row['id'] / 'index.html').read_text()
        assert html.escape(first) in page and 'a=1&amp;b=2' in page
        assert '"alumniOf"' in page and '"worksFor"' in page
        assert row['id'] in (directory / 'directory' / 'index.html').read_text()
        assert row['id'] in (directory / 'k' / 'mitou' / 'index.html').read_text()
    assert row['id'] in (w.repo / 'web/sitemap.xml').read_text()
    data_js = (w.repo / 'web/data.js').read_text()
    assert 'lineage_evidence' in data_js and 'publication' not in data_js
    assert row['publication']['row_digest'] == site_publish.row_digest(bundled_seeds(w)[0])
    js = (w.repo / 'web/app.js').read_text()
    assert 'const SHOW_VERDICTS=false;' in js and 'const SHOW_NEWSLETTER=false;' in js
    if shutil.which('node'):
        # Exercise the SPA's real display helpers without a browser or any requests.
        functions = js.split('const news=')[0]
        check = '''const row=window.SEED.restaurants.find(x=>x.id.startsWith("alumni-"));
        if(!lineageEvidence(row).includes("&lt;제철&gt;") || !badge(row).includes("출처 확인")) process.exit(1);
        if(lineageEvidence({lineage_evidence:[{src:"https://user@host.test/#f",quote:"fake"}]})) process.exit(1);'''
        subprocess.run(['node', '-e', 'global.window={};\n' + data_js + '\n' + functions + '\n' + check,], check=True)


def test_first_party_must_own_subject_and_can_verify_undated_profile(publication_world, monkeypatch):
    w = publication_world
    host = 'oneil.test'
    dest = upsert_target(w.conn, 'restaurant', '오닐', origin='discovered', status='candidate')
    body = '김도훈 셰프는 미토우에서 수셰프로 일했다. 김도훈 셰프는 레스토랑 ‘오닐’의 헤드 셰프이다.\n주소: 서울특별시 용산구 한남로 12'
    article(w, host, body, date=None)
    official.register_site(w.conn, dest, f'https://{host}/', 'probe_verified', True, 'name match', NOW)
    _, stats = process(w)
    assert stats['verified'] == 0
    official.register_site(w.conn, dest, f'https://{host}/about', 'article_link', True, 'publisher linked ownership', NOW)
    w.conn.commit()
    _, stats = process(w)
    assert stats['verified'] == 1
    app(w, monkeypatch).main()
    row, = bundled_seeds(w)
    assert row['lineage_status'] == 'verified' and row['role'] == '수셰프'
    assert row['lineage_src'].startswith(f'https://{host}/')
    assert '주소: 서울특별시 용산구 한남로 12' in {e['quote'] for e in row['lineage_evidence']}


@pytest.mark.parametrize('kind', ['syndicated', 'same_group', 'separate_edges', 'bad_hosts', 'headline',
                                  'synthetic', 'fabricated', 'model', 'junk', 'ambiguous', 'alias',
                                  'no_city', 'conflict', 'closure', 'stale', 'guess'])
def test_unqualified_sources_cannot_create_verified_row(publication_world, monkeypatch, kind):
    w = publication_world
    if kind == 'syndicated':
        article(w, PRESS[0], FIRST)
        article(w, PRESS[1], FIRST.replace('열었다', '오픈했다'))
    elif kind == 'same_group':
        article(w, 'foodnews.news', FIRST)
        article(w, 'mobile.foodnews.news', SECOND)
    elif kind == 'separate_edges':
        article(w, PRESS[0], '미토우 출신 김도훈 셰프.')
        article(w, PRESS[1], FIRST.replace('미토우 출신 ', ''))
    elif kind == 'bad_hosts':
        for host in ('xportsnews.com', 'sports.khan.co.kr', 'sports.donga.com', 'celebinfluencernews.com',
                     'wikitree.co.kr', 'bing.com', 'naver.com', 'www.yna.co.kr', 'cjnews.cj.net', 'unknown.test'):
            article(w, host, FIRST)
        w.conn.execute("INSERT INTO web_host_guard(host,paused) VALUES ('newsis.com','blocked:403')")
        article(w, PRESS[2], FIRST)
    elif kind == 'headline':
        article(w, PRESS[0], FIRST, method='feed', visible='')
    elif kind == 'synthetic':
        article(w, PRESS[0], FIRST, visible='전혀 다른 본문.')
    elif kind in ('fabricated', 'model'):
        article(w, PRESS[0], FIRST)
        process(w)
        if kind == 'fabricated':
            w.conn.execute("UPDATE extraction SET quote='김도훈 셰프는 미토우에서 수셰프로 일했다.'")
        else:
            w.conn.execute("UPDATE extraction SET asserted_by='ai_inference',pending_only=1")
        w.conn.commit()
    elif kind == 'junk':
        for host in PRESS[:2]:
            article(w, host, FIRST.replace('김도훈', '최근'))
    elif kind == 'ambiguous':
        for name in ('홍도형', '고주현'):
            upsert_target(w.conn, 'chef', name, origin='discovered', aliases=('김도훈',))
        article(w, PRESS[0], FIRST)
    elif kind == 'alias':
        w.seed['restaurants'][0]['prev_names'] = ['오닐']
        w.seed_path.write_text(json.dumps(w.seed, ensure_ascii=False))
        load_seed_targets(w.conn, w.seed)
        article(w, PRESS[0], FIRST)
        article(w, PRESS[1], SECOND)
    elif kind == 'no_city':
        article(w, PRESS[0], FIRST.replace('서울에서 ', ''))
        article(w, PRESS[1], SECOND)
    elif kind == 'conflict':
        article(w, PRESS[0], FIRST, date='2025-01-01')
        article(w, PRESS[1], SECOND, date='2025-01-03')
        article(w, PRESS[2], '김도훈 셰프는 레스토랑 ‘새미루’의 오너 셰프이다.', date=ARTICLE_DATE)
    elif kind == 'closure':
        article(w, PRESS[0], FIRST)
        article(w, PRESS[1], SECOND)
        article(w, PRESS[2], '레스토랑 ‘오닐’은 폐업했다.')
    elif kind == 'stale':
        article(w, PRESS[0], FIRST, date=OLDER)
        article(w, PRESS[1], SECOND, date=OLDER)
    elif kind == 'guess':
        dest = upsert_target(w.conn, 'restaurant', '오닐', origin='discovered', status='candidate')
        official.register_site(w.conn, dest, 'https://unconfirmed.test/', 'probe_verified', True, 'guess', NOW)
        article(w, 'unconfirmed.test', FIRST)
    _, stats = process(w)
    assert stats['verified'] == 0
    app(w, monkeypatch).main()
    assert not bundled_seeds(w)
    if kind in ('syndicated', 'same_group', 'separate_edges'):
        assert stats['checking'] == 1
    if kind == 'no_city':
        assert stats['missing_location'] == 1 and stats['gap_jobs'] >= 1
    if kind == 'closure':
        assert stats['conflict'] == 1


def test_adjacent_past_guest_and_planned_clauses_do_not_bind_affiliation(publication_world):
    g = Gazetteer(publication_world.conn)
    quotes = (
        '미토우 출신 김도훈 셰프와 레스토랑 ‘오닐’이 함께 일했다.',
        '미토우 출신 김도훈 셰프가 레스토랑 ‘오닐’에서 게스트 디너를 열었다.',
        '미토우 출신 김도훈 셰프가 레스토랑 ‘오닐’을 열 계획이다.',
        '김도훈 셰프는 레스토랑 ‘오닐’의 헤드 셰프였다.',
        '미토우 출신 김도훈 셰프가 서울에서 팝업 레스토랑 ‘오닐’을 열었다.',
        '미토우 출신 김도훈 셰프 옆에 레스토랑 ‘오닐’이 보인다.',
    )
    for quote in quotes:
        assert all(a['label'] == 'career' for a in publication_assertions(quote, g)), quote


def test_single_source_approval_upgrade_and_rollback(publication_world, monkeypatch):
    from test_deepseek_client import meter, response
    from tests_balance_fake import healthy_balance
    from kg_ai.budget import Budget
    from kg_ai.deepseek import Client

    w = publication_world
    article(w, PRESS[0], '김도훈 셰프는 미토우에서 수셰프로 일했다. ' + FIRST.replace('미토우 출신 ', ''))
    assert process(w)[1]['checking'] == 1
    entry = app(w, monkeypatch)
    entry.main()
    assert not bundled_seeds(w)
    disabled = Client(Budget(meter(w.conn)), key='', fallback_key='')
    assert review.review(w.conn, w.seed_path, w.log, w.raw.root, client=disabled)['reason'] == 'missing_key'
    assert review.review(w.conn, w.seed_path, w.log, w.raw.root, client=disabled, daily_cap=0)['bundle_remaining'] == 1
    visits = []

    def transport(payload, _key):
        prompt = json.loads(json.loads(payload)['messages'][1]['content'])
        visits.append(prompt)
        return response({'reviews': [{'id': prompt['id'], 'decision': 'approve', 'reason': 'Both edges and city are stated'}]})

    client = Client(Budget(meter(w.conn)), key='fixture-value', fallback_key='', transport=transport,
                    balance_transport=healthy_balance)
    assert review.review(w.conn, w.seed_path, w.log, w.raw.root, client=client)['bundle_approved'] == 1
    assert visits[0]['proposed']['name'] == '오닐'
    assert not json.loads(w.seed_path.read_text()).get('ai_reviewed_unlinked')
    assert json.loads(w.log.read_text())['action'] == 'publication_approve'
    entry.main()
    row, = bundled_seeds(w)
    rid = row['id']
    assert row['lineage_status'] == 'unverified' and row['career_checking'] == ['kitchen', 'role']
    run_build(w)
    for prefix, badge in (('', '확인 중'), ('en/', 'Checking')):
        markup = (w.repo / 'web' / prefix / 'r' / rid / 'index.html').read_text()
        assert badge in markup and '"alumniOf"' not in markup and '"worksFor"' not in markup
    article(w, PRESS[1], '미토우 출신 김도훈 셰프. 레스토랑 ‘오닐’의 오너 셰프 김도훈은 오늘 계절 메뉴를 준비한다.')
    assert process(w)[1]['verified'] == 1
    entry.main()
    upgraded, = bundled_seeds(w)
    assert upgraded['id'] == rid and upgraded['lineage_status'] == 'verified'
    assert not upgraded.get('career_checking') and upgraded['role'] == '출신'
    assert sum(r['kind'] == 'new_alumni' for r in json.loads((w.repo / 'crawler/applied.json').read_text())) == 1
    seed = json.loads(w.seed_path.read_text())
    seed['restaurants'][-1]['verification_status'] = 'confirmed'
    original = w.seed_path.read_bytes()
    w.seed_path.write_text(json.dumps(seed, ensure_ascii=False))
    with pytest.raises(ValueError, match='manual correction PR'):
        site_publish.rollback(w.seed_path, rid, 'test refusal')
    w.seed_path.write_bytes(original)
    monkeypatch.setenv('KG_SERVICE_DB', str(w.repo / 'unavailable.sqlite'))
    tomb = site_publish.rollback(w.seed_path, upgraded['publication']['review_id'], 'fixture correction')
    assert tomb['id'] == rid and tomb['day_kst'] == '2026-11-20'
    assert site_publish.rollback(w.seed_path, rid, 'repeat') is None
    assert approved_reviews(w)
    monkeypatch.setenv('KG_SERVICE_DB', str(w.db_path))
    entry.main()
    assert not approved_reviews(w)
    article(w, PRESS[2], FIRST)
    process(w)
    entry.main()
    assert not bundled_seeds(w)
    run_build(w)
    assert not (w.repo / 'web/r' / rid).exists()
    load_seed_targets(w.conn, json.loads(w.seed_path.read_text()))
    ids = (tomb['chef_target_id'], tomb['restaurant_target_id'])
    assert {row[0] for row in w.conn.execute("SELECT json_extract(attrs_json,'$.published_to_site') FROM target WHERE target_id IN (?,?)", ids)} == {0}


def test_twelve_per_kst_day_empty_kitchens_seoul_and_rollback_slot(publication_world, monkeypatch):
    w = publication_world
    names = ['김도훈', '최성진', '백은경', '문현우', '이지연', '엄준호', '김경민', '박도희',
             '나유진', '조영민', '강희진', '마재호', '이준영', '박규호', '홍도나']
    venues = ['오닐', '해봄', '송연', '가람채', '머루담', '해진당', '솔해정', '은들집',
              '오정채', '달보리', '여름집', '들봄채', '온봄정', '오후장', '도란정']
    for i, (chef, venue) in enumerate(zip(names, venues)):
        kitchen = '라연' if i == 14 else '미토우'
        city = '부산' if i in (13, 14) else '서울'
        article(w, PRESS[0], f'{kitchen} 출신 {chef} 셰프가 {city}에서 레스토랑 ‘{venue}’을 열었다.')
        article(w, PRESS[1], f'{chef} 셰프는 {kitchen}에서 수셰프로 일했다. {chef} 셰프는 레스토랑 ‘{venue}’의 헤드 셰프이다.')
    assert process(w)[1]['verified'] == 15
    entry = app(w, monkeypatch)
    entry.main()
    rows = bundled_seeds(w)
    assert len(rows) == site_publish.CAP == 12
    assert rows[0]['city'] == '서울' and rows[1]['kitchen'] == 'layeon'
    assert all(r['city'] == '서울' for r in rows[2:])
    assert all(r['checked'] == '2026-11-20' for r in rows)
    entry.main()
    assert bundled_seeds(w) == rows
    site_publish.rollback(w.seed_path, rows[-1]['id'], 'reserved cap slot')
    entry.main()
    assert len(bundled_seeds(w)) == 11
    real_instant = dt.datetime.fromisoformat(NEXT_DAY.replace('Z', '+00:00')).astimezone(site_publish.KST)
    monkeypatch.setattr(site_publish, 'instant', lambda now=None: real_instant if now is None else
                        (dt.datetime.fromisoformat(now.replace('Z', '+00:00')).astimezone(site_publish.KST)
                         if isinstance(now, str) else now.astimezone(site_publish.KST)))
    entry.main()
    restored = bundled_seeds(w)
    assert len(restored) == 14 and sum(r['checked'] == '2026-11-21' for r in restored) == 3
    assert rows[-1]['id'] not in {r['id'] for r in restored}


def test_interrupted_apply_recovers_with_one_audit_event(publication_world, monkeypatch):
    w = publication_world
    article(w, PRESS[0], FIRST)
    article(w, PRESS[1], SECOND)
    process(w)
    entry = app(w, monkeypatch)
    atomic = site_publish.atomic_json

    def crash_before_seed(path, payload):
        if Path(path) == w.seed_path:
            raise OSError('fixture write failure')
        atomic(path, payload)

    with monkeypatch.context() as m:
        m.setattr(site_publish, 'atomic_json', crash_before_seed)
        with pytest.raises(OSError):
            entry.main()
    assert not bundled_seeds(w) and approved_reviews(w)

    def crash_after_seed(path, payload):
        if Path(path).name == 'applied.json':
            raise OSError('fixture audit write failure')
        atomic(path, payload)

    with monkeypatch.context() as m:
        m.setattr(site_publish, 'atomic_json', crash_after_seed)
        with pytest.raises(OSError):
            entry.main()
    row, = bundled_seeds(w)
    entry.main()
    entry.main()
    assert bundled_seeds(w) == [row]
    log = json.loads((w.repo / 'crawler/applied.json').read_text())
    assert len(log) == 1 and log[0]['id'] == row['id'] and log[0]['gate'] == 'verified'


def test_competing_apply_and_inherited_daily_lock(publication_world, monkeypatch):
    import fcntl
    import os

    w = publication_world
    article(w, PRESS[0], FIRST)
    article(w, PRESS[1], SECOND)
    process(w)
    # Run standalone crawler entry points concurrently using a complete temp source tree.
    shutil.copytree(ROOT / 'daemon', w.repo / 'daemon', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'sources', w.repo / 'sources')
    for filename in ('apply.py', 'config.json', 'vault.py', 'privacy_filter.py'):
        shutil.copyfile(ROOT / 'crawler' / filename, w.repo / 'crawler' / filename)
    command = [sys.executable, 'crawler/apply.py']
    with (w.repo / '.git/kg_daily.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        env = {**os.environ, 'KG_DAILY_LOCK_FD': str(lock.fileno())}
        a = subprocess.Popen(command, cwd=w.repo, env=env, pass_fds=(lock.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        b = subprocess.Popen(command, cwd=w.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert not a.communicate(timeout=10)[1] and a.returncode == 0
    assert not b.communicate(timeout=10)[1] and b.returncode == 0
    assert len(bundled_seeds(w)) == 1
    assert len(json.loads((w.repo / 'crawler/applied.json').read_text())) == 1


def test_fingerprint_reuses_refetch_and_requires_review_on_new_proof(publication_world, monkeypatch):
    from kg_ai.budget import Budget
    from kg_ai.deepseek import Client
    from test_deepseek_client import meter, response
    from tests_balance_fake import healthy_balance

    w = publication_world
    article(w, PRESS[0], FIRST, slug='original')
    process(w)
    original = w.conn.execute("SELECT review_id,payload_json FROM review_queue WHERE reason=?", (site_publish.REASON,)).fetchone()

    def transport(body, _key):
        prompt = json.loads(json.loads(body)['messages'][1]['content'])
        return response({'reviews': [{'id': prompt['id'], 'decision': 'approve', 'reason': 'All stated'}]})

    client = Client(Budget(meter(w.conn)), key='fixture', fallback_key='', transport=transport, balance_transport=healthy_balance)
    assert review.review(w.conn, w.seed_path, w.log, w.raw.root, client=client)['bundle_approved'] == 1
    article(w, PRESS[0], FIRST, slug='original')
    process(w)
    only, = approved_reviews(w)
    assert only['review_id'] == original['review_id']
    payload = json.loads(only['payload_json'])
    assert payload['approval']['proofs']['lineage'][0]['fetch_id'] == 'f0'
    article(w, PRESS[0], FIRST.replace('열었다', '오픈했다'), slug='second-story')
    assert process(w)[1]['checking'] == 1
    app(w, monkeypatch).main()
    assert not bundled_seeds(w)
    current = w.conn.execute("SELECT review_id FROM review_queue WHERE reason=? AND state='open'", (site_publish.REASON,)).fetchone()
    assert current['review_id'] != original['review_id']

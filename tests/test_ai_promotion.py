"""Exercise approved career publication without provider or network calls."""
import json
import subprocess
import sys

import pytest

from test_ai_review import client_for
from test_site_publication import ROOT, NOW, NEXT_DAY, app, article, publication_world, run_build
from kg_ai import promotion, review
from kg_daemon import claims
from kg_daemon.seed_migration import migrate_seed

FIRST = '민하늘 셰프는 미토우에서 헤드 셰프로 근무하고 있다.'
SECOND = '미토우의 헤드 셰프 민하늘은 제철 생선으로 저녁 메뉴를 선보인다.'


@pytest.fixture
def world(publication_world):
    w = publication_world
    migrate_seed(w.conn, w.seed)
    return w


def finding(w, quote, *, chef='민하늘', venue='미토우', label='employment_claim', host='hankyung.com',
            approve=False, role=None, when=None, article_text=None, fetch_id=None):
    body = article_text or f'{quote}\n기존식당과 미토우에 관한 취재 기사입니다.'
    ident = fetch_id or article(w, host, body)
    fetch = w.conn.execute('SELECT * FROM fetch_record WHERE fetch_id=?', (ident,)).fetchone()
    xid = 'promotion-' + str(w.conn.execute('SELECT COUNT(*) FROM extraction').fetchone()[0])
    start = body.index(quote)
    w.conn.execute("""INSERT INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,chef_text,restaurant_text,
        role_text,date_text,span_start,span_end,quote,extractor,extractor_version,published_at,created_at,asserted_by,pending_only)
        VALUES (?,?,?,?,?,'inference',?,?,?,?,?,?,?,'deepseek:web','web-extract-1',?,?,'ai_inference',1)""",
        (xid, ident, fetch['canonical_url'], host, label, chef, venue, role, when, start, start + len(quote), quote,
         fetch['published_at'], NOW))
    rid = claims.queue_review(w.conn, 'ai_inference', 'extraction', xid, {}, dedupe_key=xid, now=NOW)
    entry = review.annotation(dict(label=label, quote=quote, canonical_url=fetch['canonical_url'],
                                   published_at=fetch['published_at'], chef_text=chef, restaurant_text=venue,
                                   org_text=None, extraction_id=xid), rid, NOW)
    if approve:
        review._close(w.conn, rid, 'approve', 'fixture approval', None, NOW)
        seed = read(w)
        seed.setdefault('ai_reviewed_unlinked', []).append(entry)
        w.seed_path.write_text(json.dumps(seed, ensure_ascii=False))
    w.conn.commit()
    return xid, rid, entry


def read(w):
    return json.loads(w.seed_path.read_text())


def run(w, approvals=()):
    calls = []
    stats = review.review(w.conn, w.seed_path, w.log, w.raw.root,
                          client=client_for(w.conn, {x: 'approve' for x in approvals}, calls))
    assert bool(calls) == bool(approvals)
    return stats


def jobs(seed):
    return [edge for kind in ('restaurants', 'kitchens') for row in seed['career_projection'][kind]
            for edge in row.get('career_affiliations', ())]


def daily(w, monkeypatch):
    app(w, monkeypatch).main()
    return json.loads((w.repo / 'crawler' / 'out' / 'last_summary.json').read_text())['publication']


def rollback(w, rid, db=False):
    options = ['--db', str(w.db_path)] if db else []
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'ai_review_rollback.py'), '--review-id', rid,
                    '--seed', str(w.seed_path), '--log', str(w.log), *options], check=True, capture_output=True)


def test_approval_checking_then_independent_upgrade(world, monkeypatch):
    w = world
    xid, first, entry = finding(w, FIRST, role='헤드 셰프')
    assert run(w, [xid])['promoted'] == 1
    edge, = jobs(read(w))
    assert edge['lineage_status'] == 'unverified' and 'chef' in edge['career_checking']
    assert edge['evidence'] == [dict(kind='affiliation', src=entry['src'], quote=FIRST, badge='AI 검토')]
    pub = daily(w, monkeypatch)
    assert (pub['added'], pub['new_alumni_checking'], pub['new_alumni_verified']) == (1, 1, 0)
    assert pub['named_chefs'] == 2 and pub['checking_total'] == 1

    finding(w, SECOND, host='www.hankyung.com', approve=True)
    assert run(w)['promoted'] == 0
    assert jobs(read(w))[0]['lineage_status'] == 'unverified'
    _, corroboration, _ = finding(w, '민하늘 셰프가 미토우의 주방을 이끌고 있다.', host='newsis.com',
                                      article_text='민하늘 셰프가 미토우의 주방을 이끌고 있다. 계절마다 식재료를 연구한다.',
                                      approve=True)
    assert run(w)['promoted'] == 0
    edge, = jobs(read(w))
    assert edge['lineage_status'] == 'verified'
    assert len(edge['evidence']) == 3
    pub = daily(w, monkeypatch)
    assert (pub['added'], pub['upgraded'], pub['new_alumni_verified'], pub['verified_total']) == (0, 1, 0, 1)
    assert daily(w, monkeypatch)['upgraded'] == 0
    rollback(w, corroboration)
    assert jobs(read(w))[0]['lineage_status'] == 'unverified'
    assert run(w)['promoted'] == 0
    assert first in read(w)[promotion.KEY]['supports']


def test_new_venue_backfill_idempotent_render_and_rollback(world):
    w = world
    _, rid, entry = finding(w, '팝업을 마무리한 민하늘 셰프가 고향인 광주에서 한식 다이닝 레스토랑 ‘정연’을 오픈했다.',
                            venue='정연', label='opening', approve=True)
    stats = run(w)
    assert (stats['calls'], stats['promoted'], stats['unlinked_remaining']) == (0, 1, 0)
    seed = read(w)
    row = next(r for r in seed['restaurants'] if r['name'] == '정연')
    published = next(r for r in seed['career_projection']['restaurants'] if r['id'] == row['id'])
    assert published['city'] == '광주' and 'country' not in published
    assert row['ai_reviewed'] == [entry]
    assert published['career_affiliations'][0]['relation'] == 'opening'
    before, audit = w.seed_path.read_bytes(), w.log.read_bytes() if w.log.exists() else b''
    assert run(w)['promoted'] == 0
    assert w.seed_path.read_bytes() == before
    assert (w.log.read_bytes() if w.log.exists() else b'') == audit

    run_build(w)
    private = ('ai_promotion', 'ai_promotion_skip', 'promotion_skips', 'binding_receipt', 'body_fingerprint', 'model_meta')
    for prefix, status, badge in (('', '확인 중', 'AI 검토'), ('en/', 'Checking', 'AI-reviewed')):
        page = (w.repo / 'web' / prefix / 'r' / row['id'] / 'index.html').read_text()
        assert all(s in page for s in ('민하늘', status, badge, '&amp;b=2'))
        assert not any(k in page for k in (*private, 'worksFor', 'alumniOf', 'addressCountry', 'noindex'))
        assert not any(k in (w.repo / 'web' / prefix / 'rss.xml').read_text() for k in private)
    directory = (w.repo / 'web' / 'r')
    assert {p.name for p in directory.iterdir()} == {r['id'] for r in seed['restaurants']}
    js = (w.repo / 'web' / 'data.js').read_text()
    assert 'AI 검토' in js and not any(k in js for k in private)
    rollback(w, rid, db=True)
    assert not any(r['id'] == row['id'] for r in read(w)['restaurants'])
    assert not any(r['id'] == row['id'] for r in read(w)['career_projection']['restaurants'])
    assert not w.conn.execute("SELECT 1 FROM chef WHERE display_name='민하늘'").fetchone()
    assert not w.conn.execute("SELECT 1 FROM restaurant WHERE name='정연'").fetchone()
    assert run(w)['promoted'] == 0
    before = w.seed_path.read_bytes()
    rollback(w, rid, db=True)
    assert w.seed_path.read_bytes() == before


@pytest.mark.parametrize(('quote', 'venue', 'label'), [
    ('민하늘 셰프는 별빛호텔에서 총괄 셰프로 근무하고 있다.', '별빛호텔', 'employment_claim'),
    ('민하늘 셰프는 글로벌 푸드 대회에서 셰프로 일했다.', '글로벌 푸드 대회', 'career'),
    ('민하늘 셰프는 정연에서 게스트 셰프로 일했다.', '정연', 'employment_claim'),
    ('민하늘 셰프가 팝업 레스토랑 정연을 오픈했다.', '정연', 'opening'),
    ('민하늘 셰프가 온다. 최가람 셰프는 정연에서 일했다.', '정연', 'employment_claim'),
    ('민하늘 셰프가 정연을 오픈할 예정이다.', '정연', 'opening'),
])
def test_guard_keeps_non_careers_unlinked(world, quote, venue, label):
    finding(world, quote, venue=venue, label=label, approve=True)
    stats = run(world)
    assert (stats['promoted'], stats['unlinked_remaining']) == (0, 1)
    assert promotion.KEY not in read(world)


def test_dated_former_and_opening_make_a_lineage_path(world, monkeypatch):
    w = world
    past = '민하늘 셰프는 미토우에서 2019년까지 일했다.'
    opened = '민하늘 셰프가 2022년에 레스토랑 정연을 오픈했다.'
    body = past + '\n' + opened
    finding(w, past, label='career', when='2019년', approve=True, article_text=body)
    finding(w, opened, venue='정연', label='opening', when='2022년', approve=True, article_text=body, fetch_id='f0')
    assert run(w)['promoted'] == 2
    s = read(w)
    dest = next(r for r in s['restaurants'] if r['name'] == '정연')
    public = next(r for r in s['career_projection']['restaurants'] if r['id'] == dest['id'])
    origin, = public['origins']
    assert origin['kitchen_id'] == 'mitou' and origin['relation'] == 'former'
    assert origin['career_checking'] == ['kitchen', 'period']
    assert daily(w, monkeypatch)['alumni_by_kitchen']['mitou'] == 1


def test_two_review_passes_before_apply_emit_one_verified_addition(world, monkeypatch):
    w = world
    finding(w, FIRST, approve=True)
    assert run(w)['promoted'] == 1
    finding(w, SECOND, host='newsis.com', approve=True)
    assert run(w)['promoted'] == 0
    pub = daily(w, monkeypatch)
    assert (pub['added'], pub['upgraded'], pub['new_alumni_checking'], pub['new_alumni_verified']) == (1, 0, 0, 1)
    assert daily(w, monkeypatch)['added'] == 0


@pytest.mark.parametrize('point', ['before_seed', 'after_seed', 'after_audit'])
def test_replays_interrupted_review_delivery(world, monkeypatch, point):
    w = world
    xid, rid, _ = finding(w, FIRST)
    original_seed, original_audit = review.atomic_json, review.append_log

    def crash_seed(*args):
        if point == 'after_seed':
            original_seed(*args)
        raise OSError('simulated interruption')

    def crash_audit(*args):
        original_audit(*args)
        raise OSError('simulated interruption')

    with monkeypatch.context() as mock:
        if point == 'after_audit':
            mock.setattr(review, 'append_log', crash_audit)
        else:
            mock.setattr(review, 'atomic_json', crash_seed)
        with pytest.raises(OSError, match='simulated interruption'):
            run(w, [xid])
    run(w)
    assert len(jobs(read(w))) == 1
    assert sum(row.get('review_id') == rid for row in promotion.audit_rows(w.log)) == 1
    assert w.conn.execute("SELECT json_type(payload_json,'$.ai_review.delivery') FROM review_queue WHERE review_id=?", (rid,)).fetchone()[0] is None


def test_standalone_candidate_uses_source_body(world):
    w = world
    source = 'promotion-fixture-press'
    w.conn.execute("INSERT INTO source(source_id,name,kind,host,tier,created_at) VALUES (?,?,'press',?,'press',?)",
                   (source, 'Fixture press', 'newsis.com', NOW))
    url = 'https://newsis.com/feature'
    for i, body in enumerate((FIRST, '다른 내용의 원문입니다.'), 1):
        raw = w.raw.root / f'document-{i}.json'
        raw.write_text(json.dumps({'title': '주방 기사', 'body': body}, ensure_ascii=False))
        w.conn.execute("""INSERT INTO document(document_id,source_id,url,canonical_url,content_hash,fetched_at,raw_ref)
                       VALUES (?,?,?,?,?,?,?)""", (str(i), source, url, url, str(i), NOW, str(raw)))
        w.conn.execute("""INSERT INTO candidate_fact(candidate_id,idempotency_key,document_id,source_id,fact_type,
                         payload_json,evidence_json,extractor,asserted_by,created_at)
                         VALUES (?,?,?,?,'employment',?,?,'fixture','ai_inference',?)""",
                       (str(i), str(i), str(i), source, json.dumps({'label': 'employment_claim'}),
                        json.dumps({'quote': FIRST, 'source_url': url}), NOW))
        review_id = claims.queue_review(w.conn, 'ai_inference', 'candidate_fact', str(i), {}, dedupe_key='doc-' + str(i))
        review._close(w.conn, review_id, 'approve', 'fixture approval', None, NOW)
        seed = read(w)
        seed.setdefault('ai_reviewed_unlinked', []).append(dict(label='employment_claim', quote=FIRST, src=url,
            chef='민하늘', restaurant='미토우', badge='AI 검토', provenance='ai_reviewed', review_id=review_id,
            extraction_id=str(i)))
        w.seed_path.write_text(json.dumps(seed, ensure_ascii=False))
    w.conn.commit()
    assert run(w)['promoted'] == 1
    support, = read(w)[promotion.KEY]['supports'].values()
    assert support['snapshot_checked'] and support['independent_eligible']
    assert len(jobs(read(w))[0]['evidence']) == 1


def test_named_hotel_outlet_and_bilingual_chef_alias(world):
    w = world
    quote = '민하늘(Min Ha-neul) 셰프는 별빛호텔 한식당 미토우에서 근무하고 있다.'
    finding(w, quote, approve=True)
    finding(w, 'Min Ha-neul works as head chef at Mitou.', chef='Min Ha-neul', venue='Mitou',
            host='newsis.com', approve=True)
    assert run(w)['promoted'] == 1
    edge, = jobs(read(w))
    assert (edge['chef_name'], edge['chef_name_en'], edge['lineage_status']) == ('민하늘', 'Min Ha-neul', 'verified')


@pytest.mark.parametrize(('quote', 'expected'), [
    ('Ana Baker works as head chef at Celeste in France.', {'country': 'FR'}),
    ('Ana Baker works as head chef at Celeste in Dubai.', {'city': 'Dubai'}),
    ('Ana Baker works as head chef at Celeste.', {}),
])
def test_new_venue_uses_only_stated_geography(world, quote, expected):
    finding(world, quote, chef='Ana Baker', venue='Celeste', approve=True)
    assert run(world)['promoted'] == 1
    row = next(r for r in read(world)['restaurants'] if r['name'] == 'Celeste')
    assert {k: row[k] for k in ('city', 'country') if k in row} == expected


def test_named_training_keeps_mentor_and_no_destination(world):
    finding(world, 'Ana Baker trained under Bea West at Mitou.', chef='Ana Baker', venue='Mitou',
            label='mentorship_claim', approve=True)
    assert run(world)['promoted'] == 1
    edge, = jobs(read(world))
    assert (edge['relation'], edge['mentor_name'], edge['career_checking']) == ('training', 'Bea West', ['chef'])
    assert all(not row.get('origins') for row in read(world)['career_projection']['restaurants'])


@pytest.mark.parametrize(('other_host', 'same_body'), [('newsis.com', True), ('celebinfluencernews.com', False)])
def test_repost_and_cite_only_sources_do_not_verify(world, other_host, same_body):
    first_body = FIRST + '\n' + SECOND + '\n' + '이 기사는 식당 주방의 근무 경력을 설명합니다. ' * 3
    finding(world, FIRST, approve=True, article_text=first_body)
    finding(world, SECOND, host=other_host, approve=True, article_text=first_body if same_body else None)
    assert run(world)['promoted'] == 1
    edge, = jobs(read(world))
    assert edge['lineage_status'] == 'unverified' and len(edge['evidence']) == 2


def test_rollback_preserves_a_later_manual_edit(world):
    _, rid, _ = finding(world, 'Ana Baker works at Celeste.', chef='Ana Baker', venue='Celeste', approve=True)
    assert run(world)['promoted'] == 1
    data = read(world)
    venue = next(r for r in data['restaurants'] if r['name'] == 'Celeste')
    venue['note'] = 'curated independently'
    world.seed_path.write_text(json.dumps(data, ensure_ascii=False))
    rollback(world, rid, db=True)
    assert next(r for r in read(world)['restaurants'] if r['id'] == venue['id'])['note'] == 'curated independently'
    assert jobs(read(world)) == []


def test_fingerprint_recipe_matches_native_career_document():
    from kg_ai.article import digest
    from kg_collect import evaluate
    from kg_collect.site_projection import retained_fingerprint, _shingle_set

    text = FIRST + '\n' + SECOND + '\n' + '이 기사는 식당 주방의 근무 경력을 설명합니다. ' * 3
    # lineage._finish stores digest(shingle)[:12] over casefolded shingles as JSON;
    # _snapshot packs that stored list, and promotion must supply the same hashes.
    hashes = sorted(digest(s)[:12] for s in evaluate.shingles(text[:3000].casefold()))
    native = retained_fingerprint(json.loads(json.dumps(hashes)))
    assert _shingle_set(native) == {bytes.fromhex(h) for h in hashes}


def test_native_near_repost_does_not_verify_reviewed_link(world, monkeypatch):
    from test_lineage_g3 import fake_adapter, fetch, ingest, item

    w = world
    native = (w.conn, w.raw)
    shared = FIRST + '\n' + SECOND + '\n' + '이 기사는 식당 주방의 근무 경력을 설명합니다. ' * 3
    adapter = fake_adapter(w.conn, [(FIRST, item(FIRST, '민하늘', '미토우', 'current'))], [], monkeypatch)
    doc = fetch(native, 'native-repost-test', shared, hint='ko')
    assert ingest(native, doc, adapter)
    # A different publisher carries an edited copy of the same article: the body
    # fingerprint must collapse it even though URL, quote and hash all differ.
    finding(w, SECOND, host='newsis.com', approve=True, article_text=shared.replace('설명합니다', '소개합니다'))
    assert run(w)['promoted'] == 1
    edge, = jobs(read(w))
    assert edge['lineage_status'] == 'unverified' and len(edge['evidence']) == 2


@pytest.mark.parametrize('selector', ['extraction', 'since'])
def test_rollback_selectors_remove_promoted_link(world, selector):
    w = world
    xid, rid, _ = finding(w, FIRST, approve=True)
    assert run(w)['promoted'] == 1
    flag = ['--extraction-id', xid] if selector == 'extraction' else ['--since', NOW[:10]]
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'ai_review_rollback.py'), *flag,
                    '--seed', str(w.seed_path), '--log', str(w.log), '--db', str(w.db_path)],
                   check=True, capture_output=True)
    assert jobs(read(w)) == []
    assert rid in read(w)[promotion.KEY]['withdrawn']
    assert run(w)['promoted'] == 0


def test_native_g3_source_corroborates_review_and_survives_rollback(world, monkeypatch):
    from test_lineage_g3 import fake_adapter, fetch, ingest, item

    w = world
    native = (w.conn, w.raw)
    quote = '민하늘 셰프는 미토우에서 총괄 셰프로 근무하고 있다.'
    adapter = fake_adapter(w.conn, [(quote, item(quote, '민하늘', '미토우', 'current'))], [], monkeypatch)
    doc = fetch(native, 'native-promotion-test', quote, hint='ko')
    assert ingest(native, doc, adapter)
    _, rid, _ = finding(w, SECOND, approve=True)
    assert run(w)['promoted'] == 1
    edge, = jobs(read(w))
    assert edge['lineage_status'] == 'verified'
    assert [e.get('badge') for e in edge['evidence']] == [None, 'AI 검토']
    rollback(w, rid, db=True)
    assert w.conn.execute("SELECT COUNT(*) FROM career_assertion WHERE state='source_claim'").fetchone()[0] == 1
    assert w.conn.execute("SELECT COUNT(*) FROM chef WHERE display_name='민하늘'").fetchone()[0] == 1


def test_reupgrade_after_downgrade_counts_again(world, monkeypatch):
    w = world
    xid, _, _ = finding(w, FIRST, role='헤드 셰프')
    assert run(w, [xid])['promoted'] == 1
    assert daily(w, monkeypatch)['new_alumni_checking'] == 1
    _, corroboration, _ = finding(w, '민하늘 셰프가 미토우의 주방을 이끌고 있다.', host='newsis.com',
                                  article_text='민하늘 셰프가 미토우의 주방을 이끌고 있다. 계절마다 식재료를 연구한다.',
                                  approve=True)
    assert run(w)['promoted'] == 0
    assert daily(w, monkeypatch)['upgraded'] == 1
    rollback(w, corroboration)
    assert jobs(read(w))[0]['lineage_status'] == 'unverified'
    # A new independent review on the next KST day re-upgrades the link: the
    # verify event already applied for the first upgrade must not silence it.
    monkeypatch.setattr(review, 'utcnow', lambda: NEXT_DAY)
    finding(w, '민하늘 셰프는 미토우의 주방을 운영하고 있다.', host='foodnews.news',
            article_text='민하늘 셰프는 미토우의 주방을 운영하고 있다. 미토우의 저녁 코스는 제철 식재료로 구성된다.',
            approve=True)
    assert run(w)['promoted'] == 0
    assert jobs(read(w))[0]['lineage_status'] == 'verified'
    assert daily(w, monkeypatch)['upgraded'] == 1
    assert daily(w, monkeypatch)['upgraded'] == 0


@pytest.mark.parametrize(('english', 'name', 'org'), [(False, '민하늘', '미토우'), (True, 'Min Ha-neul', 'Mitou')])
def test_people_jsonld_merges_curated_scalar_into_reviewed_origin_person(english, name, org):
    import importlib.util
    spec = importlib.util.spec_from_file_location('lineage_evidence_fixture', ROOT / 'scripts' / 'lineage_evidence.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    kitchens = {'mitou': {'id': 'mitou', 'name': '미토우', 'en': 'Mitou'}}
    # The reviewed former stint lives only in origins; the destination employment
    # is carried solely by the curated scalar, which names the same chef.
    edge = {'kitchen_id': 'mitou', 'chef_id': 'chef-min', 'chef_name': '민하늘', 'chef_name_en': 'Min Ha-neul',
            'relation': 'former', 'lineage_status': 'verified', 'career_checking': [],
            'evidence': [{'kind': 'lineage', 'src': 'https://newsis.com/food/1', 'quote': '민하늘 셰프는 미토우에서 일했다.',
                          'badge': 'AI 검토'}]}
    row = {'id': 'r1', 'name': '정연', 'chef': 'Min Ha-neul', 'chef_en': 'Min Ha-neul', 'kitchen': 'mitou',
           'lineage_status': 'verified', 'career_checking': [], 'origins': [edge], 'career_affiliations': []}
    base, rid = 'https://kg.example', 'https://kg.example/r/r1/'
    people = module.people_jsonld(row, kitchens, base, rid, english)
    assert people == [{'@type': 'Person', 'name': name, 'jobTitle': 'Chef' if english else '셰프',
                       'alumniOf': [{'@type': 'Organization', 'name': org, 'url': f'{base}/k/mitou/'}],
                       'worksFor': {'@id': rid}}]
    row['career_checking'] = ['chef']
    person, = module.people_jsonld(row, kitchens, base, rid, english)
    assert 'worksFor' not in person


@pytest.mark.parametrize(('chef', 'venue', 'label', 'quote', 'relation'), [
    ('샘킴', '오스테리아 샘킴', 'employment_claim',
     '예약 수 기준으로 보면 도우룸 광화문(이준), 오스테리아 샘킴(샘킴), 에그앤플라워(김희은)가 상위권이다.', 'current'),
    ('김희은', '에그앤플라워', 'employment_claim',
     '레스토랑별 셰프: 김희은（에그앤플라워）, 이준（도우룸 광화문）.', 'current'),
    ('Ana Baker', 'Celeste', 'employment_claim',
     'Restaurants and their chefs: Other Place (Chef Bea West), “Celeste” (Chef Ana Baker).', 'current'),
    ('Ana Baker', 'Celeste', 'career',
     'Chefs and their restaurants: Chef Ana Baker (Celeste), Chef Bea West (Other Place).', 'current'),
    ('민하늘', '정연', 'career', '민하늘 셰프가 운영하는 별빛호텔의 식당 정연', 'current'),
    ('민하늘', '정연', 'career', '민하늘 셰프가 이끄는 정연', 'current'),
    ('민하늘', '정연', 'career', '민하늘 셰프가 주방을 맡고 있는 정연', 'current'),
    ('민하늘', '정연', 'career', '정연의 오너셰프 민하늘', 'current'),
    ('민하늘', '정연', 'career', '정연의 헤드셰프 민하늘', 'current'),
    ('민하늘', '정연', 'career', '정연의 총괄셰프 민하늘', 'current'),
    ('민하늘', '정연', 'career', '정연 민하늘 셰프', 'current'),
    ('민하늘', '정연', 'career', '정연의 전 헤드셰프 민하늘', 'former'),
    ('민하늘', '정연', 'career', '민하늘 셰프는 식당 ‘수퍼판’과 레스토랑 ‘정연’을 운영하고 있다.', 'current'),
    ('민하늘', '수퍼판', 'career', '민하늘 셰프는 식당 ‘수퍼판’과 레스토랑 ‘정연’을 운영하고 있다.', 'current'),
    ('Ana Baker', 'Celeste', 'career', 'Chef Ana Baker runs Celeste.', 'current'),
    ('Ana Baker', 'Celeste', 'career', 'Celeste, run by chef Ana Baker, serves dinner.', 'current'),
    ('Ana Baker', 'Celeste', 'career', 'Ana Baker, head chef at Celeste, serves dinner.', 'current'),
    ('Ana Baker', 'Celeste', 'career', 'Ana Baker is the chef at Celeste.', 'current'),
    ('Ana Baker', 'Celeste', 'career', 'Ana Baker, former head chef at Celeste.', 'former'),
    ('민하늘', '정연', 'opening', '파리에 문을 연 민하늘 셰프의 정연', 'opening'),
    ('민하늘', '정연', 'opening', '민하늘 셰프는 정연을 정식 개점했다.', 'opening'),
    ('민하늘', '정연', 'opening', '민하늘 셰프는 정연을 정식 오픈했다.', 'opening'),
    ('Ana Baker', 'Celeste', 'opening', 'Ana Baker launched Celeste.', 'opening'),
    ('Ana Baker', 'Celeste', 'opening', 'Chef Ana Baker returned to Paris. She opened Celeste there.', 'opening'),
    ('민하늘', '정연', 'opening', '민하늘 셰프는 파리로 돌아왔다. 그 뒤 정연을 열었다.', 'opening'),
    ('민하늘', '정연', 'opening', '민하늘·최가람 부부는 정연을 열었다.', 'opening'),
    ('민하늘', '정연', 'opening', '민하늘은 행사에 방문했다. 민하늘 셰프는 정연을 열었다.', 'opening'),
])
def test_coverage_literal_bindings(chef, venue, label, quote, relation):
    row, reason = promotion._parse(dict(chef=chef, restaurant=venue, label=label, quote=quote), {})
    assert reason is None
    assert (row['chef_text'], row['venue_text'], row['relation'], row['quote']) == (chef, venue, relation, quote)


@pytest.mark.parametrize(('quote', 'chef', 'venue', 'label'), [
    ('예약 수 기준으로 보면 정연(최가람), 미토우(민하늘)가 상위권이다.', '민하늘', '정연', 'career'),
    ('사진: 미토우(민하늘)', '민하늘', '미토우', 'career'),
    ('Restaurants and their chefs: Celeste (Bea West), Other Place (Ana Baker).', 'Ana Baker', 'Celeste', 'career'),
    ('Photo credit: Celeste (Chef Ana Baker).', 'Ana Baker', 'Celeste', 'career'),
    ('민하늘 | 20년 경력 | 미토우', '민하늘', '미토우', 'career'),
    ('민하늘 셰프의 미토우가 1위를 차지했다.', '민하늘', '미토우', 'career'),
    ('민하늘 셰프는 식당 미토우에서 상을 받았다.', '민하늘', '미토우', 'career'),
    ('민하늘 셰프가 운영하지 않는 정연', '민하늘', '정연', 'career'),
    ('Chef Ana Baker does not run Celeste.', 'Ana Baker', 'Celeste', 'career'),
    ('Chef Ana Baker runs Celeste Hotel.', 'Ana Baker', 'Celeste', 'career'),
    ('민하늘 셰프는 식당 ‘수퍼판’과 분식 브랜드 ‘가지가지’를 운영하고 있다.', '민하늘', '가지가지', 'career'),
    ('민하늘 셰프는 파리로 돌아왔다. 최가람은 정연을 열었다.', '민하늘', '정연', 'opening'),
    ('Chef Ana Baker returned to Paris. Her partner opened Celeste.', 'Ana Baker', 'Celeste', 'opening'),
    ('Chef Ana Baker met Chef Bea West in Paris. She opened Celeste.', 'Ana Baker', 'Celeste', 'opening'),
    ('Chef Ana Baker returned to Paris.\nShe opened Celeste.', 'Ana Baker', 'Celeste', 'opening'),
    ('Chef Ana Baker returned to Paris. Celeste opened its doors.', 'Ana Baker', 'Celeste', 'opening'),
    ('민하늘 셰프는 소개했고 최가람은 정연을 열었다.', '민하늘', '정연', 'opening'),
    ('민하늘 셰프가 레스토랑을 열었다.', '민하늘', '정연', 'opening'),
    ('민하늘 셰프가 정연을 가오픈했다.', '민하늘', '정연', 'opening'),
    ('His first European venture followed with Seok – in the French capital.', 'Mingoo Kang', 'Seok', 'opening'),
])
def test_coverage_refuses_unbound_pair(quote, chef, venue, label):
    row, reason = promotion._parse(dict(chef=chef, restaurant=venue, label=label, quote=quote), {})
    assert row is None and reason


@pytest.mark.parametrize(('venue', 'expected'), [
    ('오닐(ONIL)', 'temporary'), ('O’Nil', 'temporary'), ('DOORI', 'temporary'), ('주052', 'temporary'),
    ('조선호텔앤리조트', 'institution'), ('신세계그룹', 'institution'), ('신세계백화점', 'institution'),
    ('오덴세', 'institution'), ('마스터셰프 코리아4', 'institution'), ('흑백요리사', 'institution'),
    ('테이블웨어 유튜브 채널', 'institution'), ('사찰음식 연구소', 'institution'), ('파주 3층 식당', 'invalid_endpoint'),
])
def test_coverage_target_exclusions(world, venue, expected):
    _, rid, _ = finding(world, f'민하늘 셰프가 {venue}을 정식 개점했다.', venue=venue, label='opening', approve=True)
    stats = run(world)
    assert stats['promoted'] == 0
    assert stats['promotion_skips'] == {expected: 1}
    assert tuple(world.conn.execute('SELECT unlinked_index,review_id,reason FROM ai_promotion_skip').fetchone()) == (0, rid, expected)
    assert not world.conn.execute('SELECT 1 FROM ai_promotion_identity').fetchone()


def test_coverage_elided_bilingual_opening_and_retention(world):
    w = world
    first = '파리에서 레스토랑 ‘SEOK’이 별빛호텔에 문을 열었다.'
    second = '한식당 ‘세옥(SEOK)’이 문을 열었다.'
    xid, first_rid, _ = finding(w, first, venue='SEOK', label='opening',
                                article_text='민하늘 셰프가 ' + first, approve=True)
    _, second_rid, _ = finding(w, second, venue='세옥', label='opening', host='newsis.com',
                               article_text=second + ' 민하늘 셰프가 처음 선보인 레스토랑이다.', approve=True)
    assert run(w)['promoted'] == 1
    first_row, second_row = (read(w)[promotion.KEY]['supports'][rid] for rid in (first_rid, second_rid))
    assert (first_row['city'], first_row['country_code']) == ('파리', 'FR')
    assert (second_row['city'], second_row['country_code']) == (None, None)
    assert first_row['branch_id'] == second_row['branch_id']
    assert first_row['chef_id'] == second_row['chef_id']
    assert all(r['binding_receipt']['rule'] == 'source_attribution' for r in (first_row, second_row))
    assert jobs(read(w))[0]['lineage_status'] == 'verified'
    ref = w.conn.execute('SELECT text_ref FROM fetch_record WHERE fetch_id=(SELECT fetch_id FROM extraction WHERE extraction_id=?)',
                         (xid,)).fetchone()[0]
    (w.raw.root / ref).unlink()
    before = w.seed_path.read_bytes()
    assert run(w)['promoted'] == 0
    assert w.seed_path.read_bytes() == before
    rollback(w, second_rid, db=True)
    assert jobs(read(w))[0]['lineage_status'] == 'unverified'


@pytest.mark.parametrize(('chef', 'venue', 'quote', 'prefix', 'expected'), [
    ('손 셰프', '미토우', '손 셰프는 미토우에서 일했다.', '손종원 셰프가 과거를 돌아봤다. ', '손종원'),
    ('손종원', '미토우', '손 셰프는 미토우에서 일했다.', '셰프 손종원이 과거를 돌아봤다. ', '손종원'),
    ('Chef Baker', 'Celeste', 'Chef Baker opened Celeste.', 'Chef Ana Baker returned to Paris. ', 'Ana Baker'),
    ('Ana Baker', 'Celeste', 'Baker launched Celeste.', 'Chef Ana Baker returned to Paris. ', 'Ana Baker'),
    ('손 셰프', '미토우', '손 셰프는 미토우에서 일했다.', '손종원 셰프와 손하늘 셰프가 인사했다. ', None),
    ('Ana Baker', 'Celeste', 'Baker launched Celeste.', 'Ana Baker and Bea Baker returned to Paris. ', None),
])
def test_coverage_source_short_name(world, chef, venue, quote, prefix, expected):
    _, rid, _ = finding(world, quote, chef=chef, venue=venue, article_text=prefix + quote, approve=True,
                        label='career' if venue == '미토우' else 'opening')
    stats = run(world)
    if not expected:
        assert stats['promotion_skips'] == {'chef_unresolved': 1}
        return
    row = read(world)[promotion.KEY]['supports'][rid]
    assert row['resolved_chef_text'] == expected and row['binding_receipt']['rule'] == 'short_subject'
    assert len(jobs(read(world))) == 1


@pytest.mark.parametrize(('body', 'expected'), [
    ('민하늘 셰프가 정연을 소개했다. 정연을 열었다.', None),
    ('최가람 셰프는 정연을 열었다.', 'chef_unresolved'),
    ('민하늘 셰프는 정연을 열었다. 최가람 셰프는 정연을 열었다.', 'no_original'),
    ('사진: 정연을 열었다. 민하늘 셰프가 사진을 소개했다.', 'chef_unresolved'),
])
def test_coverage_exact_source_attribution(world, body, expected):
    finding(world, '정연을 열었다.', venue='정연', label='opening', article_text=body, approve=True)
    stats = run(world)
    assert stats['promotion_skips'] == ({expected: 1} if expected else {})
    assert stats['promoted'] == (0 if expected else 1)


def test_coverage_skip_snapshot_keeps_malformed_duplicate_and_rollback_rows(world):
    w = world
    _, rid, entry = finding(w, '민하늘 셰프가 정연을 찾았다.', venue='정연', approve=True)
    _, withdrawn, _ = finding(w, '민하늘 셰프가 미토우의 주방을 운영하고 있다.', approve=True)
    data = read(w)
    data['ai_reviewed_unlinked'].extend([{**entry, 'quote': '다른 검토 인용문입니다.'},
                                       {**entry, 'review_id': ['malformed']}])
    w.seed_path.write_text(json.dumps(data, ensure_ascii=False))
    review.append_log(w.log, {'action': 'rollback', 'review_id': withdrawn})
    stats = run(w)
    assert stats['promotion_skips'] == {
        'duplicate_review_id': 1, 'invalid_annotation': 1, 'no_relation': 1, 'tombstoned': 1,
    }
    assert [tuple(r) for r in w.conn.execute('SELECT unlinked_index,review_id,reason FROM ai_promotion_skip ORDER BY unlinked_index')] == [
        (0, rid, 'no_relation'), (1, withdrawn, 'tombstoned'), (2, rid, 'duplicate_review_id'), (3, None, 'invalid_annotation'),
    ]
    rollback(w, withdrawn, db=True)
    assert [tuple(r) for r in w.conn.execute('SELECT unlinked_index,reason FROM ai_promotion_skip ORDER BY unlinked_index')] == [
        (0, 'no_relation'), (1, 'duplicate_review_id'), (2, 'invalid_annotation'),
    ]
    data.pop('ai_reviewed_unlinked')
    w.seed_path.write_text(json.dumps(data, ensure_ascii=False))
    assert run(w)['promotion_skips'] == {}
    assert not w.conn.execute('SELECT 1 FROM ai_promotion_skip').fetchone()


@pytest.mark.parametrize('host', ['www.youtube.com', 'youtu.be', 'seoul.co.kr:8888'])
def test_coverage_unsafe_publication_source(world, host):
    # The annotation URL goes through the source gate before opening any retained file.
    _, _, entry = finding(world, FIRST, approve=True)
    data = read(world)
    entry['src'] = f'https://{host}/food/recipe'
    data['ai_reviewed_unlinked'] = [entry]
    world.seed_path.write_text(json.dumps(data, ensure_ascii=False))
    assert run(world)['promotion_skips'] == {'unsafe_source': 1}


@pytest.mark.parametrize('kind', ['disabled', 'manual_only', 'prohibited', 'event_notice'])
def test_coverage_original_source_policy(world, kind):
    w = world
    _, _, _ = finding(w, FIRST, approve=True)
    if kind == 'event_notice':
        w.conn.execute("INSERT INTO domain_profile(host,tier_guess,first_seen_at) VALUES ('hankyung.com','event_notice',?)", (NOW,))
    else:
        w.conn.execute("""INSERT INTO source(source_id,name,kind,host,tier,enabled,created_at)
            VALUES ('coverage-press','Fixture press','press','hankyung.com','press',?,?)""",
            (kind != 'disabled', NOW))
        if kind != 'disabled':
            w.conn.execute("""INSERT INTO source_policy(source_id,collect_permission,refresh_cadence,registry_version,updated_at)
                VALUES ('coverage-press',?,'daily','fixture',?)""", (kind, NOW))
    w.conn.commit()
    assert run(w)['promotion_skips'] == {'unsafe_source': 1}


def test_coverage_raw_span_maps_after_whitespace_cleanup(world):
    quote = '정연을 열었다.'
    body = '민하늘 셰프는    ' + quote
    fetch_id = article(world, 'hankyung.com', body, visible=body)
    finding(world, quote, venue='정연', label='opening', article_text=body, fetch_id=fetch_id, approve=True)
    assert run(world)['promoted'] == 1


def test_coverage_source_quote_and_stale_span_are_distinct_gates(world):
    w = world
    quote = '정연을 열었다.'
    xid, rid, _ = finding(w, quote, venue='정연', label='opening', article_text='민하늘 셰프는 ' + quote, approve=True)
    w.conn.execute('UPDATE extraction SET span_start=0,span_end=? WHERE extraction_id=?', (len(quote), xid))
    w.conn.commit()
    assert run(w)['promotion_skips'] == {'no_original': 1}
    data = read(w)
    data['ai_reviewed_unlinked'][0]['quote'] = '민하늘 셰프는 정연을 열었다.'
    w.seed_path.write_text(json.dumps(data, ensure_ascii=False))
    assert run(w)['promotion_skips'] == {'no_original': 1}
    assert w.conn.execute('SELECT review_id FROM ai_promotion_skip').fetchone()[0] == rid


def test_coverage_clipped_event_roster_stays_unlinked(world):
    quote = '정연(민하늘)'
    finding(world, quote, venue='정연', approve=True, article_text='팝업 레스토랑별 셰프: ' + quote)
    assert run(world)['promotion_skips'] == {'temporary': 1}

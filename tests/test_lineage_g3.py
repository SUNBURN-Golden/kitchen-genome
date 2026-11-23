import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kg_ai import pending
from kg_ai.budget import Budget
from kg_ai.deepseek import Client
from kg_collect import cycle, lineage, site_projection, site_publish
from kg_collect.deepseek import Adapters
from kg_collect.lineage_identity import candidate_keys, canonical, redirects, reverse
from kg_collect.pipeline import process_doc
from kg_collect.rawstore import RawStore
from kg_collect.targets import Gazetteer
from kg_daemon import claims
from kg_daemon.collect_bridge import collector_records
from kg_daemon.db import connect, migrate
from kg_daemon.runner import CostMeter

ROOT = Path(__file__).resolve().parents[1]
NOW = '2026-11-20T00:00:00Z'


def item(quote, chef, venue, relation='former', **fields):
    return dict(quote=quote, chef_text=chef, venue_text=venue, relation=relation, **fields)


@pytest.fixture
def world(tmp_path):
    conn = connect(str(tmp_path / 'service.sqlite3'))
    migrate(conn)
    return conn, RawStore(tmp_path / 'raw')


def branch(conn, key, name, country=None, city=None, seed_id=None):
    conn.execute('INSERT INTO restaurant(restaurant_id,name,created_at) VALUES (?,?,?)', ('r-' + key, name, NOW))
    conn.execute('INSERT INTO branch(branch_id,restaurant_id,country_code,city,created_at) VALUES (?,?,?,?,?)',
                 (key, 'r-' + key, country, city, NOW))
    if seed_id:
        conn.execute("""INSERT INTO alias(alias_id,entity_type,entity_id,alias,kind,created_at)
                        VALUES (?,'restaurant',?,?,'legacy_id',?)""", (key, 'r-' + key, seed_id, NOW))


def fetch(world, fid, text, host='eater.com', published_at='2026-11-05', hint=None):
    conn, raw = world
    url = f'https://{host}/food/{fid}'
    parts = ''.join(f'<p>{html.escape(p)}</p>' for p in text.splitlines())
    body = f'<html lang="{hint or ""}"><body><article>{parts}</article></body></html>'.encode()
    raw_ref, raw_hash = raw.put(host, body, 'html')
    text_ref = raw.put_text(host, text)
    conn.execute('''INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_ref,raw_ref,content_hash,
                   fetched_at,published_at,fetcher_version,extractor_version) VALUES (?,?,?,?,'html','ok',?,?,?,?,?,'test','test')''',
                 (fid, url, url, host, text_ref, raw_ref, raw_hash, NOW, published_at))
    conn.commit()
    return conn.execute('SELECT * FROM fetch_record WHERE fetch_id=?', (fid,)).fetchone()


def fake_adapter(conn, outputs, calls, monkeypatch):
    monkeypatch.setenv('KG_QWEN_MODEL', 'fixture-qwen')
    conn.execute("INSERT INTO daemon_run(run_id,daemon,status,cost_cap_units,started_at) VALUES ('g3-fixture','test','running',0,?)", (NOW,))
    conn.commit()
    meter = CostMeter(conn, 'g3-fixture', {'llm_tokens_k': .5})
    meter.stage = 'extract'

    def send(raw, key):
        assert key == 'fixture-key'
        body = json.loads(raw)
        assert body['model'] == 'fixture-qwen'
        assert body['enable_thinking'] is False
        article, parameters = (json.loads(m['content']) for m in body['messages'][1:])
        calls.append((article, parameters))
        matching = [row for anchor, row in outputs if anchor in article['text']]
        return 200, json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content':
                    json.dumps({'assertions': matching}, ensure_ascii=False)}}],
                    'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}).encode()
    return Adapters(Client(Budget(meter), key='', fallback_key='fixture-key', fallback_transport=send,
                           transport=lambda *_: pytest.fail('unexpected primary AI dispatch')))


def ingest(world, record, adapter, heavy=False):
    conn, raw = world
    gaz = Gazetteer(conn)
    if heavy:
        return cycle.extract_roster(conn, raw, record, gaz, adapter, None)
    doc = cycle.load_doc(conn, raw, record)
    return process_doc(conn, doc, gaz, tier='press', model_adapter=adapter, now=NOW)['lineage_complete']


ARTICLES = [
    ('ko', '한정수 셰프는 한정원에서 수셰프로 일했다.', '한정수', '한정원'),
    ('en', 'Ana Baker worked at Alder House as pastry chef from 2014 to 2016.', 'Ana Baker', 'Alder House'),
    ('fr', 'Léa Durand a travaillé chez Le Verger comme cheffe pâtissière.', 'Léa Durand', 'Le Verger'),
    ('ja', '山田花子は青庭レストランで料理長として勤務した。', '山田花子', '青庭レストラン'),
    ('es', 'Lucía Pérez trabajó en Casa Elena como chef pastelera.', 'Lucía Pérez', 'Casa Elena'),
    ('it', 'Matteo Bassi ha lavorato presso La Quercia come capo cuoco.', 'Matteo Bassi', 'La Quercia'),
    ('de', 'Lisa Becker arbeitete bei Lindenhof als Küchenchef.', 'Lisa Becker', 'Lindenhof'),
]


@pytest.mark.parametrize('lang,quote,chef,venue', ARTICLES)
def test_multilingual_verified_spans_and_legacy_filter(world, monkeypatch, lang, quote, chef, venue):
    conn, raw = world
    rows = [(quote, item(quote, chef, venue))]
    calls = []
    adapter = fake_adapter(conn, rows, calls, monkeypatch)
    document = fetch(world, f'bio-{lang}', 'Kitchen profile\n' + quote, hint=lang)
    assert ingest(world, document, adapter)
    assert len(calls) == 1 and calls[0][1]['language'] == lang
    assertion = conn.execute('SELECT * FROM career_assertion').fetchone()
    assert assertion['language'] == lang
    assert assertion['state'] == 'source_claim'
    stored = raw.read_text(document['text_ref'])
    assert stored[assertion['span_start']:assertion['span_end']] == assertion['quote'] == quote
    assert conn.execute('SELECT valid_to_kind FROM employment').fetchone()[0] == 'unknown'
    assert conn.execute('SELECT status FROM claim WHERE claim_id=?', (assertion['claim_id'],)).fetchone()[0] == 'source_claim'
    legacy = collector_records(conn.execute('PRAGMA database_list').fetchone()[2], {'press': 'test'})
    assert all(pending(r['body']['collect']) or r['body']['collect']['extractor'].startswith('rules')
               for records in legacy.values() for r in records)
    assert ingest(world, document, adapter, heavy=True)
    assert len(calls) == 1
    assert conn.execute('SELECT COUNT(*) FROM career_assertion').fetchone()[0] == 1


def test_quote_and_optional_fields_validate_independently(world, monkeypatch):
    conn, _ = world
    quote = 'Ana Baker worked at Alder House with sous-chef Miro Lake in a kitchen featured in 2020.'
    rows = [(quote, item(quote, 'Ana Baker', 'Alder House', role_text='sous-chef', from_text='2010')),
            (quote, item('Ana Baker trained at Alder House.', 'Ana Baker', 'Alder House', 'training')),
            (quote, item(quote, 'Miro Lake', 'Alder House'))]
    adapter = fake_adapter(conn, rows, [], monkeypatch)
    assert ingest(world, fetch(world, 'fields', quote), adapter)
    rows = conn.execute('SELECT state,role_text,valid_from,quote FROM career_assertion ORDER BY state DESC').fetchall()
    assert rows[0]['state'] == 'source_claim'
    assert rows[0]['role_text'] is None and rows[0]['valid_from'] is None
    assert sum(row['state'] == 'review' for row in rows) == 2
    interleaved = 'Ana Baker worked at Alder House before visiting New Cafe.'
    assert lineage.validate_item(item(interleaved, 'Ana Baker', 'New Cafe'), interleaved, 'en')[1] == 'unbound_career'


@pytest.mark.parametrize('quote,chef,venue,relation,fields,lang', [
    ('한정수가 일했고 김철수가 한정원에서 일했다.', '한정수', '한정원', 'former', {}, 'ko'),
    ('한정수가 개업했고 김철수가 한정원에서 수셰프로 2014년부터 일했다.', '한정수', '한정원', 'former',
     dict(role_text='수셰프', from_text='2014년'), 'ko'),
    ('한정수가 개업했고 김철수가 한정원에서 수셰프로 2014년부터 일했다.', '한정수', '한정원', 'opening', {}, 'ko'),
    ('山田花子が開業し、田中一郎が青庭レストランで勤務した。', '山田花子', '青庭レストラン', 'former', {}, 'ja'),
    ('한정수가 현재 김철수가 한정원에서 근무한다고 말했다.', '한정수', '한정원', 'current', {}, 'ko'),
    # An honorific/title on the SECOND subject must not pass as an appositive of the chef.
    ('한정수가 일했고 김철수 씨가 한정원에서 일했다.', '한정수', '한정원', 'former', {}, 'ko'),
    ('한정수가 개업했고 김철수 씨가 한정원에서 수셰프로 2014년부터 일했다.', '한정수', '한정원', 'former',
     dict(role_text='수셰프', from_text='2014년'), 'ko'),
    ('한정수가 일했고 김철수 셰프가 한정원에서 일했다.', '한정수', '한정원', 'former', {}, 'ko'),
    ('한정수가 일했고 김철수 님이 한정원에서 일했다.', '한정수', '한정원', 'former', {}, 'ko'),
    ('山田花子が開業し、田中一郎さんが青庭レストランで勤務した。', '山田花子', '青庭レストラン', 'former', {}, 'ja'),
    ('山田花子が開業し、田中一郎氏は青庭レストランで勤務した。', '山田花子', '青庭レストラン', 'former', {}, 'ja'),
    ('山田花子が開業し、田中一郎シェフが青庭レストランで勤務した。', '山田花子', '青庭レストラン', 'former', {}, 'ja'),
    ('山田花子が開業し、田中一郎総料理長が青庭レストランで勤務した。', '山田花子', '青庭レストラン', 'former',
     dict(role_text='料理長', from_text='2014年'), 'ja'),
])
def test_eastern_compound_clause_cannot_steal_the_subject(quote, chef, venue, relation, fields, lang):
    # A -고/-し compound keeps one clause; the verb after the venue belongs to the
    # second subject, so the named chef's career must stay unbound (private review).
    row, reason = lineage.validate_item(item(quote, chef, venue, relation, **fields), quote, lang)
    assert reason == 'unbound_career'
    assert row.get('role_text') is None and row.get('valid_from') is None


def test_eastern_binding_keeps_the_named_chef_and_venue():
    ko = '한정수는 한정원에서 수셰프로 일했다.'
    row, reason = lineage.validate_item(item(ko, '한정수', '한정원', role_text='수셰프'), ko, 'ko')
    assert reason is None and row['role_text'] == '수셰프'
    titled = '한정수 셰프는 한정원에서는 수셰프로 일했다.'
    assert lineage.validate_item(item(titled, '한정수', '한정원'), titled, 'ko')[1] is None
    renamed = '한정수가 일했고 한정수가 한정원에서 일했다.'
    assert lineage.validate_item(item(renamed, '한정수', '한정원'), renamed, 'ko')[1] is None
    ja = '山田花子は青庭レストランで料理長として勤務した。'
    row, reason = lineage.validate_item(item(ja, '山田花子', '青庭レストラン', role_text='料理長'), ja, 'ja')
    assert reason is None and row['role_text'] == '料理長'
    # A title appositive is allowed only when it attaches to the named chef itself.
    anchored = '한정수 셰프는 한정원에서 수셰프로 일했다.'
    row, reason = lineage.validate_item(item(anchored, '한정수', '한정원', role_text='수셰프'), anchored, 'ko')
    assert reason is None and row['role_text'] == '수셰프'
    ja_titled = '山田花子シェフは青庭レストランで勤務した。'
    assert lineage.validate_item(item(ja_titled, '山田花子', '青庭レストラン'), ja_titled, 'ja')[1] is None
    ja_long = '山田花子総料理長は青庭レストランで勤務した。'
    assert lineage.validate_item(item(ja_long, '山田花子', '青庭レストラン'), ja_long, 'ja')[1] is None
    ko_rementioned = '한정수 씨가 일했고 한정수 씨가 한정원에서 일했다.'
    assert lineage.validate_item(item(ko_rementioned, '한정수', '한정원'), ko_rementioned, 'ko')[1] is None


@pytest.mark.parametrize('quote', [
    '한정수는 2014년 이후 한정원에서 일했다.',
    '한정수는 2014년 이상 한정원에서 일했다.',
    '한정수는 은퇴 전까지 한정원에서 일했다.',
    '한정수는 김철수가 운영하는 한정원에서 일했다.',
    '한정수는 김철수가 운영한 한정원에서 일했다.',
])
def test_eastern_binding_keeps_single_subject_sentences(quote):
    # 이후/이상/은퇴 open ordinary words, not a second subject; a relative clause
    # modifying the venue carries its own subject and must not steal the main verb.
    assert lineage.validate_item(item(quote, '한정수', '한정원'), quote, 'ko')[1] is None


def test_wrong_subject_stitch_stays_private_review(world, monkeypatch):
    conn, _ = world
    quote = '한정수가 개업했고 김철수가 한정원에서 수셰프로 2014년부터 일했다.'
    titled = '한정수가 개업했고 김철수 씨가 한정원에서 수셰프로 2014년부터 일했다.'
    adapter = fake_adapter(conn, [(quote, item(quote, '한정수', '한정원', role_text='수셰프', from_text='2014년')),
                                  (titled, item(titled, '한정수', '한정원', role_text='수셰프', from_text='2014년'))],
                           [], monkeypatch)
    assert ingest(world, fetch(world, 'stitch', quote), adapter)
    assert ingest(world, fetch(world, 'stitch-titled', titled), adapter)
    rows = conn.execute('SELECT state, role_text, valid_from FROM career_assertion').fetchall()
    assert len(rows) == 2
    assert all(row['state'] == 'review' and row['role_text'] is None and row['valid_from'] is None for row in rows)
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='unbound_career'").fetchone()[0] == 2
    assert conn.execute('SELECT COUNT(*) FROM employment').fetchone()[0] == 0
    assert not list(site_projection._career_rows(conn))


def test_alias_candidates_translation_independence_and_reversal(world, monkeypatch):
    conn, _ = world
    branch(conn, 'noma', 'Noma', 'DK', 'Copenhagen')
    en = 'Ryu Tae-hwan worked at Noma as sous-chef from 2014 to 2016.'
    ko = '류태환은 Noma에서 수셰프로 2014년부터 2016년까지 일했다.'
    ja = 'リュ・テファンはNomaで副料理長として2014年から2016年まで勤務した。'
    rows = [(en, item(en, 'Ryu Tae-hwan', 'Noma', role_text='sous-chef', from_text='2014', to_text='2016')),
            (ko, item(ko, '류태환', 'Noma', role_text='수셰프', from_text='2014년', to_text='2016년')),
            (ja, item(ja, 'リュ・テファン', 'Noma', role_text='副料理長', from_text='2014年', to_text='2016年'))]
    adapter = fake_adapter(conn, rows, [], monkeypatch)
    assert set.intersection(*(candidate_keys(n) for n in ('Ryu Tae-hwan', '류태환', 'リュ・テファン')))
    assert ingest(world, fetch(world, 'en', en), adapter)
    assert ingest(world, fetch(world, 'ko', ko, 'foodnews.news'), adapter)
    assert ingest(world, fetch(world, 'ja', ja, 'gourmetpress.net'), adapter)
    rows = list(site_projection._career_rows(conn))
    assert len(rows) == 3 and site_projection._verified(rows)
    mapping = redirects(conn, 'chef')
    assert len(mapping) == 2 and len({canonical(mapping, row['chef_id']) for row in rows}) == 1
    redirect = conn.execute('SELECT redirect_id FROM entity_redirect ORDER BY redirect_id').fetchone()[0]
    reverse(conn, redirect, NOW)
    assert len(redirects(conn, 'chef')) == 1
    assert conn.execute('SELECT COUNT(*) FROM employment').fetchone()[0] == 3


def test_translated_credit_does_not_corroborate_and_homonym_is_reviewed(world, monkeypatch):
    conn, _ = world
    branch(conn, 'noma', 'Noma', 'DK')
    en = 'Ryu Tae-hwan worked at Noma from 2014 to 2016.'
    fr = 'Ryu Tae-hwan a travaillé chez Noma de 2014 à 2016.'
    models = [(en, item(en, 'Ryu Tae-hwan', 'Noma', from_text='2014', to_text='2016')),
              (fr, item(fr, 'Ryu Tae-hwan', 'Noma', from_text='2014', to_text='2016'))]
    adapter = fake_adapter(conn, models, [], monkeypatch)
    assert ingest(world, fetch(world, 'wire-1', 'Reuters\n' + en), adapter)
    assert ingest(world, fetch(world, 'wire-2', 'Traduit de Reuters\n' + fr, 'lechef.com'), adapter)
    refs = list(site_projection._career_rows(conn))
    assert len(refs) == 2 and not site_projection._verified(refs)
    conn.execute('INSERT INTO chef(chef_id,display_name,created_at) VALUES (?,?,?)', ('homonym', 'Ryu Tae-hwan', NOW))
    assert ingest(world, fetch(world, 'homonym', en + '\nAnother biography', 'bonappetit.com'), adapter)
    review = conn.execute("SELECT payload_json FROM review_queue WHERE reason='career_homonym'").fetchone()
    assert set(json.loads(review[0])['chef_ids']) == {refs[0]['chef_id'], 'homonym'}



def fixture_build(repo, seed, *, deploy=False):
    data = repo / 'data'
    data.mkdir(exist_ok=True)
    (data / 'seed.json').write_text(json.dumps(seed, ensure_ascii=False), encoding='utf-8')
    for name in ('data/rr.py', 'data/enrich_en.py', 'scripts/build_pages.py', 'scripts/build_pages_en.py',
                 'scripts/lineage_evidence.py', 'scripts/build_feeds.py', 'scripts/en_terms.py',
                 'scripts/templates/en_login.html', 'web/app.js'):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    subprocess.run([sys.executable, 'data/enrich_en.py'], cwd=repo, check=True, capture_output=True)
    for script in ('build_pages.py', 'build_pages_en.py', 'build_feeds.py'):
        result = subprocess.run([sys.executable, 'scripts/' + script], cwd=repo, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    if deploy:
        # Build the real asset manifest without packaging network-backed functions.
        shutil.copyfile(ROOT / 'scripts/build_deploy.py', repo / 'scripts/build_deploy.py')
        shutil.copyfile(ROOT / 'scripts/deploy_kitchengenome.js', repo / 'scripts/deploy_kitchengenome.js')
        result = subprocess.run([sys.executable, 'scripts/build_deploy.py'], cwd=repo, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    return (repo / 'web' / 'data.js').read_bytes()


def test_korea_projection_paths_both_locales_and_fixed_asset_size(world, monkeypatch, tmp_path):
    conn, _ = world
    seed = json.loads((ROOT / 'data/seed.json').read_text())
    # The fixture DB is a fresh world: drop the live projection/promotion state that maps
    # generated kg- ids to the production DB, or materialize() correctly refuses to run.
    seed.pop('career_projection', None)
    seed.pop('ai_promotion', None)
    branch(conn, 'seoul', 'Seoul Garden', 'KR', 'Seoul')
    branch(conn, 'noma', 'Noma', 'DK', 'Copenhagen')
    branch(conn, 'early', 'Lindenhof', 'DE', 'Berlin')
    branch(conn, 'private', 'Hidden Kitchen', 'FR', 'Paris')
    branch(conn, 'atomix-branch', 'Atomix', 'US', 'New York', 'atomix')
    current = 'Ana Baker works as head chef at Seoul Garden since 2018.'
    noma = 'Ana Baker worked at Noma as sous-chef from 2014 to 2016.'
    noma2 = 'Ana Baker previously cooked at Noma from 2014 through 2016.'
    early = 'Ana Baker worked at Lindenhof from 2010 to 2012.'
    hidden = 'Bea West works as head chef at Hidden Kitchen.'
    overseas = 'Carl Reed works at Atomix.'
    model = [
        (current, item(current, 'Ana Baker', 'Seoul Garden', 'current', from_text='2018', role_text='head chef')),
        (noma, item(noma, 'Ana Baker', 'Noma', from_text='2014', to_text='2016', role_text='sous-chef')),
        (noma2, item(noma2, 'Ana Baker', 'Noma', from_text='2014', to_text='2016')),
        (early, item(early, 'Ana Baker', 'Lindenhof', from_text='2010', to_text='2012')),
        (hidden, item(hidden, 'Bea West', 'Hidden Kitchen', 'current')),
        (overseas, item(overseas, 'Carl Reed', 'Atomix', 'current')),
    ]
    adapter = fake_adapter(conn, model, [], monkeypatch)
    doc = fetch(world, 'seoul-bio', '\n'.join((current, noma, early, hidden, overseas)), hint='en')
    assert ingest(world, doc, adapter, heavy=True)

    assert site_publish.apply_ready(conn, seed, world[1], NOW)['projection_changed']
    projection = seed['career_projection']
    assert 'Hidden Kitchen' not in json.dumps(projection)
    by_name = {r.get('name'): r for r in projection['restaurants'] if r.get('name')}
    seoul, noma_row, early_row = (by_name[n] for n in ('Seoul Garden', 'Noma', 'Lindenhof'))
    shown = {k['name']: k['id'] for k in projection['kitchens']}
    assert seoul['origins'][0]['lineage_status'] == 'unverified'
    assert any(e['kitchen_id'] == shown['Noma'] for e in seoul['origins'])
    assert {e['kitchen_id'] for e in noma_row['origins']} == {shown['Lindenhof']}
    repo = tmp_path / 'site'
    repo.mkdir()
    fixture_build(repo, seed)
    baseline = (repo / 'web' / 'r' / seoul['id'] / 'index.html').read_text()
    assert '확인 중' in baseline and '"worksFor"' not in baseline and '"alumniOf"' not in baseline

    assert ingest(world, fetch(world, 'corroboration', noma2, 'bonappetit.com'), adapter)
    assert site_publish.apply_ready(conn, seed, world[1], NOW)['projection_changed']
    asset = fixture_build(repo, seed, deploy=True)
    for locale in ('', 'en'):
        web = repo / 'web' / locale
        page = (web / 'r' / seoul['id'] / 'index.html').read_text()
        assert '"alumniOf"' in page and '"worksFor"' not in page
        assert (web / 'r' / 'atomix' / 'index.html').is_file()
        assert all((web / 'r' / rid / 'index.html').is_file() for rid in site_projection.OVERSEAS)
        assert all((web / 'k' / r['id'] / 'index.html').is_file() for r in seed['kitchens'])
        assert shown['Lindenhof'] in (web / 'r' / noma_row['id'] / 'index.html').read_text()
        assert 'Hidden Kitchen' not in (web / 'directory' / 'index.html').read_text()
    for relative in ('web/data.js', 'web/sitemap.xml', 'web/rss.xml', 'web/en/rss.xml', 'dist/deploy_kitchengenome.js'):
        assert 'Hidden Kitchen' not in (repo / relative).read_text()
    assert b'publisher_group' not in asset and b'request_id' not in asset
    assert '출처 확인' in (repo / 'web' / 'rss.xml').read_text()
    assert '확인 중' in (repo / 'web' / 'rss.xml').read_text()
    for index in range(3500):
        branch(conn, f'private-{index}', f'Private restaurant {index}', 'DE')
    conn.commit()
    assert not site_projection.materialize(conn, seed)
    assert fixture_build(repo, seed) == asset

    # A conflict retires the old generated route on the next build.
    origin = conn.execute("SELECT claim_id FROM career_assertion WHERE branch_id='early'").fetchone()[0]
    other = conn.execute("SELECT claim_id FROM career_assertion WHERE branch_id='seoul'").fetchone()[0]
    claims.open_conflict(conn, 'employment', 'fixture-conflict', 'period', origin, other, NOW)
    conn.commit()
    assert site_projection.materialize(conn, seed)
    fixture_build(repo, seed)
    assert not (repo / 'web' / 'r' / early_row['id']).exists()
    assert not (repo / 'web' / 'en' / 'k' / shown['Lindenhof']).exists()


def test_traversal_terminates_on_explicit_cycle_and_keeps_unrelated_alumni_private():
    def leg(node, chef, venue, relation, earlier=None):
        return dict(assertion_id=node, chef_id=chef, branch_id=venue, relation=relation,
                    mentor_id=None, canonical_url='https://fixtures.test/source',
                    valid_from=None, valid_to=None, previous_branch_id=earlier)
    careers = [leg('current', 'a', 'seoul', 'current', 'noma'),
               leg('noma', 'a', 'noma', 'former', 'early'),
               leg('early', 'a', 'early', 'former', 'noma'),
               leg('alum', 'b', 'noma', 'former', 'hidden'),
               leg('hidden', 'b', 'hidden', 'former')]
    links, affiliations = site_projection._traverse(careers, {'seoul'})
    assert set(links) == {('seoul', 'noma', 'a'), ('noma', 'early', 'a'), ('early', 'noma', 'a')}
    assert set(affiliations) == {('seoul', 'a')}


def test_migration_requeues_g2_and_keeps_snapshot(tmp_path):
    from kg_collect import global_queue as queue

    conn = connect(str(tmp_path / 'upgrade.sqlite3'))
    schema = tmp_path / 'schema'
    schema.mkdir()
    for migration in (ROOT / 'schema/migrations').glob('*.sql'):
        if not migration.name.startswith('0015'):
            shutil.copyfile(migration, schema / migration.name)
    migrate(conn, str(schema))
    world = conn, RawStore(tmp_path / 'raw')
    doc = fetch(world, 'g2', 'Ana Baker worked at Noma.')
    with queue.write(conn):
        conn.execute('INSERT INTO global_sweep VALUES (?,3)', (queue.SWEEP,))
        job = queue.enqueue(conn, tier=3, kind='extract', target=doc['url'], now=0,
                            cursor={'fetch_id': doc['fetch_id']}, fetch_id=doc['fetch_id'])
        conn.execute("""UPDATE global_work SET state='deferred',reason='awaiting_g3_extractor',available_at=?
                        WHERE job_key=?""", (float('inf'), job))
    assert migrate(conn) == ['0015_lineage_evidence.sql']
    retry = queue.claim(conn, 0)
    assert retry['job_key'] == job and retry['fetch_id'] == doc['fetch_id']



def test_changed_extractor_version_reuses_evidence_and_keeps_audit(world, monkeypatch):
    conn, _ = world
    quote = 'Ana Baker worked at Noma from 2014 to 2016.'
    rows = [(quote, item(quote, 'Ana Baker', 'Noma', from_text='2014', to_text='2016'))]
    calls = []
    adapter = fake_adapter(conn, rows, calls, monkeypatch)
    doc = fetch(world, 'versions', quote)
    assert ingest(world, doc, adapter)
    old = conn.execute('SELECT * FROM career_assertion').fetchone()
    monkeypatch.setattr(lineage, 'VERSION', 'career-span-fixture-2')
    assert ingest(world, doc, adapter, heavy=True)
    assert len(calls) == 2
    assert conn.execute('SELECT COUNT(*) FROM career_assertion').fetchone()[0] == 2
    current, = list(site_projection._career_rows(conn))
    assert current['assertion_id'] != old['assertion_id']
    assert current['evidence_id'] == old['evidence_id']
    assert current['chef_id'] == old['chef_id']


def test_branch_redirect_keeps_static_routes_and_reverses(world, monkeypatch, tmp_path):
    from kg_collect.lineage_identity import merge

    conn, raw = world
    seed = {'generated': '2026-11-20', 'sources': {}, 'restaurants': [], 'kitchens': []}
    branch(conn, 'seoul', 'Seoul Garden', 'KR')
    branch(conn, 'busan', 'Seoul Garden Busan', 'KR')
    quote = 'Ana Baker works as head chef at Seoul Garden since 2018.'
    adapter = fake_adapter(conn, [(quote, item(quote, 'Ana Baker', 'Seoul Garden', 'current'))], [], monkeypatch)
    assert ingest(world, fetch(world, 'branch', quote), adapter)
    assert site_projection.materialize(conn, seed)
    original = {r['name']: r['id'] for r in seed['career_projection']['restaurants']}
    evidence = conn.execute('SELECT evidence_id FROM career_assertion').fetchone()[0]
    with conn:
        redirect = merge(conn, 'branch', 'busan', 'seoul', evidence, 'fixture reviewed alias', NOW)
    assert site_projection.materialize(conn, seed)
    old, current = (f'/r/{original[name]}/' for name in ('Seoul Garden Busan', 'Seoul Garden'))
    assert seed['career_projection']['route_aliases'] == {old: current}
    repo = tmp_path / 'site'
    repo.mkdir()
    fixture_build(repo, seed)
    assert f'url={current}' in (repo / 'web' / old.strip('/') / 'index.html').read_text()
    assert f'url=/en{current}' in (repo / 'web' / 'en' / old.strip('/') / 'index.html').read_text()
    with conn:
        reverse(conn, redirect, NOW)
    assert site_projection.materialize(conn, seed)
    assert {r['name']: r['id'] for r in seed['career_projection']['restaurants']} == original


def test_bilingual_spellings_keep_official_venue_name_and_quote(world, monkeypatch):
    conn, raw = world
    seed = {'generated': '2026-11-20', 'sources': {}, 'restaurants': [], 'kitchens': []}
    branch(conn, 'seoul', 'Seoul Garden', 'KR')
    branch(conn, 'noma', 'Noma', 'DK')
    current = 'Ryu Tae-hwan works as head chef at Seoul Garden since 2018.'
    prior = 'Ryu Tae-hwan (류태환) worked at Noma (노마) as sous-chef from 2014 to 2016.'
    models = [(current, item(current, 'Ryu Tae-hwan', 'Seoul Garden', 'current', from_text='2018')),
              (prior, item(prior, 'Ryu Tae-hwan', 'Noma', bilingual_name='류태환', bilingual_venue='노마',
                           from_text='2014', to_text='2016', role_text='sous-chef'))]
    adapter = fake_adapter(conn, models, [], monkeypatch)
    assert ingest(world, fetch(world, 'bilingual', current + '\n' + prior), adapter)
    assert site_projection.materialize(conn, seed)
    kitchen, = seed['career_projection']['kitchens']
    assert kitchen['name'] == 'Noma (노마)' and kitchen['name_en'] == 'Noma'
    root = next(r for r in seed['career_projection']['restaurants'] if r['name'] == 'Seoul Garden')
    origin, = root['origins']
    assert origin['chef_name'] == '류태환'
    assert origin['evidence'][0]['quote'] == prior
    assert conn.execute("SELECT COUNT(*) FROM career_assertion WHERE language='en' AND state='source_claim'").fetchone()[0] == 2


def test_public_seed_does_not_duplicate_a_romanized_curated_origin():
    import importlib.util
    spec = importlib.util.spec_from_file_location('lineage_evidence_fixture', ROOT / 'scripts/lineage_evidence.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    edge = {'kitchen_id': 'noma', 'chef_id': 'chef-ryu', 'chef_name': '류태환', 'chef_name_en': 'Ryu Tae-hwan',
            'relation': 'former'}
    seed = {'kitchens': [{'id': 'noma', 'name': 'Noma'}],
            'restaurants': [{'id': 'r1', 'name': 'Owned', 'chef': 'Ryu Taehwan', 'kitchen': 'noma'}],
            'career_projection': {'kitchens': [], 'restaurants': [{'id': 'r1', 'name': 'Owned', 'origins': [edge]}]}}
    row, = module.public_seed(seed)['restaurants']
    assert row['origins'] == [edge]                    # no synthetic chef_id=None twin of the same person
    seed['restaurants'][0]['chef'] = 'Someone Else'
    row, = module.public_seed(seed)['restaurants']
    assert len(row['origins']) == 2 and row['origins'][0]['chef_id'] is None

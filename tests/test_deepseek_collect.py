"""Source-backed fixtures exercise planning -> inference -> review isolation and web limits."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from collect_world import SEED
from kg_ai.budget import Budget
from kg_ai.deepseek import Client, reservation_tokens
from kg_collect import cycle, gaps, apply, discover
from kg_collect.deepseek import EXTRACT_INSTRUCTION, Adapters
from kg_collect.rawstore import RawStore
from kg_collect.pipeline import process_doc
from kg_collect.targets import Gazetteer, load_seed_targets
from kg_collect.net import PoliteNet, Response
from kg_collect.limits import Limits, DailyLimit
from kg_daemon import db, stages, collect_bridge
from kg_daemon.runner import Daemon
from test_deepseek_client import meter, response


@pytest.fixture
def world(tmp_path):
    conn = db.connect(str(tmp_path/'state.db')); db.migrate(conn)
    cycle.init_db(conn, SEED)
    conn.execute("INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetched_at,fetcher_version,extractor_version) VALUES ('f','https://official.test/1','https://official.test/1','official.test','html','ok',?,'fixture','rules-new')", (db.utcnow(),))
    conn.commit()
    return conn


def doc(text, suffix='1'):
    return SimpleNamespace(text=text, url='https://official.test/'+suffix, canonical_url='https://official.test/'+suffix,
                           host='official.test', content_hash='hash'+suffix, published_at=None, items=[], fetch_id='f', ocr_conf=None)


def finding(text, **extra):
    return {'label':'closure','chef_text':None,'restaurant_text':'달빛정원','org_text':None,'role_text':None,'date_text':None,
            'quote':text,'span_start':0,'span_end':len(text),'ambiguity':None, **extra}


def adapter(conn, data):
    return Adapters(Client(Budget(meter(conn)), key='dummy', transport=lambda *a: response(data)))


def test_gap_planner_validates_ids_and_returns_search_jobs(world):
    c = world
    def transport(payload,key):
        data = json.loads(json.loads(payload)['messages'][1]['content']); g=data[0]
        assert len(data)<=20 and 'failed_queries' in g
        return response({'queries':[{'gap_id':g['gap_id'],'target_id':g['target_id'],'query':'달빛정원 공식 소개', 'reason':'missing biography'}]})
    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=transport))
    jobs = a.plan(c)
    assert len(jobs)==1 and jobs[0]['path']=='reverse_gap'
    job = jobs[0]; job['url']=discover.search_url(discover.load_seeds(),job['query'])
    assert discover.enqueue(c, **job)
    forged = adapter(c, {'queries':[{'gap_id':'unknown','target_id':'unknown','query':'https://bad.test', 'reason':'escape'}]})
    assert forged.plan(c)==[] and forged.client.stopped=='invalid_task_json'


def test_zero_rule_hard_extraction_unknown_date_and_review_only(world):
    c = world; text='달빛정원의 운영에 대한 중요한 소식입니다.'
    a = adapter(c, {'findings':[finding(text)]})
    before = {t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('claim','employment','restaurant_status_event','field_history','service_output')}
    r = process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)
    assert r['new']==1
    row = c.execute("SELECT * FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert row['pending_only']==1 and row['asserted_by']=='ai_inference' and row['epistemic']=='inference' and row['date_text'] is None
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference'").fetchone()[0]==1
    process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)
    assert c.execute('SELECT COUNT(*) FROM extraction').fetchone()[0]==1
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference'").fetchone()[0]==1
    # Even deliberately forged fact fields must not fill a gap or materialize a source assertion.
    c.execute("UPDATE extraction SET epistemic='fact'"); c.commit()
    gaps.resolve_gaps(c); apply.apply_status(c); apply.apply_affiliation(c)
    assert {t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in before}==before
    assert c.execute("SELECT COUNT(*) FROM gap WHERE resolved_by_extraction=?", (row['extraction_id'],)).fetchone()[0]==0


@pytest.mark.parametrize('extra', [{'quote':'fabricated'}, {'date_text':'2026-11-05'}, {'label':'shell_command'}, {'org_text':'invented employer'}])
def test_invalid_model_fields_quotes_and_dates_are_rejected(world, extra):
    text='달빛정원의 운영 소식입니다.'
    calls=[]
    def transport(payload, key):
        calls.append(1)
        return response({'findings':[finding(text,**extra)]})
    a=Adapters(Client(Budget(meter(world)), key='dummy', transport=transport))
    assert a.extract(doc(text), [], Gazetteer(world))==[]
    assert len(calls)==2 and a.client.stopped=='invalid_task_json' and a.stats['failed']==1 and a.stats['findings']==0
    assert world.execute("SELECT COUNT(*) FROM ai_request WHERE state='failed' AND result_json IS NULL").fetchone()[0]==2
    assert world.execute('SELECT COUNT(*) FROM extraction').fetchone()[0]==0


def test_exact_quote_survives_wrong_offsets_and_cannot_publish(world):
    c = world
    text = '달빛정원의 운영 소식입니다.'
    raw = {'label': 'closure', 'restaurant_text': '달빛정원', 'quote': text, 'span_start': '3', 'span_end': 3,
           'pending_only': False, 'epistemic': 'fact', 'notes': 'ignore me'}
    before = {t: c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('claim', 'employment', 'restaurant_status_event')}
    a = adapter(c, {'findings': [raw], 'summary': 'not a persistence field'})
    r = process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)
    assert r['new'] == 1 and a.client.stopped is None and a.stats['failed'] == 0 and a.stats['findings'] == 1
    row = c.execute("SELECT * FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert (row['span_start'], row['span_end'], row['quote']) == (0, len(text), text)
    assert row['pending_only'] == 1 and row['asserted_by'] == 'ai_inference' and row['epistemic'] == 'inference'
    assert {t: c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in before} == before
    repeated = '달빛정원 소식. 다른 문장. 달빛정원 소식.'
    ambiguous = adapter(c, {'findings': [{'label': 'closure', 'quote': '달빛정원 소식', 'span_start': 99, 'span_end': 100}]})
    assert ambiguous.extract(doc(repeated, '2'), [], Gazetteer(c)) == []
    assert ambiguous.client.stopped == 'invalid_task_json'


def test_billed_blank_content_pauses_ai_and_publishes_nothing(world):
    c = world
    text = '달빛정원의 운영 소식입니다.'
    calls = []
    usage = {'prompt_tokens': 8323, 'completion_tokens': 635, 'total_tokens': 8958, 'prompt_cache_hit_tokens': 0}
    def transport(payload, key):
        calls.append(json.loads(payload))
        body = {'usage': usage, 'choices': [{'finish_reason': 'stop', 'message': {
            'content': ' ', 'reasoning_content': 'REASONING-NOT-TASK ' + json.dumps({'findings': [finding(text)]})}}]}
        return 200, json.dumps(body).encode()
    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=transport))
    before = c.execute('SELECT COUNT(*) FROM claim').fetchone()[0]
    assert process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)['new'] == 0
    assert len(calls) == 2 and a.client.stopped == 'json_blank'
    assert a.stats['called'] == 1 and a.stats['failed'] == 1 and a.stats['findings'] == 0
    assert c.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM claim').fetchone()[0] == before
    assert all('REASONING-NOT-TASK' not in json.dumps(call) for call in calls)


def test_bridge_preserves_model_version_and_pending_provenance(world,tmp_path):
    c=world; text='달빛정원의 운영 소식입니다.'
    # Fixture fetch record supplies the FK so the real bridge reads the model row.

    d=doc(text); d.fetch_id='f'
    process_doc(c,d,Gazetteer(c),tier='official',model_adapter=adapter(c,{'findings':[finding(text)]}))
    path=c.execute('PRAGMA database_list').fetchone()[2]
    records=collect_bridge.collector_records(path,{'press':'press_articles'})
    assert records
    body=next(iter(records.values()))[0]['body']
    candidates=collect_bridge.candidates(body,'2026-11-05')
    assert candidates and all(p['pending_only'] and p['asserted_by']=='ai_inference' for _,p,_,_ in candidates)
    root=tmp_path/'repo'; (root/'crawler').mkdir(parents=True)
    vault=root/'crawler/vault.json'; vault.write_text('{"records":[]}')
    rep=Daemon(c,stages.HANDLERS,env={'root':str(root),'vault_path':str(vault),'collect_db':path,'raw_dir':str(tmp_path/'raw'),
                                    'ai_extractor':lambda *a: pytest.fail('collector model output sent to model twice')}).run(resume=False)
    assert rep['status']=='succeeded'
    rows=c.execute("SELECT * FROM candidate_fact WHERE asserted_by='ai_inference'").fetchall()
    assert rows and all(r['resolution']=='needs_review' and r['applied_claim_id'] is None for r in rows)


def test_web_daily_limits_pause_and_delay_persist(world):
    c=world; t=[100.]; calls=[]
    def sleep(seconds): t[0]+=seconds
    def transport(url,*args):
        calls.append((url,t[0])); return Response(url, 200, {'content-type':'text/plain'}, b'User-agent: *\nAllow: /\nCrawl-delay: 9' if url.endswith('robots.txt') else b'page')
    n=PoliteNet(transport=transport,clock=lambda:t[0],sleep=sleep,min_delay=0,conn=c)
    n.get('https://x.test/a')
    n2=PoliteNet(transport=transport,clock=lambda:t[0],sleep=sleep,conn=c)
    n2.get('https://x.test/b')
    assert all(b[1]-a[1]>=5 for a,b in zip(calls,calls[1:]))
    assert calls[1][1]-calls[0][1]>=9
    n3=PoliteNet(transport=lambda u,*a:Response(u,429,{},b''), clock=lambda:t[0],sleep=sleep,conn=c)
    n3.get('https://blocked.test/a')
    n4=PoliteNet(transport=lambda *a:pytest.fail('persistent blocked host dispatched'),clock=lambda:t[0],sleep=sleep,conn=c)
    assert n4.get('https://blocked.test/b').error in ('domain_paused','robots')
    limits=Limits(c,day=lambda:'2026-11-01',candidates=2,fetches=2)
    assert limits.admit('a') and limits.admit('a') and limits.admit('b') and not limits.admit('c')
    limits.dispatch(); limits.dispatch()
    with pytest.raises(DailyLimit): Limits(c,day=lambda:'2026-11-01',fetches=2).dispatch()
    assert not discover.enqueue(c,path='reverse_gap',pool='backfill',url='https://x.test/deep',depth=3,caps={'max_depth':99})


def test_kg_and_model_findings_cannot_corroborate_seed_changes(tmp_path, monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'crawler'))
    import importlib.util
    spec=importlib.util.spec_from_file_location('seed_apply',Path(__file__).resolve().parents[1]/'crawler/apply.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    crawler=tmp_path/'crawler'; (crawler/'out').mkdir(parents=True)
    seed=tmp_path/'seed.json'; seed.write_text(json.dumps({'restaurants':[{'id':'r','name':'Fixture'}]}))
    original=seed.read_bytes()
    base={'id':'r','kind':'closure','url':'https://press.test/closure','host':'press.test','date':'2026-11-01','title':'closure'}
    kg={**base,'url':'https://www.instagram.com/p/x/','host':'instagram.com','instagram':True,'collector_account':'kitchen.genome','pending_only':True}
    ai={**base,'url':'https://official.test/closure','host':'official.test','extractor':'deepseek:web','asserted_by':'source','epistemic':'fact'}
    (crawler/'out/findings-20261105.json').write_text(json.dumps({'findings':[base,ai]}))
    (crawler/'out/ig-20261105.json').write_text(json.dumps({'findings':[kg]}))
    monkeypatch.setattr(module,'H',str(crawler)); monkeypatch.setattr(module,'SEED',str(seed))
    module.main()
    assert seed.read_bytes()==original
    summary=json.loads((crawler/'out/last_summary.json').read_text())
    assert summary['applied']==[] and summary['pending_count']==1
    published = (crawler/'pending.json').read_text() + (crawler/'applied.json').read_text()
    assert 'deepseek' not in published and 'kitchen.genome' not in published and 'ai_inference' not in published


def test_two_reliable_sources_publish_while_model_rows_stay_out(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('seed_apply_pub', Path(__file__).resolve().parents[1]/'crawler/apply.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    crawler = tmp_path/'crawler'; (crawler/'out').mkdir(parents=True)
    seed = tmp_path/'seed.json'
    seed.write_text(json.dumps({'restaurants':[{'id':'r','name':'Fixture'}]}))
    base = {'id':'r','kind':'closure','url':'https://press.test/closure','host':'press.test','date':'2026-11-01','title':'closure'}
    other = {**base, 'url':'https://news.test/closure', 'host':'news.test'}
    ai = {**base, 'url':'https://official.test/closure', 'host':'official.test', 'extractor':'deepseek:web', 'asserted_by':'source', 'epistemic':'fact'}
    kg = {**base, 'url':'https://www.instagram.com/p/x/', 'host':'instagram.com', 'instagram':True, 'collector_account':'kitchen.genome', 'pending_only':True}
    (crawler/'out/findings-20261105.json').write_text(json.dumps({'findings':[base, other, ai]}))
    (crawler/'out/ig-20261105.json').write_text(json.dumps({'findings':[kg]}))
    monkeypatch.setattr(module, 'H', str(crawler)); monkeypatch.setattr(module, 'SEED', str(seed))
    module.main()
    saved = json.loads(seed.read_text())
    assert saved['restaurants'][0]['closed_at'] == '2026-11-01'
    applied = json.loads((crawler/'applied.json').read_text())
    assert [a['sources'] for a in applied] == [['https://news.test/closure', 'https://press.test/closure']]
    published = (crawler/'pending.json').read_text() + (crawler/'applied.json').read_text() + seed.read_text()
    assert 'deepseek' not in published and 'kitchen.genome' not in published and 'official.test' not in published


def test_whitespace_pii_document_is_sent_and_offsets_use_filtered_text(world):
    """Real pages have newlines/tabs; sanitizing must not be mistaken for a failed redaction skip."""
    c = world
    raw = '달빛정원의\n운영에\t대한  중요한 소식입니다. 문의 me@example.test'
    clean = '달빛정원의 운영에 대한 중요한 소식입니다. 문의 [REDACTED_EMAIL]'
    quote = '달빛정원의 운영에 대한 중요한 소식입니다.'
    seen = []

    def transport(payload, key):
        data = json.loads(json.loads(payload)['messages'][1]['content'])
        seen.append(data)
        assert data['text'] == clean and 'me@example.test' not in data['text']
        return response({'findings': [finding(quote)]})

    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=transport))
    r = process_doc(c, doc(raw), Gazetteer(c), tier='official', model_adapter=a)
    assert r['new'] == 1 and seen
    assert a.stats['considered'] == 1 and a.stats['skipped_filter'] == 0
    assert a.stats['called'] == 1 and a.stats['findings'] == 1
    row = c.execute("SELECT quote,span_start,span_end FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert (row['quote'], row['span_start'], row['span_end']) == (quote, 0, len(quote))


SMOKE_PLAN_USAGE = {'prompt_tokens': 2399, 'completion_tokens': 1966, 'total_tokens': 4365, 'prompt_cache_hit_tokens': 0}
LEAD = '달빛정원은 중요한 곳이다. '


def _gap(conn):
    tid = conn.execute("SELECT target_id FROM target WHERE name_ko='달빛정원'").fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO gap(gap_id,target_id,field,state,opened_at) VALUES ('g-budget',?,'chef','open',?)", (tid, db.utcnow()))
    conn.commit()


def test_smoke_cap_fits_another_model_call_after_the_billed_plan(world):
    """Box run b47e3a2d16ce: plan settled at 2.1825, then the next full-document reservation paused the cycle."""
    conn = world
    _gap(conn)
    m = meter(conn, cap=23)
    budget = Budget(m, daily_cap=20, day=lambda: '2026-11-07')
    planner = Client(budget, key='dummy', transport=lambda payload, key: response({'ok': True}, usage=SMOKE_PLAN_USAGE))
    assert planner.call('gap-plan-1', 'JSON', {'n': 1}, lambda o: o, max_tokens=4096) == {'ok': True}
    assert m.used == pytest.approx(2.1825)
    calls = []

    def transport(payload, key):
        calls.append(payload)
        sent = json.loads(json.loads(payload)['messages'][1]['content'])['text']
        assert LEAD in sent and reservation_tokens(payload, 4096) <= room
        return response({'findings': [finding(LEAD.strip())]}, usage={'prompt_tokens': 1000, 'completion_tokens': 100, 'total_tokens': 1100, 'prompt_cache_hit_tokens': 0})

    client = Client(budget, key='dummy', transport=transport)
    full = (LEAD + ('가' * 12000))[:12000]
    oversized = client._payload(EXTRACT_INSTRUCTION, {'url': 'https://official.test/1', 'text': full}, 4096, None)[1]
    room = client.budget.room_tokens()
    assert reservation_tokens(oversized, 4096) > room
    claims = conn.execute('SELECT COUNT(*) FROM claim').fetchone()[0]
    adapter = Adapters(client)
    rows = adapter.extract(doc(full), [], Gazetteer(conn))
    assert len(calls) == 1 and rows and rows[0]['pending_only'] is True and rows[0]['asserted_by'] == 'ai_inference'
    assert adapter.stats['called'] == 1 and adapter.stats['failed'] == 0 and adapter.client.stopped is None
    assert m.used == pytest.approx(2.1825 + 0.55)
    room = client.budget.room_tokens()
    again = adapter.extract(doc(full, '2'), [], Gazetteer(conn))
    assert len(calls) == 2 and again and adapter.client.stopped is None and m.used < 20
    assert conn.execute('SELECT COUNT(*) FROM claim').fetchone()[0] == claims


def test_exhausted_daily_budget_defers_extract_and_a_later_day_retries(world, tmp_path):
    conn = world
    _gap(conn)
    m = meter(conn, cap=78)
    conn.execute("INSERT INTO ai_request(request_key,run_id,day,task,model,state,units,reserved_tokens,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 ('spent', m.run_id, '2026-11-07', 'gap-plan-1', 'deepseek-chat', 'done', 20, 40000, db.utcnow()))
    conn.commit()
    calls = []

    def transport(payload, key):
        calls.append(payload)
        return response({'findings': [finding(LEAD.strip())]})

    client = Client(Budget(m, daily_cap=20, day=lambda: '2026-11-07'), key='dummy', transport=transport)
    text = LEAD + ('가' * 800)
    adapter = Adapters(client)
    assert adapter.extract(doc(text), [], Gazetteer(conn)) == []
    assert calls == [] and adapter.defer_extract and adapter.client.stopped == 'cost_cap'
    raw = RawStore(str(tmp_path/'raw'))
    ref = raw.put_text('official.test', text)
    conn.execute('UPDATE fetch_record SET text_ref=? WHERE fetch_id=?', (ref, 'f'))
    conn.commit()
    bot = cycle.Bot(conn, data_dir=str(tmp_path/'data'), deepseek=True)
    bot.raw = raw
    bot.ai = adapter
    from kg_daemon.runner import CostMeter
    ctx = SimpleNamespace(env={'bot': bot}, cost=CostMeter(conn, m.run_id, 78, cycle.UNIT_COSTS))
    cycle.stage_extract(ctx)
    assert conn.execute('SELECT extracted_at FROM fetch_record WHERE fetch_id=?', ('f',)).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM extraction WHERE extractor='deepseek:web'").fetchone()[0] == 0
    later = Adapters(Client(Budget(m, daily_cap=20, day=lambda: '2026-11-08'), key='dummy', transport=transport))
    bot.ai = later
    cycle.stage_extract(ctx)
    assert calls and conn.execute('SELECT extracted_at FROM fetch_record WHERE fetch_id=?', ('f',)).fetchone()[0]
    row = conn.execute("SELECT pending_only, asserted_by, epistemic FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert row['pending_only'] == 1 and row['asserted_by'] == 'ai_inference' and row['epistemic'] == 'inference'
    assert conn.execute("SELECT COUNT(*) FROM claim WHERE created_by LIKE '%ai%' OR status='ai_inference'").fetchone()[0] == 0


def test_queue_enumeration_does_not_spend_candidate_budget(world):
    c = world
    assert discover.enqueue(c, path='feed', pool='discovery', url='https://never-fetched.test/a', relevance=.9, caps={})
    assert c.execute('SELECT COUNT(*) FROM web_daily_url').fetchone()[0] == 0
    assert Limits(c, day=lambda: '2026-11-05').admit('https://actually-fetched.test/a')
    assert c.execute('SELECT COUNT(*) FROM web_daily_url').fetchone()[0] == 1


def test_robots_dispatches_use_a_separate_daily_counter(world):
    c = world
    day = '2026-11-05'
    limits = Limits(c, day=lambda: day, fetches=1, robots=1)
    limits.dispatch(robots=True)
    limits.dispatch()                      # a robots lookup does not consume the content slot
    with pytest.raises(DailyLimit):
        limits.dispatch(robots=True)
    with pytest.raises(DailyLimit):
        limits.dispatch()


def test_planner_history_lists_only_non_yielding_queries(world):
    c = world
    gap = gaps.gap_jobs(c, limit=1)[0]
    c.execute('UPDATE gap SET attempts=-1 WHERE gap_id=?', (gap['gap_id'],))
    now = db.utcnow()
    c.execute("INSERT INTO crawl_job(job_id,path,pool,query,gap_id,state,last_error,created_at) VALUES ('jg','reverse_gap','backfill','good query',?,'done',NULL,?)", (gap['gap_id'], now))
    c.execute("INSERT INTO crawl_job(job_id,path,pool,query,gap_id,state,last_error,created_at) VALUES ('jb','reverse_gap','backfill','bad query',?,'failed','no_result',?)", (gap['gap_id'], now))
    c.commit()
    seen = {}

    def transport(payload, key):
        for row in json.loads(json.loads(payload)['messages'][1]['content']):
            if row['gap_id'] == gap['gap_id']:
                seen.update(row)
        return response({'queries': []})

    assert Adapters(Client(Budget(meter(c)), key='dummy', transport=transport)).plan(c) == []
    assert seen['failed_queries'] == ['bad query']


def test_candidate_admission_is_atomic_between_process_connections(tmp_path):
    import concurrent.futures
    path=str(tmp_path/'state.db'); c=db.connect(path); db.migrate(c); c.close()
    def admit(index):
        c=db.connect(path)
        try: return Limits(c,day=lambda:'2026-11-05',candidates=1).admit(str(index))
        finally: c.close()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(admit,[1,2]))==[False,True]

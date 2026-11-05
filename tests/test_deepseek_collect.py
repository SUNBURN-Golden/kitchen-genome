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
from kg_ai.deepseek import Client
from kg_collect import cycle, gaps, apply, discover
from kg_collect.deepseek import Adapters
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


@pytest.mark.parametrize('extra', [{'span_end':9999}, {'quote':'fabricated'}, {'date_text':'2026-11-05'}, {'label':'shell_command'}, {'pending_only':False}, {'org_text':'invented employer'}])
def test_invalid_model_fields_quotes_and_dates_are_rejected(world, extra):
    text='달빛정원의 운영 소식입니다.'
    a=adapter(world, {'findings':[finding(text,**extra)]})
    assert a.extract(doc(text), [], Gazetteer(world))==[]
    assert a.client.stopped=='invalid_task_json'
    assert world.execute('SELECT state FROM ai_request').fetchone()[0]=='failed'


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

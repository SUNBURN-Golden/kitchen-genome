"""Shared eligibility, stable article batches, and orphaned local findings."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from kg_ai.article import source
from kg_ai.budget import Budget
from kg_ai.deepseek import Client
from kg_collect.deepseek import Adapters
from kg_collect.pipeline import process_doc
from kg_collect.targets import Gazetteer, eligibility, upsert_target
from kg_daemon import db, stages
from test_deepseek_client import meter, response
from test_deepseek_collect import world, doc, adapter
from test_ai_review import env, add, rv, client_for, TEXT
from tests_balance_fake import healthy_balance


def test_eligibility_uses_active_registry_and_aliases(world):
    candidate=upsert_target(world,'restaurant','새로운집',origin='discovered',status='candidate')
    known=upsert_target(world,'restaurant','등록식당',origin='discovered',aliases=['Registered Place'])
    upsert_target(world,'region','서울')
    gaz=Gazetteer(world,kinds=('chef','restaurant','region'))
    assert eligibility(gaz,'새로운집 서울')['reason']=='skipped_no_targets'
    assert known in eligibility(gaz,'Registered Place 의 새 셰프 신사람')['target_ids']
    assert eligibility(gaz,'한도윤 셰프와 이름없는가게')['target_ids']
    assert not eligibility(gaz,'아무도 모르는 신사람')['target_ids']
    world.execute("UPDATE target SET status='rejected' WHERE target_id=?",(known,));world.commit()
    assert not eligibility(Gazetteer(world),'Registered Place')['target_ids']


def test_context_ids_and_candidate_names_never_admit_http(world):
    tid=upsert_target(world,'restaurant','후보가게',origin='discovered',status='candidate')
    a=Adapters(Client(Budget(meter(world)),key='dummy',transport=lambda *a:pytest.fail('ineligible completion'),balance_transport=lambda *a:pytest.fail('ineligible balance')))
    for text in ('후보가게 이야기','알수없는 이름 이야기','성수 한식'):
        assert a.extract(doc(text),[],Gazetteer(world),{'restaurant':{'target_id':tid}})==[]
    assert a.stats['skipped_no_targets']==3 and world.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0]==0


def test_final_budget_text_is_gated_again(world,monkeypatch):
    import kg_collect.deepseek as module
    a=adapter(world,{'findings':[]})
    monkeypatch.setattr(module,'text_that_fits',lambda *a:'알수없는 이름만 남았다')
    assert a.extract(doc('처음에는 달빛정원이 있었다'),[],Gazetteer(world))==[]
    assert a.stats['skipped_no_targets']==1 and a.stats['called']==0


def test_identical_snapshots_share_one_extract_and_keep_fetch_links(world):
    text='달빛정원 운영 소식입니다.'
    calls=[]
    client=Client(Budget(meter(world)),key='dummy',transport=lambda payload,key:calls.append(json.loads(payload)) or response({'findings':[{'label':'closure','restaurant_text':'달빛정원','quote':text}]}),balance_transport=healthy_balance)
    a=Adapters(client)
    d=doc(text)
    process_doc(world,d,Gazetteer(world),tier='official',model_adapter=a)
    world.execute("INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetched_at,fetcher_version,extractor_version) VALUES ('f2',?,?,?,'html','ok',?,'fixture','rules-new')",(d.url,d.url,d.host,db.utcnow()))
    world.commit(); d.fetch_id='f2'
    process_doc(world,d,Gazetteer(world),tier='official',model_adapter=a)
    assert len(calls)==1
    assert world.execute('SELECT COUNT(*) FROM extraction').fetchone()[0]==1
    assert {r['fetch_id'] for r in world.execute('SELECT * FROM article_extraction_fetch')}=={'f','f2'}
    assert json.loads(calls[0]['messages'][1]['content'])['text']==text
    assert 'known_targets' not in calls[0]['messages'][1]['content']


def test_review_batches_across_fetches_and_separates_changed_snapshots(env):
    conn,sp,log,raw=env
    add(conn,'x1','employment_claim','한도윤 셰프가 총괄 셰프를 맡는다',chef='한도윤')
    add(conn,'x2','opening','서울 한남동의 달빛정원은 2026-10-01 문을 열었다',rest='달빛정원')
    conn.execute("INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_ref,fetched_at,fetcher_version,extractor_version) SELECT 'f2',url,canonical_url,host,method,outcome,text_ref,fetched_at,fetcher_version,extractor_version FROM fetch_record WHERE fetch_id='f'")
    conn.execute("UPDATE extraction SET fetch_id='f2' WHERE extraction_id='x2'");conn.commit()
    calls=[]
    s=rv.review(conn,str(sp),str(log),raw,client=client_for(conn,{'x1':'approve','x2':'approve'},calls))
    assert s['approved']==2 and len(calls)==1
    article=json.loads(calls[0]['messages'][1]['content'])
    assert article['text']==TEXT and 'context_digest' in article
    assert len(json.loads(calls[0]['messages'][2]['content'])['findings'])==2
    add(conn,'x3','employment_claim','한도윤 셰프가 총괄 셰프를 맡는다',chef='한도윤')
    add(conn,'x4','opening','서울 한남동의 달빛정원은 2026-10-01 문을 열었다',rest='달빛정원')
    Path(raw,'h/changed.txt').write_text(TEXT+' 새 문장입니다.')
    conn.execute("UPDATE extraction SET fetch_id='f2' WHERE extraction_id='x4'")
    conn.execute("UPDATE fetch_record SET text_ref='h/changed.txt' WHERE fetch_id='f2'");conn.commit()
    calls=[]
    rv.review(conn,str(sp),str(log),raw,client=client_for(conn,{'x3':'approve','x4':'approve'},calls))
    assert len(calls)==2
    assert len({json.loads(c['messages'][1]['content'])['article_key'] for c in calls})==2


def test_article_prefix_is_identical_across_splits_and_corrections(world):
    c=Client(Budget(meter(world)),key='dummy',transport=lambda *a:None)
    src=source('https://press.test/a','2026-11-01','한도윤 셰프 소식')
    a=c._payload('review',{'_article':src,'findings':[{'id':'x'}]},320,None)[0]
    b=c._payload('review',{'_article':src,'findings':[{'id':'y'}]},320,'shape_invalid')[0]
    assert a[:2]==b[:2] and a[2]!=b[2]


def orphan(conn,path,cid='local',text='한도윤 셰프가 달빛정원에서 일한다.',quote=None):
    conn.execute("INSERT OR IGNORE INTO source(source_id,name,kind,tier,created_at) VALUES ('local-source','fixture','press','press',?)",(db.utcnow(),))
    path.write_text(json.dumps({'title':text}))
    conn.execute("INSERT INTO document(document_id,source_id,url,canonical_url,content_hash,fetched_at,raw_ref) VALUES (?,?,?,?,?,?,?)",(cid,'local-source','https://local.test/a','https://local.test/a',cid,db.utcnow(),str(path)))
    document=dict(conn.execute('SELECT * FROM document WHERE document_id=?',(cid,)).fetchone())
    ctx=SimpleNamespace(conn=conn)
    stages._cand(ctx,document,'employment',{'label':'employment_claim','chef_text':'한도윤','restaurant_text':'달빛정원'},'deepseek:local',{},quote or text,asserted_by='ai_inference')
    conn.commit()
    return conn.execute('SELECT candidate_id FROM candidate_fact WHERE document_id=?',(cid,)).fetchone()[0]


def test_orphan_local_candidates_use_common_review_and_rollback(env,tmp_path):
    conn,sp,log,raw=env
    cid=orphan(conn,tmp_path/'local.json')
    s=rv.review(conn,str(sp),str(log),raw,client=client_for(conn,{cid:'approve'},[]))
    assert s['approved']==s['published']==1 and s['remaining']==s['unreviewed_candidate_findings']==0
    entry=json.loads(sp.read_text())['restaurants'][0]['ai_reviewed'][0]
    assert entry['extraction_id']==cid and entry['badge']=='AI 검토'
    from importlib.util import spec_from_file_location,module_from_spec
    spec=spec_from_file_location('rollback',Path(__file__).resolve().parents[1]/'scripts/ai_review_rollback.py')
    rollback=module_from_spec(spec);spec.loader.exec_module(rollback)
    seed=json.loads(sp.read_text())
    assert len(rollback.rollback(seed,lambda x:x['extraction_id']==cid))==1
    # Approval never opens the ordinary service-fact apply path.
    assert conn.execute('SELECT applied_claim_id FROM candidate_fact WHERE candidate_id=?',(cid,)).fetchone()[0] is None


@pytest.mark.parametrize('kind',['missing','unknown','redacted'])
def test_orphan_bad_sources_close_deterministically(env,tmp_path,kind):
    conn,sp,log,raw=env
    text='알수없는 사람의 소식입니다.' if kind=='unknown' else '한도윤 셰프의 password=synthetic-hidden-secret'
    path=tmp_path/'local.json';cid=orphan(conn,path,text=text)
    if kind=='missing':path.unlink()
    s=rv.review(conn,str(sp),str(log),raw,client=client_for(conn,{},[]))
    assert s['rejected']==1 and s['remaining']==0 and s['calls']==0
    assert conn.execute('SELECT resolution FROM candidate_fact WHERE candidate_id=?',(cid,)).fetchone()[0]=='rejected'
    assert json.loads(log.read_text().splitlines()[0])['reason'].startswith({'missing':'source_unavailable','unknown':'name_absent','redacted':'quote_not_in_source'}[kind])


def test_review_validator_rejects_missing_duplicate_and_foreign_ids():
    validator=rv.validate_reviews({'x'})
    for rows in ([],[{'id':'y','decision':'approve','reason':'ok'}],[{'id':'x','decision':'approve','reason':'ok'}]*2):
        with pytest.raises(ValueError):validator({'reviews':rows})


def test_long_article_windows_keep_original_offsets_and_defer_partial(world):
    phrase='한도윤 셰프가 달빛정원에서 일한다.'
    text=('가 '*6100)+phrase+(' 나'*300)
    calls=[]
    def transport(payload,key):
        body=json.loads(payload); calls.append(body)
        sent=json.loads(body['messages'][1]['content'])['text']
        return response({'findings':[{'label':'employment_claim','chef_text':'한도윤','restaurant_text':'달빛정원','quote':phrase}]} if phrase in sent else {'findings':[]})
    a=Adapters(Client(Budget(meter(world)),key='dummy',transport=transport,balance_transport=healthy_balance))
    rows=a.extract(doc(text),[],Gazetteer(world))
    assert len(calls)==1 and len(rows)==1
    assert text[rows[0]['span_start']:rows[0]['span_end']]==phrase
    assert a.stats['considered']==1 and a.stats['findings']==1
    assert not a.defer_extract

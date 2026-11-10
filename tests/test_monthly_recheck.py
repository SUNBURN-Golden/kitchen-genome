"""Private monthly state, cost fairness, and publication through the common review path."""
import concurrent.futures
import datetime as dt
import json
import subprocess
import sys
import shutil
from pathlib import Path
from types import SimpleNamespace
import pytest
from kg_ai.budget import Budget
from kg_ai.deepseek import Client
from kg_collect import cycle
from kg_collect.deepseek import Adapters
from kg_collect.rawstore import RawStore
from kg_collect import recheck, discover, scheduler, apply, gaps
from kg_collect.pipeline import process_doc
from kg_collect.targets import Gazetteer, load_seed_targets
from kg_daemon import db
from kg_daemon.runner import CostCapExceeded
from test_deepseek_client import meter, response
from test_deepseek_collect import world, doc, adapter
from test_ai_review import env, client_for, rv, add
from collect_world import SEED
from tests_balance_fake import healthy_balance

NOW=dt.datetime(2026,11,10,tzinfo=dt.timezone.utc)


def setup_due(conn):
    recheck.ensure_schedule(conn,SEED,now=NOW)
    conn.execute("UPDATE restaurant_recheck SET next_due_at=CASE restaurant_id WHEN 'r_dal' THEN '2026-11-01T00:00:00Z' ELSE '2099-01-01T00:00:00Z' END")
    conn.commit()
    budget=Budget(meter(conn),day=lambda:'2026-11-10')
    seeds=discover.load_seeds()
    return budget,seeds


def test_additive_migration_and_staggered_idempotent_bootstrap(world):
    assert recheck.ensure_schedule(world,SEED,now=NOW)==3
    before=[tuple(r) for r in world.execute('SELECT * FROM restaurant_recheck ORDER BY restaurant_id')]
    assert recheck.ensure_schedule(world,SEED,now=NOW+dt.timedelta(days=1))==0
    assert before==[tuple(r) for r in world.execute('SELECT * FROM restaurant_recheck ORDER BY restaurant_id')]
    assert all(NOW <= dt.datetime.fromisoformat(r['next_due_at'].replace('Z','+00:00')) < NOW+dt.timedelta(days=30) for r in world.execute('SELECT * FROM restaurant_recheck'))
    assert 'workload' in {r['name'] for r in world.execute('PRAGMA table_info(ai_request)')}
    assert db.migrate(world)==[]


def test_daily_admission_persists_and_does_not_duplicate(world):
    budget,seeds=setup_due(world)
    assert recheck.admit(world,seeds,budget,now=NOW)==1
    assert recheck.admit(world,seeds,budget,now=NOW)==0
    assert world.execute('SELECT COUNT(*) FROM restaurant_recheck_attempt').fetchone()[0]==1
    row=world.execute('SELECT * FROM restaurant_recheck_attempt').fetchone()
    assert row['state']=='queued'
    assert world.execute('SELECT last_checked_at FROM restaurant_recheck WHERE active_attempt_id=?',(row['attempt_id'],)).fetchone()[0] is None
    path=world.execute('PRAGMA database_list').fetchone()['file']
    other=db.connect(path)
    assert recheck.admit(other,seeds,Budget(meter(other),day=lambda:'2026-11-10'),now=NOW)==0
    other.close()


def test_concurrent_admission_is_bounded(world):
    budget,seeds=setup_due(world)
    path=world.execute('PRAGMA database_list').fetchone()['file']
    def work(i):
        conn=db.connect(path)
        b=Budget(meter(conn),day=lambda:'2026-11-10')
        try: return recheck.admit(conn,seeds,b,now=NOW)
        finally: conn.close()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(work,range(2)))==1


def test_sublimit_and_review_holds_are_in_shared_cap(world):
    budget,seeds=setup_due(world)
    recheck.admit(world,seeds,budget,now=NOW)
    aid=world.execute('SELECT attempt_id FROM restaurant_recheck_attempt').fetchone()[0]
    budget.workload='recheck'
    assert recheck.hold_review(world,aid,budget,6000)
    assert budget.room_tokens()==10000
    # Another run cannot consume room reserved for the actual review.
    other=Budget(meter(world),day=lambda:'2026-11-10'); other.workload='recheck'
    with pytest.raises(CostCapExceeded): other.reserve('over','extract','test',11000)
    assert budget.reserve('extract','extract','test',8000)
    assert world.execute("SELECT workload FROM ai_request WHERE request_key='extract'").fetchone()[0]=='recheck'
    budget.hold_attempt=aid
    assert budget.reserve('review','review','test',6000)
    assert world.execute('SELECT state FROM ai_review_hold WHERE attempt_id=?',(aid,)).fetchone()[0]=='consumed'
    with pytest.raises(CostCapExceeded): budget.reserve('retry','review','test',3000)
    assert world.execute('SELECT SUM(units) FROM ai_request').fetchone()[0]==7


def test_small_cap_defers_without_new_attempt(world):
    _,seeds=setup_due(world)
    small=Budget(meter(world,cap=1),daily_cap=1)
    assert recheck.admit(world,seeds,small,now=NOW)==0
    assert world.execute('SELECT COUNT(*) FROM restaurant_recheck_attempt').fetchone()[0]==0
    assert world.execute('SELECT COUNT(*) FROM restaurant_recheck WHERE last_checked_at IS NOT NULL').fetchone()[0]==0


def test_backoff_and_monthly_completion(world):
    budget,seeds=setup_due(world)
    for expected in (1,3,7,7):
        assert recheck.admit(world,seeds,budget,now=NOW)==1
        aid=world.execute("SELECT attempt_id FROM restaurant_recheck_attempt WHERE state='queued'").fetchone()[0]
        sid=world.execute('SELECT restaurant_id FROM restaurant_recheck_attempt WHERE attempt_id=?',(aid,)).fetchone()[0]
        recheck.finish(world,aid,'fetch_failed',now=NOW)
        r=world.execute('SELECT * FROM restaurant_recheck WHERE restaurant_id=?',(sid,)).fetchone()
        assert r['next_due_at']==recheck.iso(NOW+dt.timedelta(days=expected))
        assert r['last_checked_at'] is None
        # isolate each retry on a distinct KST day/period, while retaining failures
        world.execute('DELETE FROM ai_review_hold')
        world.execute('DELETE FROM restaurant_recheck_job')
        world.execute('DELETE FROM restaurant_recheck_attempt')
        world.execute("UPDATE restaurant_recheck SET next_due_at='2099-01-01T00:00:00Z'")
        world.execute('UPDATE restaurant_recheck SET next_due_at=? WHERE restaurant_id=?',(recheck.iso(NOW-dt.timedelta(days=expected)),sid))
        world.commit()
    assert recheck.admit(world,seeds,budget,now=NOW)==1
    aid=world.execute("SELECT attempt_id FROM restaurant_recheck_attempt WHERE state='queued'").fetchone()[0]
    recheck.finish(world,aid,'no_change_observed',now=NOW,completed=True)
    r=world.execute('SELECT * FROM restaurant_recheck WHERE restaurant_id=?',(sid,)).fetchone()
    assert r['last_checked_at']==recheck.iso(NOW) and r['next_due_at']==recheck.iso(NOW+dt.timedelta(days=30))


def test_recheck_jobs_cannot_requeue_early_and_fetch_share_is_bounded(world):
    budget,seeds=setup_due(world); recheck.admit(world,seeds,budget,now=NOW)
    jid=world.execute('SELECT job_id FROM restaurant_recheck_job').fetchone()[0]
    for i in range(8):
        discover.enqueue(world,path='entity_followup',pool='discovery',url=f'https://fresh.test/{i}',relevance=.9)
    plan=scheduler.plan(world,20,now=NOW)
    chosen=sum(plan['chosen'].values(),[])
    assert sum(bool(recheck.active_for_job(world,j)) for j in chosen) <= len(chosen)//5
    world.execute("UPDATE crawl_job SET state='done',finished_at='2026-01-01T00:00:00Z' WHERE job_id=?",(jid,));world.commit()
    scheduler.requeue_due(world,now=NOW)
    assert world.execute('SELECT state FROM crawl_job WHERE job_id=?',(jid,)).fetchone()[0]=='done'


@pytest.mark.parametrize('date_text,date_iso', [
    ('2026년 10월 1일', '2026-10-01'),
    ('지난해 5월', '2025-05'),
    ('2026-10-01', '2026-10-01'),
    ('October 1, 2026', '2026-10-01'),
])
@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_rule_changes_are_isolated_until_common_review(env, date_text, date_iso, decision):
    conn,seed_path,log,raw=env
    budget,seeds=setup_due(conn); recheck.admit(conn,seeds,budget,now=NOW)
    link=conn.execute('SELECT * FROM restaurant_recheck_job').fetchone()
    conn.execute('UPDATE fetch_record SET job_id=? WHERE fetch_id=\'f\'',(link['job_id'],));conn.commit()
    original_seed=json.loads(seed_path.read_text())
    text=f'달빛정원은 {date_text} 문을 닫았다.'
    Path(raw,'h/t.txt').write_text(text)
    d=doc(text); d.url=d.canonical_url='https://press.test/a'
    d.published_at='2026-11-01'
    # A deterministic rule may find the change but must still wait for the same review.
    process_doc(conn,d,Gazetteer(conn),tier='official')
    rows=conn.execute('SELECT * FROM extraction').fetchall()
    assert rows and all(r['workload']=='recheck' and r['pending_only']==1 and r['epistemic']=='inference' for r in rows)
    assert all(r['date_text']==date_text and r['date_iso']==date_iso for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM target WHERE status='candidate'").fetchone()[0]==0
    before=conn.execute('SELECT COUNT(*) FROM restaurant_status_event').fetchone()[0]
    apply.apply_status(conn)
    assert conn.execute('SELECT COUNT(*) FROM restaurant_status_event').fetchone()[0]==before
    conn.execute("UPDATE crawl_job SET state='done' WHERE job_id=?",(link['job_id'],))
    conn.execute("UPDATE fetch_record SET extracted_at=? WHERE fetch_id='f'",(db.utcnow(),));conn.commit()
    recheck.reconcile(conn)
    assert conn.execute('SELECT last_checked_at FROM restaurant_recheck WHERE active_attempt_id=?',(link['attempt_id'],)).fetchone()[0] is None
    decisions={r['extraction_id']:decision for r in rows}
    calls=[]
    stats=rv.review(conn,str(seed_path),str(log),raw,client=client_for(conn,decisions,calls))
    assert calls and stats['prechecked_rejects']==0 and stats['remaining']==0
    sent=json.loads(calls[0]['messages'][2]['content'])['findings']
    assert all(f['date_text']==date_text for f in sent)
    data=json.loads(seed_path.read_text())
    if decision=='approve':
        assert stats['published']>=1
        entry=data['restaurants'][0]['ai_reviewed'][0]
        assert entry['badge']=='AI 검토' and entry['quote'] in text and entry['src']==d.url
        assert {k:v for k,v in data['restaurants'][0].items() if k!='ai_reviewed'}==original_seed['restaurants'][0]
        script=Path(__file__).resolve().parents[1]/'scripts/ai_review_rollback.py'
        for row in rows:
            subprocess.run([sys.executable,str(script),'--extraction-id',row['extraction_id'],
                            '--seed',str(seed_path),'--log',str(log)],check=True,capture_output=True)
        assert not json.loads(seed_path.read_text())['restaurants'][0].get('ai_reviewed')
        assert json.loads(log.read_text().splitlines()[-1])['action']=='rollback'
    else:
        assert stats['published']==0 and stats['rejected']==len(rows) and data==original_seed
    records=[json.loads(line) for line in log.read_text().splitlines()]
    assert {r['extraction_id'] for r in records if r['action']==decision}==set(decisions)
    assert all(r['reason'].startswith('model:') for r in records if r['action']==decision)
    assert conn.execute("SELECT workload FROM ai_request WHERE task='ai-review-2'").fetchone()[0]=='recheck'

    assert conn.execute('SELECT last_checked_at FROM restaurant_recheck WHERE restaurant_id=(SELECT restaurant_id FROM restaurant_recheck_attempt WHERE attempt_id=?)',(link['attempt_id'],)).fetchone()[0] is not None


def test_full_rules_inspection_without_changes_is_not_confirmed_open(world):
    budget,seeds=setup_due(world); recheck.admit(world,seeds,budget,now=NOW)
    link=world.execute('SELECT * FROM restaurant_recheck_job').fetchone()
    world.execute("UPDATE fetch_record SET job_id=? WHERE fetch_id='f'",(link['job_id'],));world.commit()
    process_doc(world,doc('달빛정원 공식 소개 페이지입니다.'),Gazetteer(world),tier='official')
    world.execute("UPDATE crawl_job SET state='done' WHERE job_id=?",(link['job_id'],))
    world.execute("UPDATE fetch_record SET extracted_at=? WHERE fetch_id='f'",(db.utcnow(),));world.commit()
    recheck.reconcile(world)
    r=world.execute('SELECT * FROM restaurant_recheck WHERE restaurant_id=(SELECT restaurant_id FROM restaurant_recheck_attempt WHERE attempt_id=?)',(link['attempt_id'],)).fetchone()
    assert r['last_checked_at'] and r['last_outcome']=='reviewed_or_no_change'
    assert world.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0]==0


def test_deferred_older_recheck_does_not_stop_fresh_extraction(world, tmp_path):
    budget,seeds=setup_due(world); recheck.admit(world,seeds,budget,now=NOW)
    link=world.execute('SELECT * FROM restaurant_recheck_job').fetchone()
    older='한도윤 셰프 달빛정원 운영 소식입니다. '+'소개 문장입니다. '*180
    fresh='한도윤 셰프 달빛정원 운영 소식입니다.'
    raw=RawStore(str(tmp_path/'raw'))
    old_ref=raw.put_text('official.test',older)
    new_ref=raw.put_text('official.test',fresh)
    world.execute("UPDATE fetch_record SET job_id=?,text_ref=?,fetched_at='2026-11-10T00:00:01Z' WHERE fetch_id='f'",(link['job_id'],old_ref))
    world.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_ref,fetched_at,fetcher_version,extractor_version)
                     VALUES ('fresh','https://official.test/fresh','https://official.test/fresh','official.test','html','ok',?,
                     '2026-11-10T00:00:02Z','fixture','rules-new')""",(new_ref,))
    world.commit()
    calls=[]
    def transport(payload,key):
        calls.append(json.loads(payload))
        assert not world.execute("SELECT 1 FROM ai_review_hold WHERE state='held'").fetchone()
        return response({'findings':[]})
    a=Adapters(Client(budget,key='dummy',transport=transport,balance_transport=healthy_balance))
    bot=cycle.Bot(world,data_dir=str(tmp_path/'data'),deepseek=True)
    bot.raw,bot.ai=raw,a
    # The real rules flag side-by-side chef/restaurant mentions as ambiguous.
    # Chronological extraction must continue after the older recheck defers.
    cycle.stage_extract(SimpleNamespace(env={'bot':bot},cost=budget.meter))
    assert len(calls)==1 and a.client.stopped is None
    assert 'fresh' in calls[0]['messages'][1]['content']
    assert world.execute("SELECT extracted_at FROM fetch_record WHERE fetch_id='f'").fetchone()[0] is None
    assert world.execute("SELECT extracted_at FROM fetch_record WHERE fetch_id='fresh'").fetchone()[0]
    assert not world.execute("SELECT 1 FROM ai_review_hold WHERE state='held'").fetchone()
    assert world.execute("SELECT COALESCE(SUM(units),0) FROM ai_request WHERE workload='recheck'").fetchone()[0]<=8
    assert world.execute('SELECT SUM(units) FROM ai_request').fetchone()[0]<=20


def test_trimmed_recheck_without_known_names_stays_uninspected(world, tmp_path):
    budget,seeds=setup_due(world); recheck.admit(world,seeds,budget,now=NOW)
    link=world.execute('SELECT * FROM restaurant_recheck_job').fetchone()
    # The real byte fitting keeps a prefix but removes the only known names.
    text='Introduction. '+('a '*500)+' 한도윤 셰프 달빛정원 운영 소식입니다.'
    raw=RawStore(str(tmp_path/'raw'))
    ref=raw.put_text('official.test',text)
    world.execute("UPDATE fetch_record SET job_id=?,text_ref=? WHERE fetch_id='f'",(link['job_id'],ref))
    world.execute("UPDATE crawl_job SET state='done' WHERE job_id=?",(link['job_id'],));world.commit()
    a=Adapters(Client(budget,key='dummy',transport=lambda *args:pytest.fail('trimmed unknown-only completion'),
                      balance_transport=healthy_balance))
    bot=cycle.Bot(world,data_dir=str(tmp_path/'data'),deepseek=True)
    bot.raw,bot.ai=raw,a
    cycle.stage_extract(SimpleNamespace(env={'bot':bot},cost=budget.meter))
    assert a.window_partial and a.stats['skipped_no_targets']==1
    assert a.defer_extract and a.client.stopped is None
    assert world.execute("SELECT extracted_at FROM fetch_record WHERE fetch_id='f'").fetchone()[0] is None
    assert not world.execute("SELECT 1 FROM restaurant_recheck_job WHERE fetch_id='f' AND extraction_id IS NULL").fetchone()
    assert not world.execute('SELECT 1 FROM ai_request').fetchone()
    assert not world.execute("SELECT 1 FROM ai_review_hold WHERE state='held'").fetchone()
    # Finishing any deterministic findings still cannot certify the incomplete scan.
    assert world.execute("SELECT 1 FROM review_queue WHERE state='open' AND reason='ai_inference'").fetchone()
    world.execute("UPDATE review_queue SET state='dismissed' WHERE reason='ai_inference'");world.commit()
    recheck.reconcile(world)
    row=world.execute("SELECT * FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()
    assert row['last_checked_at'] is None and row['active_attempt_id']==link['attempt_id']


@pytest.mark.parametrize('text,completed', [
    ('모래시계 공식 소개 페이지입니다.', False),
    ('서하람 셰프의 공식 소개 페이지입니다.', False),
    ('한도윤 셰프의 공식 소개 페이지입니다.', False),
    ('Dalbit Garden 공식 소개 페이지입니다.', True),
    ('달빛정원 공식 소개 페이지입니다.', True),
])
def test_completion_requires_due_restaurant_alias(world, text, completed):
    budget,seeds=setup_due(world); recheck.admit(world,seeds,budget,now=NOW)
    link=world.execute('SELECT * FROM restaurant_recheck_job').fetchone()
    world.execute("UPDATE fetch_record SET job_id=? WHERE fetch_id='f'",(link['job_id'],));world.commit()
    process_doc(world,doc(text),Gazetteer(world),tier='official')
    world.execute("UPDATE crawl_job SET state='done' WHERE job_id=?",(link['job_id'],))
    world.execute("UPDATE fetch_record SET extracted_at=? WHERE fetch_id='f'",(db.utcnow(),));world.commit()
    recheck.reconcile(world)
    row=world.execute("SELECT * FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()
    assert bool(row['last_checked_at'])==completed
    assert row['last_outcome']==('reviewed_or_no_change' if completed else 'partial_inspection')


@pytest.mark.parametrize('historical_extracted', [True,False])
def test_coalesced_job_ignores_historical_fetches(world, historical_extracted):
    budget,seeds=setup_due(world)
    tid=world.execute("SELECT target_id FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()[0]
    url='https://official.test/1'
    discover.enqueue(world,path='official_chain',pool='refresh',url=url,target_id=tid,caps=seeds['caps'])
    jid=discover.job_id('official_chain',url)
    world.execute("""INSERT INTO fetch_record(fetch_id,job_id,url,canonical_url,host,method,outcome,fetched_at,extracted_at,fetcher_version,extractor_version)
                     VALUES ('historical',?,?,?,'official.test','html','ok','2026-10-01T00:00:00Z',?,'fixture','rules-new')""",
                  (jid,url,url,'2026-10-01T00:00:01Z' if historical_extracted else None))
    world.commit()
    recheck.admit(world,seeds,budget,now=NOW)
    link=world.execute('SELECT * FROM restaurant_recheck_job').fetchone()
    assert link['job_id']==jid and link['dedicated_root']==0
    old=world.execute("SELECT * FROM fetch_record WHERE fetch_id='historical'").fetchone()
    assert recheck.active_for_fetch(world,old) is None
    world.execute("UPDATE fetch_record SET job_id=? WHERE fetch_id='f'",(jid,));world.commit()
    # A current pending fetch is still a retry, never a completed check.
    world.execute("UPDATE crawl_job SET state='done' WHERE job_id=?",(jid,));world.commit()
    recheck.reconcile(world)
    assert world.execute("SELECT active_attempt_id FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()[0]
    process_doc(world,doc('달빛정원 공식 소개 페이지입니다.'),Gazetteer(world),tier='official')
    world.execute("UPDATE fetch_record SET extracted_at=? WHERE fetch_id='f'",(db.utcnow(),));world.commit()
    recheck.reconcile(world)
    row=world.execute("SELECT * FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()
    assert row['last_checked_at'] and row['last_outcome']=='reviewed_or_no_change'
    assert not world.execute("SELECT 1 FROM restaurant_recheck_job WHERE fetch_id='historical'").fetchone()


@pytest.mark.parametrize('historical_state', ['done','failed','blocked'])
@pytest.mark.parametrize('current_state', ['queued','failed'])
def test_coalesced_job_links_current_children_without_finished_history(world, historical_state, current_state):
    budget,seeds=setup_due(world)
    tid=world.execute("SELECT target_id FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()[0]
    url='https://official.test/1'
    discover.enqueue(world,path='official_chain',pool='refresh',url=url,target_id=tid,now='2026-10-01T00:00:00Z')
    jid=discover.job_id('official_chain',url)
    discover.enqueue(world,path='official_chain',pool='refresh',url=url+'/historical',parent=jid,
                     target_id=tid,state=historical_state,now='2026-10-01T00:00:00Z')
    recheck.admit(world,seeds,budget,now=NOW)
    # An older queued child is still live work; a newly failed child is a real failure.
    discover.enqueue(world,path='official_chain',pool='refresh',url=url+'/current',parent=jid,
                     target_id=tid,state=current_state,
                     now='2026-10-01T00:00:00Z' if current_state=='queued' else recheck.iso(NOW+dt.timedelta(seconds=1)))
    recheck.link_children(world,jid)
    current=discover.job_id('official_chain',url+'/current')
    assert recheck.active_for_job(world,current)
    assert not recheck.active_for_job(world,discover.job_id('official_chain',url+'/historical'))
    world.execute("UPDATE fetch_record SET job_id=? WHERE fetch_id='f'",(jid,));world.commit()
    process_doc(world,doc('달빛정원 공식 소개 페이지입니다.'),Gazetteer(world),tier='official')
    world.execute("UPDATE fetch_record SET extracted_at=? WHERE fetch_id='f'",(db.utcnow(),))
    world.execute("UPDATE crawl_job SET state='done' WHERE job_id=?",(jid,));world.commit()
    recheck.reconcile(world)
    row=world.execute("SELECT * FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()
    assert row['last_checked_at'] is None
    if current_state=='failed':
        assert row['last_outcome']=='source_unavailable'
    else:
        assert row['active_attempt_id']
        world.execute("UPDATE crawl_job SET state='done' WHERE job_id=?",(current,));world.commit()
        recheck.reconcile(world)
        row=world.execute("SELECT * FROM restaurant_recheck WHERE restaurant_id='r_dal'").fetchone()
        assert row['last_checked_at'] and row['last_outcome']=='reviewed_or_no_change'


@pytest.mark.parametrize('existing_hold', [False,True])
@pytest.mark.parametrize('reservation_race', [False,True])
def test_unused_extraction_hold_is_released_or_restored(world, monkeypatch, existing_hold, reservation_race):
    budget,seeds=setup_due(world); recheck.admit(world,seeds,budget,now=NOW)
    aid=world.execute('SELECT attempt_id FROM restaurant_recheck_attempt').fetchone()[0]
    budget.workload='recheck'
    if existing_hold:
        assert recheck.hold_review(world,aid,budget,6000)
    calls=[]
    a=Adapters(Client(budget,key='dummy',transport=lambda *args: (calls.append(1) or response({'findings':[]})),
                      balance_transport=healthy_balance))
    if reservation_race:
        def raced(*args):
            raise CostCapExceeded('concurrent reservation consumed room')
        monkeypatch.setattr(budget,'reserve',raced)
    assert a.extract(doc('달빛정원 운영 소식입니다.'),[],Gazetteer(world),workload='recheck',attempt_id=aid)==[]
    assert len(calls)==(0 if reservation_race else 1)
    assert a.client.stopped is None and a.defer_extract==reservation_race
    hold=world.execute('SELECT * FROM ai_review_hold WHERE attempt_id=?',(aid,)).fetchone()
    if existing_hold:
        assert (hold['state'],hold['units'])==('held',3)
    else:
        assert hold['state']=='released'
    assert world.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0]==len(calls)


def test_date_migration_repairs_pending_rules_without_repairing_model_evidence(tmp_path):
    old_dir=tmp_path/'old_migrations';old_dir.mkdir()
    for path in Path(db.SCHEMA_DIR).glob('*.sql'):
        if path.name<'0009':
            shutil.copy(path,old_dir/path.name)
    conn=db.connect(str(tmp_path/'legacy.db'))
    db.migrate(conn,schema_dir=str(old_dir))
    load_seed_targets(conn,SEED)
    conn.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetched_at,fetcher_version,extractor_version)
                     VALUES ('f','https://press.test/a','https://press.test/a','press.test','html','ok',?,'fixture','rules-new')""",(db.utcnow(),))
    for xid in ('legacy_rule','legacy_model'):
        add(conn,xid,'closure','달빛정원은 2026년 10월 1일 문을 닫았다.',rest='달빛정원',date='2026-10-01')
        conn.execute("UPDATE extraction SET workload='recheck' WHERE extraction_id=?",(xid,))
        conn.execute("UPDATE review_queue SET payload_json=? WHERE subject_id=?",
                     (json.dumps({'finding':{'date_text':'2026년 10월 1일'}}),xid))
    conn.execute("UPDATE extraction SET extractor='rule:closure' WHERE extraction_id='legacy_rule'");conn.commit()
    assert db.migrate(conn)==['0009_extraction_verbatim_dates.sql']
    rule=conn.execute("SELECT * FROM extraction WHERE extraction_id='legacy_rule'").fetchone()
    assert (rule['date_text'],rule['date_iso'])==('2026년 10월 1일','2026-10-01')
    model=conn.execute("SELECT * FROM extraction WHERE extraction_id='legacy_model'").fetchone()
    assert (model['date_text'],model['date_iso'])==('2026-10-01',None)
    assert rv.precheck(dict(model),'달빛정원은 2026년 10월 1일 문을 닫았다.')[0].startswith('date_not_verbatim')
    assert db.migrate(conn)==[]
    conn.close()

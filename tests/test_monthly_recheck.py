"""Private monthly state, cost fairness, and publication through the common review path."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import pytest
from kg_ai.budget import Budget
from kg_collect import recheck, discover, scheduler, apply, gaps
from kg_collect.pipeline import process_doc
from kg_collect.targets import Gazetteer, load_seed_targets
from kg_daemon import db
from kg_daemon.runner import CostCapExceeded
from test_deepseek_client import meter, response
from test_deepseek_collect import world, doc, adapter
from test_ai_review import env, client_for, rv
from collect_world import SEED

NOW=dt.datetime(2026,11,10,tzinfo=dt.timezone.utc)


def setup_due(conn):
    recheck.ensure_schedule(conn,SEED,now=NOW)
    conn.execute("UPDATE restaurant_recheck SET next_due_at='2026-11-01T00:00:00Z'")
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


def test_rule_changes_are_isolated_until_common_review(env):
    conn,seed_path,log,raw=env
    budget,seeds=setup_due(conn); recheck.admit(conn,seeds,budget,now=NOW)
    link=conn.execute('SELECT * FROM restaurant_recheck_job').fetchone()
    conn.execute('UPDATE fetch_record SET job_id=? WHERE fetch_id=\'f\'',(link['job_id'],));conn.commit()
    text='달빛정원은 2026-10-01 문을 닫았다.'
    Path(raw,'h/t.txt').write_text(text)
    d=doc(text); d.url=d.canonical_url='https://press.test/a'
    # A deterministic rule may find the change but must still wait for the same review.
    process_doc(conn,d,Gazetteer(conn),tier='official')
    rows=conn.execute('SELECT * FROM extraction').fetchall()
    assert rows and all(r['workload']=='recheck' and r['pending_only']==1 and r['epistemic']=='inference' for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM target WHERE status='candidate'").fetchone()[0]==0
    before=conn.execute('SELECT COUNT(*) FROM restaurant_status_event').fetchone()[0]
    apply.apply_status(conn)
    assert conn.execute('SELECT COUNT(*) FROM restaurant_status_event').fetchone()[0]==before
    conn.execute("UPDATE crawl_job SET state='done' WHERE job_id=?",(link['job_id'],))
    conn.execute("UPDATE fetch_record SET extracted_at=? WHERE fetch_id='f'",(db.utcnow(),));conn.commit()
    recheck.reconcile(conn)
    assert conn.execute('SELECT last_checked_at FROM restaurant_recheck WHERE active_attempt_id=?',(link['attempt_id'],)).fetchone()[0] is None
    decisions={r['extraction_id']:'approve' for r in rows}
    stats=rv.review(conn,str(seed_path),str(log),raw,client=client_for(conn,decisions,[]))
    assert stats['published']>=1 and stats['remaining']==0
    data=json.loads(seed_path.read_text())
    entry=data['restaurants'][0]['ai_reviewed'][0]
    assert entry['badge']=='AI 검토' and entry['quote'] in text and 'closed_at' not in data['restaurants'][0]
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

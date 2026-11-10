import datetime as dt
import importlib.util
import json
from pathlib import Path
import pytest

SPEC=importlib.util.spec_from_file_location('digest',Path(__file__).resolve().parents[1]/'scripts/build_weekly_digest.py')
digest=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(digest)


def fixture(repo,at='2026-11-02T00:00:00Z',label='employment_claim'):
    (repo/'data').mkdir(exist_ok=True)
    row={'extraction_id':'x','review_id':'r','quote':'한도윤 셰프가 달빛정원으로 옮겼다.','chef':'한도윤','restaurant':'달빛정원',
         'src':'https://press.test/a','label':label,'badge':'AI 검토','provenance':'ai_reviewed','reviewed_at':at}
    (repo/'data/seed.json').write_text(json.dumps({'restaurants':[{'ai_reviewed':[row]}]}))
    event={'at':at,'action':'approve','published':True,**{k:row[k] for k in ('extraction_id','review_id','quote','src')}}
    (repo/'data/ai_review_log.jsonl').write_text(json.dumps(event)+'\n')
    return row,event


def test_week_and_year_boundary():
    assert digest.approval_date('2026-01-04T15:00:00Z')==dt.date(2026,1,5)
    assert digest.week_start('2025-12-29')==dt.date(2025,12,29)
    with pytest.raises(ValueError): digest.week_start('2026-11-03')


def test_offline_draft_idempotence_and_rollback(tmp_path,monkeypatch):
    import socket,subprocess
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('network'))
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('Argon/publish'))
    row,event=fixture(tmp_path)
    now=dt.datetime(2026,11,10,tzinfo=digest.KST)
    assert digest.build(tmp_path,now=now)==['chef-moves-2026-11-02.md']
    path=tmp_path/'marketing/drafts/chef-moves-2026-11-02.md'; text=path.read_text()
    assert digest.HEADER in text and row['quote'] in text and row['src'] in text
    stamp=path.stat().st_mtime_ns
    assert digest.build(tmp_path,now=now)==[] and path.stat().st_mtime_ns==stamp
    human=path.with_name(path.stem+'.argon.md'); human.write_text('human revision')
    with (tmp_path/'data/ai_review_log.jsonl').open('a') as f: f.write(json.dumps({**event,'action':'rollback'})+'\n')
    # Even older generated weeks are reconciled after a rollback.
    digest.build(tmp_path,now=dt.datetime(2026,12,1,tzinfo=digest.KST))
    assert 'WITHDRAWN' in path.read_text() and row['quote'] not in path.read_text()
    assert human.read_text()=='human revision'
    assert path.with_name(path.stem+'.review-required.txt').exists()


@pytest.mark.parametrize('label',['closure','relocation','menu','mentorship_claim'])
def test_only_career_and_employment(tmp_path,label):
    fixture(tmp_path,label=label)
    assert digest.build(tmp_path,week='2026-11-02')==[]
    assert not list((tmp_path/'marketing/drafts').glob('chef-moves*'))


def test_approval_week_published_only_and_dedup(tmp_path):
    row,event=fixture(tmp_path,at='2026-11-08T15:00:00Z') # Monday in Korea
    assert digest.build(tmp_path,week='2026-11-02')==[]
    assert digest.build(tmp_path,week='2026-11-09')==['chef-moves-2026-11-09.md']
    (tmp_path/'data/seed.json').write_text('{}')
    digest.build(tmp_path,week='2026-11-09')
    assert 'WITHDRAWN' in (tmp_path/'marketing/drafts/chef-moves-2026-11-09.md').read_text()


def test_missed_week_recovery(tmp_path):
    fixture(tmp_path,at='2025-12-30T00:00:00Z')
    assert digest.build(tmp_path,now=dt.datetime(2026,1,20,tzinfo=digest.KST))==['chef-moves-2025-12-29.md']


def test_paths_and_human_copy_are_protected(tmp_path):
    fixture(tmp_path)
    drafts=tmp_path/'marketing/drafts'; drafts.mkdir(parents=True)
    outside=tmp_path/'outside.md'; outside.write_text('untouched')
    path=drafts/'chef-moves-2026-11-02.md'; path.symlink_to(outside)
    with pytest.raises(ValueError): digest.build(tmp_path,week='2026-11-02')
    assert outside.read_text()=='untouched'
    path.unlink(); path.write_text('human copy')
    with pytest.raises(ValueError): digest.build(tmp_path,week='2026-11-02')
    assert path.read_text()=='human copy'
    path.unlink(); drafts.rmdir(); drafts.symlink_to(tmp_path/'data',target_is_directory=True)
    with pytest.raises(ValueError): digest.build(tmp_path,week='2026-11-02')
    for week in ('../2026-11-02','/2026-11-02','2026-11-03'):
        with pytest.raises(ValueError): digest.week_start(week)

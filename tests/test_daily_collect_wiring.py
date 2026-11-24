"""Execute pipeline branches using stubbed git, gh, timeout, collectors and builders."""
import os
from datetime import datetime
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import textwrap
import pytest
ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / 'scripts/daily_pipeline.sh'


def test_daily_pipeline_syntax_and_publish_contract():
    collect = ROOT / 'scripts/collect_daily.sh'
    subprocess.run(['bash','-n',str(SH)],check=True)
    subprocess.run(['bash','-n',str(collect)],check=True)
    daily, text = SH.read_text(), SH.read_text() + '\n' + collect.read_text()
    assert daily.index('collect_daily.sh') < daily.index('python3 crawler/apply.py') < daily.index('git add -A data crawler web scripts .gitignore')
    assert collect.read_text().index('-m kg_collect') < collect.read_text().index('crawler/instagram_kg.py')
    assert 's.get("failed",0)' in collect.read_text()
    assert 'daily-summary' in text and 'COLLECT_METRICS' in daily
    assert '--cost-cap' not in text and '--units' not in text
    assert 'Single-source alumni require bundle approval' not in daily
    assert 'instagram_kg.py' in text and 'instagram.py' not in text
    assert 'git checkout' not in collect.read_text() and 'git pull' not in collect.read_text()
    assert 'gh pr merge' not in collect.read_text() and './deploy.sh' not in collect.read_text()
    assert 'gh pr merge "$PR" --squash --delete-branch' in daily
    assert 'git push -q -u origin "$BR" --force' in daily and './deploy.sh main' in daily
    assert 'RESULT=NO_CHANGE' in daily and 'RESULT=MERGED_NO_DEPLOY pr=$PR' in daily
    assert 'RESULT=NEEDS_DEPLOY pr=$PR' in daily and 'RESULT=DEPLOYED pr=$PR' in daily
    assert 'RESULT=PR_CREATED' not in text


def test_daily_pipeline_falls_back_when_clone_path_missing(tmp_path):
    home=tmp_path/'home'; (home/'.git').mkdir(parents=True)
    env=dict(os.environ,KG_HOME=str(home),KG_RESOLVE_ONLY='1')
    out=subprocess.run(['bash',str(SH),str(tmp_path/'gone')],env=env,capture_output=True,text=True,check=True).stdout
    assert out.strip().splitlines()[-1]==f'REPO={home}'
    env['KG_HOME']=str(tmp_path/'nothing')
    p=subprocess.run(['bash',str(SH),str(tmp_path/'gone')],env=env,capture_output=True,text=True)
    assert p.returncode==2 and p.stdout.count('RESULT=')==1


@pytest.fixture
def pipeline(tmp_path):
    repo=tmp_path/'repo'; (repo/'.git').mkdir(parents=True); (repo/'data_store').mkdir()
    (repo/'data').mkdir()
    (repo/'data_store/state.db').touch(); (repo/'web').mkdir()
    (repo/'web/app.js').write_text('const SHOW_VERDICTS=false;\nconst SHOW_NEWSLETTER=false;\n')
    (repo/'crawler'/'out').mkdir(parents=True)
    (repo/'crawler'/'out'/'last_summary.json').write_text('{"applied":[],"pending_count":1}')
    bindir=tmp_path/'bin'; bindir.mkdir()
    log=tmp_path/'commands'
    common=textwrap.dedent(f'''\
        #!{sys.executable}
        import json,os,pathlib,subprocess,sys
        tool=pathlib.Path(sys.argv[0]).name; args=sys.argv[1:]
        with open(os.environ['TEST_LOG'],'a') as f: f.write(tool+' '+ ' '.join(args)+'\\n')
    ''')
    scripts={
      'git':'''if args[:2]==['rev-parse','--git-common-dir']: print('.git')
if args[:2]==['rev-parse','HEAD']: print('basesha')
if args[:1]==['symbolic-ref']:
    import datetime; print(os.environ.get('TEST_HEAD') or 'crawl/'+datetime.date.today().strftime('%Y%m%d'))
if args[:2]==['merge-base','--is-ancestor']: sys.exit(0)
if args[:2]==['rev-list','--count']: print(os.environ.get('TEST_COMMITS','1'))
if args[:2]==['diff','--quiet']:
    sys.exit(1 if os.environ.get('TEST_DIRTY')=='1' else 0)
if args[:3]==['diff','--cached','--quiet']:
    if 'web' in args:
        sys.exit(0 if os.environ.get('TEST_WEB')=='unchanged' or os.environ.get('TEST_CHANGE')!='1' else 1)
    p=pathlib.Path('.git/checks'); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1))
    if n==0: sys.exit(1 if os.environ.get('TEST_DIRTY')=='1' else 0)
    sys.exit(1 if os.environ.get('TEST_CHANGE')=='1' else 0)
''',
      'gh':"if os.environ.get('TEST_PR_FAIL') and args[:2]==['pr','merge']: sys.exit(5)\nprint('https://example.test/pr/1')\n",
      'timeout':'''while args and args[0].startswith('--'): args.pop(0)
args.pop(0)
if '-m' in args and 'kg_collect' in args and os.environ.get('TEST_COLLECT_RC'): sys.exit(int(os.environ['TEST_COLLECT_RC']))
if 'crawler/instagram_kg.py' in args and os.environ.get('TEST_IG_TIMEOUT'): sys.exit(124)
sys.exit(subprocess.call(args))
''',
      'python3':f'''if args and args[0]=='-c': sys.exit(subprocess.call([{sys.executable!r}]+args))
if 'kg_collect' in args:
    if os.environ.get('TEST_SIGNAL'):
        import signal
        pathlib.Path('sources/registry.discovered.yaml').write_text('changed\\n')
        os.kill(int(os.environ['TEST_PIPELINE_PID']), getattr(signal, 'SIG'+os.environ['TEST_SIGNAL']))
    print(json.dumps({{'status':'succeeded','run_id':'fixture','ai':'enabled','cost_used':0.1,'ai_reason':os.environ.get('TEST_COLLECT_REASON','none')}}))
elif 'kg_ai' in args: print('AI_REVIEW approved=0 rejected=0 published=0 reason='+os.environ.get('TEST_REVIEW_REASON','none')+' promotion_skips={{"no_chef":2,"temporary":1}}')
elif 'kg_daemon' in args: print(json.dumps({{'status':'succeeded','ai':'enabled','ai_reason':os.environ.get('TEST_DAEMON_REASON','none')}}))
elif 'crawler/instagram_kg.py' in args:
    if '--mark-timeout' in args: print('IG_FAILED account=kitchen.genome reason=timeout')
    else:
        state=os.environ.get('TEST_IG','OK'); print('IG_'+state+' account=kitchen.genome reason=fixture pending_only=1'); sys.exit(2 if state=='BLOCKED' else 0)
elif 'crawler/crawl.py' in args:
    print('crawler stdout'); print('crawler stderr', file=sys.stderr)
    if os.environ.get('TEST_SWITCH_BRANCH'): subprocess.run(['git','checkout','-qb','feat/other-work'],check=True)
    if os.environ.get('TEST_CHANGE'): pathlib.Path('data/change.json').write_text('{{}}\\n')
    if os.environ.get('TEST_CRAWL_FAIL'):
        pathlib.Path('sources/registry.discovered.yaml').write_text('changed\\n')
        sys.exit(7)
elif 'crawler/apply.py' in args and os.environ.get('TEST_PUBLICATION'):
    assert os.environ['KG_DAILY_LOCK_FD'] == '9'
    pathlib.Path('crawler/out/last_summary.json').write_text(json.dumps({{
        'applied':[{{'name':'Fixture','kind':'new_alumni'}}], 'pending_count':0,
        'publication':{{'new_alumni_verified':2,'new_alumni_checking':1}}
    }}))
elif 'scripts/build_deploy.py' in args:
    if os.environ.get('TEST_BUILD_FAIL'): sys.exit(3)
    if os.environ.get('TEST_POST_PR_FAIL') and pathlib.Path('.git/post-pr-build').exists(): sys.exit(6)
    pathlib.Path('.git/post-pr-build').touch()
'''
    }
    for tool,script in scripts.items():
        path=bindir/tool; path.write_text(common+script); path.chmod(0o755)
    env=dict(os.environ,PATH=str(bindir)+':'+os.environ['PATH'],TEST_LOG=str(log),KG_HOME=str(repo),KG_SERVICE_DB=str(repo/'data_store/state.db'),DEEPSEEK_API_KEY='synthetic-secret-never-output',KITCHENGENOME_IG_PASSWORD='synthetic-password-never-output',KG_CF_TOKEN_FILE=str(tmp_path/'missing-token'))
    def run(**opts):
        p=subprocess.run(['bash','-c','export TEST_PIPELINE_PID=$$; exec bash "$@"','--',str(SH),str(repo)],env={**env,**opts},capture_output=True,text=True,timeout=30)
        commands=log.read_text()
        assert 'synthetic-secret' not in p.stdout+p.stderr+commands
        assert 'synthetic-password' not in p.stdout+p.stderr+commands
        assert p.stdout.count('RESULT=')==1
        assert 'git push -q -u origin main' not in commands and 'instagram.py' not in commands
        if p.returncode==0: assert 'kg_collect' in commands
        return p,commands
    run.repo = repo
    run.bindir = bindir
    run.env = env
    return run


@pytest.mark.parametrize('options,expected', [
    ({}, 'IG_OK'), ({'TEST_IG': 'BLOCKED'}, 'IG_BLOCKED'), ({'TEST_IG': 'SKIPPED'}, 'IG_SKIPPED'),
    ({'TEST_IG_TIMEOUT': '1'}, 'IG_FAILED account=kitchen.genome reason=timeout'),
    ({'KG_IG': '0'}, 'IG_SKIPPED'), ({'TEST_COLLECT_RC': '124'}, 'COLLECT=failed'),
    ({'KG_DEEPSEEK': '0'}, 'RESULT=NO_CHANGE'),
])
def test_pipeline_nonfatal_steps_have_exact_result(pipeline,options,expected):
    p,commands=pipeline(**options)
    assert p.returncode==0 and p.stdout.strip().splitlines()[-1]=='RESULT=NO_CHANGE reason=none'
    assert expected in p.stdout and 'gh pr create' not in commands and 'gh pr merge' not in commands
    assert '--mark-timeout' not in commands
    assert not list((pipeline.repo / 'crawler/out').glob('ig*blocked.flag'))
    if options.get('KG_DEEPSEEK') == '0':
        assert '--deepseek' not in commands
    if options.get('KG_IG') != '0':
        ig_command = next(line for line in commands.splitlines() if 'crawler/instagram_kg.py' in line)
        assert '--cost-cap' not in ig_command


def test_pipeline_squash_merges_verified_crawl_without_deploy_when_web_is_unchanged(pipeline):
    p,commands=pipeline(TEST_CHANGE='1', TEST_WEB='unchanged')
    assert p.returncode==0
    assert p.stdout.strip().splitlines()[-1]=='RESULT=MERGED_NO_DEPLOY pr=https://example.test/pr/1 reason=none'
    assert 'gh pr create' in commands and 'gh pr merge' in commands and '--squash' in commands
    assert '--force' in commands and 'deploy.sh' not in commands


def test_pipeline_needs_deploy_when_web_changed_without_token(pipeline):
    p,commands=pipeline(TEST_CHANGE='1')
    assert p.returncode==0
    assert p.stdout.strip().splitlines()[-1]=='RESULT=NEEDS_DEPLOY pr=https://example.test/pr/1 reason=none'
    assert 'gh pr merge' in commands and 'deploy.sh' not in commands


def test_pipeline_reports_verified_and_checking_alumni_in_pr(pipeline):
    p, commands = pipeline(TEST_CHANGE='1', TEST_PUBLICATION='1')
    assert p.returncode == 0
    pr_command = next(line for line in commands.splitlines() if line.startswith('gh pr create'))
    assert 'new_alumni 2 verified, 1 checking' in pr_command
    assert 'site_publication_rollback.py' in pr_command
    assert 'pending queue only' not in pr_command


def test_pipeline_deploys_only_after_squash_merge_when_token_exists(pipeline, tmp_path):
    token = tmp_path/'cf-token'; token.write_text('fixture-token')
    script = pipeline.repo/'deploy.sh'
    script.write_text('#!/bin/sh\necho deploy >> "$TEST_LOG"\nexit 0\n')
    script.chmod(0o755)
    p,commands=pipeline(TEST_CHANGE='1', KG_CF_TOKEN_FILE=str(token))
    assert p.returncode==0, p.stderr
    assert p.stdout.strip().splitlines()[-1]=='RESULT=DEPLOYED pr=https://example.test/pr/1 reason=none'
    assert 'gh pr merge' in commands and 'deploy' in commands.splitlines()
    script.write_text('#!/bin/sh\necho deploy-fail >> "$TEST_LOG"\nexit 1\n')
    (pipeline.repo/'.git'/'checks').unlink()
    p,commands=pipeline(TEST_CHANGE='1', KG_CF_TOKEN_FILE=str(token))
    assert p.returncode==0, p.stderr
    assert p.stdout.strip().splitlines()[-1]=='RESULT=NEEDS_DEPLOY pr=https://example.test/pr/1 reason=none'
    assert 'deploy-fail' in commands.splitlines()


def test_collect_daily_runs_web_without_git_or_legacy_instagram(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'data_store').mkdir(parents=True)
    db = repo / 'data_store' / 'state.db'
    db.touch()
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    log = tmp_path / 'commands'
    common = textwrap.dedent(f'''\
        #!{sys.executable}
        import os, pathlib, sys
        tool=pathlib.Path(sys.argv[0]).name; args=sys.argv[1:]
        with open(os.environ['TEST_LOG'],'a') as f: f.write(tool+' '+' '.join(args)+'\\n')
    ''')
    (bindir / 'timeout').write_text(common + 'import subprocess,sys\nwhile args and args[0].startswith("--"): args.pop(0)\nargs.pop(0)\nsys.exit(subprocess.call(args))\n')
    (bindir / 'python3').write_text(common + 'import json,subprocess,sys\nif args and args[0]=="-c": raise SystemExit(subprocess.call([sys.executable]+args))\nif "kg_collect" in args:\n print(json.dumps({"status":"succeeded","run_id":"fixture","ai":"paused","ai_reason":"json_blank","cost_used":1,"egress":os.environ.get("TEST_EGRESS","zecbot"),"egress_reason":"fixture","ai_stats":{"considered":1,"skipped_filter":0,"called":1,"failed":1,"findings":0}}))\nelif "instagram_kg.py" in args: raise SystemExit("instagram was invoked")\n')
    (bindir / 'git').write_text(common + 'raise SystemExit("git invoked")\n')
    for path in bindir.iterdir():
        path.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir)+':'+os.environ['PATH'], TEST_LOG=str(log), KG_SERVICE_DB=str(db), KG_IG='0', KG_DEEPSEEK='1')
    script = ROOT / 'scripts/collect_daily.sh'
    p = subprocess.run(['bash', str(script), str(repo)], env=env, capture_output=True, text=True)
    commands = log.read_text()
    assert p.returncode == 0, p.stderr
    assert 'COLLECT=succeeded' in p.stdout and 'ai=paused' in p.stdout and 'reason=json_blank' in p.stdout
    assert 'ai_docs=1/0/1/1/0' in p.stdout and 'IG_SKIPPED' in p.stdout
    assert '--deepseek' in commands and '--no-browser' in commands and 'instagram' not in commands and 'git ' not in commands

    log.write_text('')
    p = subprocess.run(['bash', str(script), str(repo)], env=dict(env, TEST_EGRESS='box'), capture_output=True, text=True)
    assert p.returncode == 0 and 'COLLECT=succeeded' in p.stdout and 'egress=box' in p.stdout
    assert 'kg_collect --db' in log.read_text() and 'kg_collect.heavy' not in log.read_text()


def test_build_failure_emits_one_failure_result(pipeline):
    p,commands=pipeline(TEST_BUILD_FAIL='1')
    assert p.returncode==3 and 'RESULT=FAILED stage=build' in p.stdout and 'git push' not in commands


def test_pipeline_appends_stdout_and_stderr_to_daily_log(pipeline):
    p,_=pipeline()
    logs=list((pipeline.repo/'data_store/logs').glob('daily_pipeline_????????.log'))
    assert len(logs)==1 and logs[0].read_text()==p.stdout
    assert 'crawler stdout\n' in p.stdout and 'crawler stderr\n' in p.stdout
    assert p.stderr==''
    first=logs[0].read_text()
    p,_=pipeline(TEST_BUILD_FAIL='1')
    assert p.returncode==3 and logs[0].read_text()==first+p.stdout
    assert logs[0].read_text().splitlines()[-1].startswith('RESULT=FAILED stage=build')


def test_pipeline_keeps_logging_when_caller_closes_stdout(pipeline):
    with subprocess.Popen(['bash',str(SH),str(pipeline.repo)],env=pipeline.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as p:
        p.stdout.close()
        p.stdout=None
        _,stderr=p.communicate(timeout=30)
    assert p.returncode==0 and stderr==''
    log=next((pipeline.repo/'data_store/logs').glob('daily_pipeline_*.log')).read_text()
    assert 'crawler stdout\n' in log and 'crawler stderr\n' in log
    assert log.count('RESULT=')==1 and log.splitlines()[-1]=='RESULT=NO_CHANGE reason=none'


@pytest.fixture
def local_pipeline(pipeline):
    """Use real local git to verify cleanup; stub every network/build operation."""
    repo=pipeline.repo
    (repo/'.git').rmdir()
    real_git=shutil.which('git')
    def git(*args):
        return subprocess.run([real_git,*args],cwd=repo,capture_output=True,text=True,check=True).stdout.strip()
    git('init','-q','-b','main')
    git('config','user.name','Pipeline Test')
    git('config','user.email','pipeline@example.test')
    (repo/'sources').mkdir()
    (repo/'sources/registry.discovered.yaml').write_text('original\n')
    (repo/'scripts').mkdir()
    (repo/'scripts/fixture').touch()
    (repo/'.gitignore').write_text('data_store/\n')
    git('add','.')
    git('commit','-qm','base')
    base=git('rev-parse','HEAD')
    (repo/'sources/untracked.yaml').write_text('keep\n')
    wrapper=pipeline.bindir/'git'
    wrapper.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import os,pathlib,subprocess,sys
        args=sys.argv[1:]
        with open(os.environ['TEST_LOG'],'a') as f: f.write('git '+' '.join(args)+'\\n')
        if args[0] in ('pull','push'): sys.exit(0)
        if os.environ.get('TEST_CLEANUP_FAIL'):
            if args==['checkout','-q','--','sources']:
                pathlib.Path('.git/cleanup-failed').touch()
                sys.exit(9)
            if args==['checkout','-q','main'] and pathlib.Path('.git/cleanup-failed').exists(): sys.exit(9)
        sys.exit(subprocess.call([{real_git!r},*args]))
    '''))
    pipeline.git=git
    pipeline.base=base
    return pipeline


@pytest.mark.parametrize('name',['HUP','INT','TERM'])
def test_pipeline_signal_reports_cause_and_restores_tree(local_pipeline,name):
    p,commands=local_pipeline(TEST_SIGNAL=name,TEST_REVIEW_REASON='balance_low')
    assert p.returncode==128+getattr(signal,'SIG'+name)
    result=f'RESULT=FAILED stage=collect reason=signal_{name} ai_reason=balance_low'
    assert p.stdout.splitlines()[-1]==result
    assert 'reason=command_failed' not in p.stdout
    assert 'reason=balance_low promotion_skips={"no_chef":2,"temporary":1}' in p.stdout
    log=next((local_pipeline.repo/'data_store/logs').glob('daily_pipeline_*.log'))
    assert log.read_text()==p.stdout
    assert local_pipeline.git('branch','--show-current')=='main'
    assert local_pipeline.git('branch','--list','crawl/*')==''
    assert local_pipeline.git('rev-parse','main')==local_pipeline.base
    assert (local_pipeline.repo/'sources/registry.discovered.yaml').read_text()=='original\n'
    assert (local_pipeline.repo/'sources/untracked.yaml').read_text()=='keep\n'
    assert 'git push' not in commands and 'gh pr create' not in commands


def test_pipeline_failure_restores_sources_and_empty_crawl_branch(local_pipeline):
    p,_=local_pipeline(TEST_CRAWL_FAIL='1')
    assert p.returncode==7 and p.stdout.splitlines()[-1].startswith('RESULT=FAILED stage=crawl reason=command_failed exit=7')
    assert local_pipeline.git('branch','--show-current')=='main'
    assert local_pipeline.git('branch','--list','crawl/*')==''
    assert (local_pipeline.repo/'sources/registry.discovered.yaml').read_text()=='original\n'
    assert (local_pipeline.repo/'sources/untracked.yaml').read_text()=='keep\n'


def test_pipeline_cleanup_failure_preserves_exit_code(local_pipeline):
    p,_=local_pipeline(TEST_CRAWL_FAIL='1',TEST_CLEANUP_FAIL='1')
    assert p.returncode==7 and p.stdout.splitlines()[-1].startswith('RESULT=FAILED stage=crawl reason=command_failed exit=7')
    assert local_pipeline.git('branch','--show-current').startswith('crawl/')


def test_pipeline_cleanup_keeps_other_current_branch(local_pipeline):
    p,_=local_pipeline(TEST_CRAWL_FAIL='1',TEST_SWITCH_BRANCH='1')
    assert p.returncode==7
    assert local_pipeline.git('branch','--show-current')=='feat/other-work'
    assert local_pipeline.git('branch','--list','crawl/*')
    assert local_pipeline.git('rev-parse','main')==local_pipeline.base
    assert (local_pipeline.repo/'sources/registry.discovered.yaml').read_text()=='original\n'


def test_pipeline_preserves_existing_crawl_commits_on_startup(local_pipeline):
    branch='crawl/'+datetime.now().strftime('%Y%m%d')
    local_pipeline.git('checkout','-qb',branch)
    (local_pipeline.repo/'data/retained.json').write_text('{}\n')
    local_pipeline.git('add','data')
    local_pipeline.git('commit','-qm','retain this commit')
    retained=local_pipeline.git('rev-parse','HEAD')
    local_pipeline.git('checkout','-q','main')
    p,_=local_pipeline()
    assert p.returncode!=0
    assert local_pipeline.git('rev-parse',branch)==retained
    assert local_pipeline.git('rev-parse','main')==local_pipeline.base


def test_pipeline_failure_preserves_crawl_commits(local_pipeline):
    p,commands=local_pipeline(TEST_CHANGE='1',TEST_PR_FAIL='1')
    assert p.returncode==5 and p.stdout.splitlines()[-1].startswith('RESULT=FAILED stage=pr')
    assert local_pipeline.git('branch','--show-current').startswith('crawl/')
    assert local_pipeline.git('rev-list','--count',local_pipeline.base+'..HEAD')=='1'
    assert local_pipeline.git('rev-parse','main')==local_pipeline.base
    assert commands.count('git checkout -q -- sources\n')==2


def test_pipeline_does_not_cleanup_after_pr_succeeded(local_pipeline):
    p,commands=local_pipeline(TEST_CHANGE='1',TEST_POST_PR_FAIL='1')
    assert p.returncode==6 and p.stdout.splitlines()[-1].startswith('RESULT=FAILED stage=pr')
    assert commands.count('git checkout -q -- sources\n')==1
    assert local_pipeline.git('rev-list','--count',local_pipeline.base+'..crawl/'+datetime.now().strftime('%Y%m%d'))=='1'


def test_pipeline_does_not_cleanup_preexisting_dirty_tree(local_pipeline):
    source=local_pipeline.repo/'sources/registry.discovered.yaml'
    source.write_text('preexisting changes\n')
    p,commands=local_pipeline(TEST_DIRTY='1')
    assert p.returncode==2 and 'FATAL: tracked changes present' in p.stdout
    assert source.read_text()=='preexisting changes\n'
    assert 'git checkout' not in commands


@pytest.mark.parametrize('phase',['COLLECT','REVIEW','DAEMON'])
@pytest.mark.parametrize('changes',[{}, {'TEST_CHANGE':'1','TEST_WEB':'unchanged'}, {'TEST_CHANGE':'1'}])
def test_balance_low_survives_all_result_branches(pipeline,phase,changes):
    p,commands=pipeline(**{f'TEST_{phase}_REASON':'balance_low',**changes})
    assert p.returncode==0 and p.stdout.strip().splitlines()[-1].endswith('reason=balance_low')
    assert 'build_weekly_digest.py' in commands and 'argon_review.sh' not in commands
    assert 'instagram_kg.py' in commands


def test_real_failure_keeps_primary_reason_and_ai_pause(pipeline):
    p,commands=pipeline(TEST_BUILD_FAIL='1',TEST_COLLECT_REASON='balance_low')
    assert p.returncode==3 and p.stdout.strip().splitlines()[-1].endswith('reason=command_failed exit=3 ai_reason=balance_low')


def test_instagram_default_and_flag_files_are_untouched():
    collect=(ROOT/'scripts/collect_daily.sh').read_text()
    daily=SH.read_text()
    assert '${KG_IG:-0}' in collect and '${KG_IG:-1}' in daily
    for flag in ('ig_blocked.flag','ig_kg_blocked.flag'):
        assert flag not in collect+daily


def test_pipeline_refuses_to_commit_when_shared_tree_switched_branch(pipeline):
    # 2026-11-12: a dev session checked out a feature branch in the same tree mid-run;
    # the crawl commit landed there and gh failed with "No commits between main and crawl/...".
    p,commands=pipeline(TEST_CHANGE='1', TEST_HEAD='feat/ai-entertainment-prefilter')
    assert p.returncode==4
    assert 'RESULT=FAILED stage=pr reason=branch_switched head=feat/ai-entertainment-prefilter' in p.stdout
    assert 'git add' not in commands and 'git commit' not in commands and 'git push' not in commands
    assert 'gh pr create' not in commands


def test_pipeline_refuses_to_push_unexpected_commits(pipeline):
    p,commands=pipeline(TEST_CHANGE='1', TEST_COMMITS='2')
    assert p.returncode==4 and 'RESULT=FAILED stage=pr reason=unexpected_commits' in p.stdout
    assert 'git commit' in commands and 'git push' not in commands and 'gh pr create' not in commands

"""Execute pipeline branches using stubbed git, gh, timeout, collectors and builders."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import pytest
ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / 'scripts/daily_pipeline.sh'


def test_daily_pipeline_syntax_and_pr_only_boundary():
    collect = ROOT / 'scripts/collect_daily.sh'
    subprocess.run(['bash','-n',str(SH)],check=True)
    subprocess.run(['bash','-n',str(collect)],check=True)
    daily, text = SH.read_text(), SH.read_text() + '\n' + collect.read_text()
    assert daily.index('collect_daily.sh') < daily.index('python3 crawler/apply.py') < daily.index('git add -A --')
    assert collect.read_text().index('-m kg_collect') < collect.read_text().index('crawler/instagram_kg.py')
    assert 's.get("failed",0)' in collect.read_text()
    assert 'instagram_kg.py' in text and 'instagram.py' not in text
    assert 'git checkout' not in collect.read_text() and 'git pull' not in collect.read_text()
    assert 'gh pr merge' not in text and '--force' not in text and './deploy.sh' not in text
    assert 'git add -A -- data/seed.json crawler/pending.json crawler/applied.json web' in daily
    assert 'git branch -D' not in text


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
    (repo/'data_store/state.db').touch(); (repo/'web').mkdir()
    (repo/'web/app.js').write_text('const SHOW_VERDICTS=false;\nconst SHOW_NEWSLETTER=false;\n')
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
if args[:3]==['diff','--cached','--quiet']:
    p=pathlib.Path('.git/checks'); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1))
    sys.exit(1 if n>0 and os.environ.get('TEST_CHANGE')=='1' else 0)
''',
      'gh':"print('https://example.test/pr/1')\n",
      'timeout':'''while args and args[0].startswith('--'): args.pop(0)
args.pop(0)
if '-m' in args and 'kg_collect' in args and os.environ.get('TEST_COLLECT_RC'): sys.exit(int(os.environ['TEST_COLLECT_RC']))
if 'crawler/instagram_kg.py' in args and os.environ.get('TEST_IG_TIMEOUT'): sys.exit(124)
sys.exit(subprocess.call(args))
''',
      'python3':f'''if args and args[0]=='-c': sys.exit(subprocess.call([{sys.executable!r}]+args))
if 'kg_collect' in args: print(json.dumps({{'status':'succeeded','run_id':'fixture','ai':'enabled','cost_used':0.1}}))
elif 'crawler/instagram_kg.py' in args:
    if '--mark-timeout' in args: print('IG_FAILED account=kitchen.genome reason=timeout')
    else:
        state=os.environ.get('TEST_IG','OK'); print('IG_'+state+' account=kitchen.genome reason=fixture pending_only=1'); sys.exit(2 if state=='BLOCKED' else 0)
elif 'scripts/build_deploy.py' in args and os.environ.get('TEST_BUILD_FAIL'): sys.exit(3)
'''
    }
    for tool,script in scripts.items():
        path=bindir/tool; path.write_text(common+script); path.chmod(0o755)
    env=dict(os.environ,PATH=str(bindir)+':'+os.environ['PATH'],TEST_LOG=str(log),KG_SERVICE_DB=str(repo/'data_store/state.db'),DEEPSEEK_API_KEY='synthetic-secret-never-output',KITCHENGENOME_IG_PASSWORD='synthetic-password-never-output')
    def run(**opts):
        p=subprocess.run(['bash',str(SH),str(repo)],env={**env,**opts},capture_output=True,text=True)
        commands=log.read_text()
        assert 'synthetic-secret' not in p.stdout+p.stderr+commands
        assert 'synthetic-password' not in p.stdout+p.stderr+commands
        assert p.stdout.count('RESULT=')==1
        assert 'gh pr merge' not in commands and 'git push -q -u origin main' not in commands
        assert './deploy.sh' not in commands and 'instagram.py' not in commands
        assert 'kg_collect' in commands
        return p,commands
    return run


@pytest.mark.parametrize('options,expected', [({},'ig=OK'),({'TEST_IG':'BLOCKED'},'ig=BLOCKED'),({'TEST_IG':'SKIPPED'},'ig=SKIPPED'),({'TEST_IG_TIMEOUT':'1'},'ig=FAILED'),({'TEST_COLLECT_RC':'124'},'collect=failed'),({'KG_DEEPSEEK':'0'},'RESULT=NO_CHANGE')])
def test_pipeline_nonfatal_steps_have_exact_result(pipeline,options,expected):
    p,commands=pipeline(**options)
    assert p.returncode==0 and 'RESULT=NO_CHANGE' in p.stdout and expected in p.stdout
    assert 'gh pr create' not in commands
    if options.get('KG_DEEPSEEK')=='0': assert '--deepseek' not in commands


def test_pipeline_pr_stops_before_merge_or_deploy(pipeline):
    p,commands=pipeline(TEST_CHANGE='1')
    assert p.returncode==0 and 'RESULT=PR_CREATED pr=https://example.test/pr/1' in p.stdout
    assert 'gh pr create' in commands and 'git push -q -u origin crawl/' in commands


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
    (bindir / 'python3').write_text(common + 'import json,subprocess,sys\nif args and args[0]=="-c": raise SystemExit(subprocess.call([sys.executable]+args))\nif "kg_collect" in args:\n print(json.dumps({"status":"succeeded","run_id":"fixture","ai":"paused","ai_reason":"json_blank","cost_used":1,"ai_stats":{"considered":1,"skipped_filter":0,"called":1,"failed":1,"findings":0}}))\nelif "instagram_kg.py" in args: raise SystemExit("instagram was invoked")\n')
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


def test_build_failure_emits_one_failure_result(pipeline):
    p,commands=pipeline(TEST_BUILD_FAIL='1')
    assert p.returncode==3 and 'RESULT=FAILED stage=build' in p.stdout and 'git push' not in commands

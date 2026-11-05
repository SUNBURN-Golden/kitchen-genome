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
    subprocess.run(['bash','-n',str(SH)],check=True)
    text=SH.read_text()
    assert text.index('-m kg_collect') < text.index('python3 crawler/apply.py') < text.index('git add -u --')
    assert 'crawler/instagram_kg.py' in text and 'crawler/instagram.py' not in text
    assert 'gh pr merge' not in text and '--force' not in text and './deploy.sh' not in text
    assert 'git add -A' not in text and 'git branch -D' not in text


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
        assert './deploy.sh' not in commands
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


def test_build_failure_emits_one_failure_result(pipeline):
    p,commands=pipeline(TEST_BUILD_FAIL='1')
    assert p.returncode==3 and 'RESULT=FAILED stage=build' in p.stdout and 'git push' not in commands

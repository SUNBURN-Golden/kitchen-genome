"""The daily pipeline runs one collection cycle, but can never break the existing flow."""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, 'scripts', 'daily_pipeline.sh')


def test_daily_pipeline_syntax_and_non_fatal_collect_step():
    subprocess.run(['bash', '-n', SH], check=True)
    s = open(SH, encoding='utf-8').read()
    i_collect, i_apply, i_add = s.index('-m kg_collect'), s.index('python3 crawler/apply.py'), s.index('git add -A data crawler web scripts')
    assert i_collect < i_apply < i_add
    block = s[s.index('KG_DB='): s.index('python3 crawler/apply.py')]
    assert 'timeout' in block and '|| true' in block and 'KG_COLLECT:-1' in block
    assert 'git checkout -q -- sources' in block            # machine-written registry never dirties the clone
    assert 'build_data.py' not in block and 'deploy' not in block
    assert 'kg_collect' not in s[i_add:]                     # nothing after staging depends on the bot

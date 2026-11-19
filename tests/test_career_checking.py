"""Unverified career claims flagged in seed (`career_checking`) render a '확인 중' label on the static pages."""
import json, os, subprocess, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


def test_career_checking_label_rendered():
    subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'build_pages.py')], check=True, capture_output=True)
    seed = json.load(open(os.path.join(ROOT, 'data', 'seed.json'), encoding='utf-8'))
    flagged = [r for r in seed['restaurants'] if r.get('career_checking')]
    assert {r['id'] for r in flagged} >= {'finz', 'oct19', 'oddhouse', 'goum', 'onjium_r'}
    for r in flagged:
        html = open(os.path.join(ROOT, 'web', 'r', r['id'], 'index.html'), encoding='utf-8').read()
        assert html.count('<span class="tag">확인 중</span>') == len(r['career_checking']), r['id']
        assert 'noindex' not in html


def test_spa_badge_prefers_checking():
    js = open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()
    assert "const badge=r=>(r.career_checking||[]).length?'<span class=\"tag\">확인 중</span>'" in js
    assert 'const SHOW_VERDICTS=false;' in js and 'const SHOW_NEWSLETTER=false;' in js

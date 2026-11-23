"""Unverified career claims flagged in seed (`career_checking`) render a '확인 중' label on the static pages."""
import json, os, shutil, subprocess, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


def test_career_checking_label_rendered(tmp_path):
    for folder in ('data', 'scripts', 'web'):
        (tmp_path / folder).mkdir()
    for name in ('data/seed.json', 'scripts/build_pages.py', 'scripts/lineage_evidence.py'):
        shutil.copyfile(os.path.join(ROOT, name), tmp_path / name)
    subprocess.run([sys.executable, tmp_path / 'scripts/build_pages.py'], check=True, capture_output=True)
    seed = json.load(open(os.path.join(ROOT, 'data', 'seed.json'), encoding='utf-8'))
    flagged = [r for r in seed['restaurants'] if r.get('career_checking')]
    assert {r['id'] for r in flagged} >= {'finz', 'oct19', 'oddhouse', 'goum', 'onjium_r'}
    for r in flagged:
        html = open(tmp_path / 'web' / 'r' / r['id'] / 'index.html', encoding='utf-8').read()
        # The title also carries one 확인 중 tag when the restaurant's lineage evidence is unverified.
        title = 1 if r.get('lineage_evidence') and r.get('lineage_status') != 'verified' else 0
        # Promoted career links render their own 확인 중 tags under 셰프 근무 이력; count outside it.
        head, sep, rest = html.partition('<h2>셰프 근무 이력</h2>')
        outside = head + (rest[rest.find('<h2>'):] if sep and '<h2>' in rest else '')
        assert outside.count('<span class="tag">확인 중</span>') == len(r['career_checking']) + title, r['id']
        assert 'noindex' not in html


def test_spa_badge_prefers_checking():
    js = open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()
    assert "const badge=r=>(r.career_checking||[]).length?'<span class=\"tag\">확인 중</span>'" in js
    assert 'const SHOW_VERDICTS=false;' in js and 'const SHOW_NEWSLETTER=false;' in js

"""rules/1.5 generalisation: headline punctuation after '오픈', quoted venue right before the opening verb, collab veto scoped to its headline
clause, unregistered quoted partner venue on collab records. Sentences here are synthetic (NOT the held-out sets)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'daemon'))
from kg_collect.extract import extract_text, same_clause  # noqa: E402
from kg_collect.gold import build_gold_db  # noqa: E402
from kg_collect.targets import Gazetteer  # noqa: E402

GOLD = {'targets': [{'kind': 'restaurant', 'name_ko': '노을'}, {'kind': 'chef', 'name_ko': '김하늘', 'restaurants': []}]}


def run(text):
    gaz = Gazetteer(build_gold_db(GOLD))
    return {(r['label'], r['chef_text'], r['restaurant_text'] or r['org_text']) for r in extract_text(text, gaz=gaz, host='news.example')}


def test_opening_followed_by_ellipsis():
    assert ('opening', None, '노을') in run('한남동 ‘노을’ 오픈…제철 코스 선보인다')


def test_opening_followed_by_closing_quote_or_bang():
    assert ('opening', None, '노을') in run('드디어 노을 오픈!')


def test_quoted_new_venue_before_opening_verb_without_venue_noun():
    got = run('김하늘 셰프 ‘달무리’ 오픈 — 제철 재료로 만든 8코스')
    assert ('opening', '김하늘', '달무리') in got


def test_quoted_new_venue_with_date_before_opening():
    assert any(g[0] == 'opening' and g[2] == '달무리' for g in run('식품사 ‘달무리’ 5일 오픈 — 강남에 첫 매장'))


def test_collab_in_other_clause_does_not_veto_opening():
    got = run('김하늘 셰프 ‘노을’ 오픈 — 제철 코스…도자기 공방과 협업한 식기')
    assert ('opening', '김하늘', '노을') in got


def test_collab_in_same_clause_still_vetoes_opening():
    got = run('김하늘 셰프와 협업해 노을에서 스페셜 메뉴 오픈')
    assert not any(g[0] == 'opening' for g in got)


def test_collab_takes_unregistered_quoted_venue():
    got = run('편의점이 미쉐린 1스타 레스토랑 ‘별빛’의 김하늘 셰프와 협업한 도시락을 출시했다.')
    assert ('collab', '김하늘', '별빛') in got


def test_same_clause_helper():
    s = 'A 오픈 — B 협업'
    assert not same_clause(s, s.index('오픈'), s.index('협업'))
    assert same_clause('A와 협업해 오픈', 2, 7)

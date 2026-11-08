"""rules/1.8: a collaboration names the chef and venue next to the cue.

Sentences here are synthetic (NOT the held-out sets). An earlier bystander must not
replace the person or restaurant beside the verb, and a short venue stays a venue
when the next words are an event ('의 갈라 디너'), not an ordinary noun ('의 맛').
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'daemon'))
from kg_collect.extract import extract_text  # noqa: E402
from kg_collect.gold import build_gold_db  # noqa: E402
from kg_collect.targets import Gazetteer  # noqa: E402

GOLD = {'targets': [
    {'kind': 'restaurant', 'name_ko': '노을'},
    {'kind': 'restaurant', 'name_ko': '한빛'},
    {'kind': 'restaurant', 'name_ko': '온기'},
    {'kind': 'chef', 'name_ko': '김하늘', 'restaurants': []},
    {'kind': 'chef', 'name_ko': '박서준', 'restaurants': []},
]}


def records(text):
    return extract_text(text, gaz=Gazetteer(build_gold_db(GOLD)), host='news.example')


def run(text):
    return {(r['label'], r['chef_text'], r['restaurant_text'] or r['org_text']) for r in records(text)}


def test_collab_uses_the_chef_beside_the_verb_not_an_earlier_bystander():
    got = run('김하늘 셰프가 사회를 보고 임지훈 셰프가 노을과 협업한다.')
    assert ('collab', '임지훈', '노을') in got
    assert not any(g[0] == 'collab' and g[1] == '김하늘' for g in got)


def test_collab_uses_the_venue_beside_the_verb():
    got = run('김하늘 셰프는 한빛 소속이고 노을과 갈라 디너를 진행한다.')
    assert ('collab', '김하늘', '노을') in got
    assert not any(g[0] == 'collab' and g[2] == '한빛' for g in got)


def test_coordinated_pair_keeps_its_first_participant():
    chefs = run('김하늘 셰프와 박서준 셰프가 함께 일했다.')
    assert ('collab', '김하늘', None) in chefs
    assert not any(g[0] == 'collab' and g[1] == '박서준' for g in chefs)
    venues = run('한빛과 노을이 협업 디너를 열었다.')
    assert ('collab', None, '한빛') in venues
    assert not any(g[0] == 'collab' and g[2] == '노을' for g in venues)


def test_single_chef_collab_is_unchanged():
    assert ('collab', '김하늘', '노을') in run('‘노을’ 김하늘 셰프가 게스트 셰프로 협업한다.')


def test_short_venue_before_an_event_noun_is_kept():
    assert ('collab', '김하늘', '노을') in run('노을의 갈라 디너에 김하늘 셰프가 게스트로 선다.')


def test_short_venue_before_an_ordinary_noun_stays_dropped():
    got = run('김하늘 셰프는 재료 온기의 맛을 살린 요리를 선보이며 협업했다.')
    assert not any(g[2] == '온기' for g in got)


def test_popup_career_uses_the_chef_beside_the_popup():
    got = run('김하늘 셰프가 사회를 보고 임지훈 셰프가 팝업 ‘달무리’를 운영했다.')
    assert ('career', '임지훈', '달무리') in got
    assert not any(g[0] == 'career' and g[1] == '김하늘' for g in got)

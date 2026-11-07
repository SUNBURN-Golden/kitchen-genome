"""rules/1.7: reject type/event nouns as venue names (synthetic only; not held-out)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'daemon'))
from kg_collect.extract import extract_text  # noqa: E402
from kg_collect.gold import build_gold_db  # noqa: E402
from kg_collect.targets import Gazetteer  # noqa: E402

GOLD = {'targets': [
    {'kind': 'restaurant', 'name_ko': '노을'},
    {'kind': 'restaurant', 'name_ko': '한빛'},
    {'kind': 'chef', 'name_ko': '김하늘', 'restaurants': []},
]}


def records(text):
    gaz = Gazetteer(build_gold_db(GOLD))
    return extract_text(text, gaz=gaz, host='news.example')


def run(text):
    return {(r['label'], r['chef_text'], r['restaurant_text'] or r['org_text']) for r in records(text)}


@pytest.mark.parametrize('text', [
    '외식연구회가 열두 번째 팝업 레스토랑을 연다.',
    '협회가 새 식당을 열고 손님을 맞는다.',
    '재단이 다이닝을 연다.',
])
def test_type_noun_object_opening_is_ignored(text):
    assert not any(g[0] == 'opening' for g in run(text))


def test_named_popup_object_opening_still_works():
    assert run("외식연구회가 팝업 레스토랑 ‘달무리’를 연다.") == {('opening', None, '달무리')}


def test_named_bare_venue_object_opening_still_works():
    assert run('외식연구회가 팝업 식당 노을을 열고 손님을 맞는다.') == {('opening', None, '노을')}


@pytest.mark.parametrize('text', [
    '업계에서는 호텔 레스토랑의 셰프 협업과 후속 레스토랑 출범이 주목된다.',
    '호텔 다이닝의 셰프 협업이 늘어나는 가운데 신규 식당 출범이 이어진다.',
    '셰프 협업 전략과 레스토랑 론칭이 화제다.',
])
def test_event_noun_is_not_collab_partner(text):
    got = run(text)
    assert not any(g[0] == 'collab' for g in got)


def test_real_collab_with_chef_and_venue_still_works():
    assert ('collab', '김하늘', '노을') in run('‘노을’ 김하늘 셰프가 게스트 셰프로 협업한다.')


def test_bare_subject_venue_collab_still_works():
    assert ('collab', '김하늘', '한빛') in run('김하늘 셰프와 식당 한빛이 협업한다.')


def test_quoted_opening_with_chef_unaffected():
    assert ('opening', '김하늘', '노을') in run('김하늘 셰프 ‘노을’ 오픈')

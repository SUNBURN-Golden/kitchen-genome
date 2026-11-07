"""rules/1.6: chef product launches and venue-object openings.
Sentences here are synthetic (NOT the held-out sets).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'daemon'))
from kg_collect.extract import extract_text  # noqa: E402
from kg_collect.gold import build_gold_db  # noqa: E402
from kg_collect.targets import Gazetteer  # noqa: E402

GOLD = {'targets': [{'kind': 'restaurant', 'name_ko': '노을'}, {'kind': 'restaurant', 'name_ko': '한빛'},
                    {'kind': 'chef', 'name_ko': '김하늘', 'restaurants': []}]}


def records(text):
    gaz = Gazetteer(build_gold_db(GOLD))
    return extract_text(text, gaz=gaz, host='news.example')


def run(text):
    return {(r['label'], r['chef_text'], r['restaurant_text'] or r['org_text']) for r in records(text)}


@pytest.mark.parametrize('text', [
    '‘노을’ 김하늘 셰프와 ‘푸른밥상 셰프컬렉션’ 출시.',
    '‘노을’ 김하늘 쉐프와 식품회사가 새 도시락을 출시했다.',
    '‘노을’ 김하늘 셰프가 제철 재료로 만든 소스를 출시한다.',
    '새 제품을 출시한 식품회사는 ‘노을’ 김하늘 셰프의 조리법을 담았다.',
    '식품회사는 셰프 컬렉션을 출시했다며 ‘노을’ 김하늘 셰프를 소개했다.',
])
def test_product_launch_is_collab_and_vetoes_employment(text):
    got = run(text)
    assert ('collab', '김하늘', '노을') in got
    assert not any(g[0] == 'employment_claim' for g in got)


def test_product_launch_with_unregistered_quoted_chef_venue():
    assert run('‘달무리’ 임지훈 셰프와 ‘푸른밥상 셰프컬렉션’ 출시.') == {('collab', '임지훈', '달무리')}


def test_product_launch_does_not_use_product_as_chef_venue():
    assert run('김하늘 셰프와 ‘푸른밥상 셰프컬렉션’을 출시했다.') == {('collab', '김하늘', None)}


@pytest.mark.parametrize('text', [
    '노을 호텔은 봄맞이 숙박 패키지를 출시했다.',
    '노을 호텔은 와인 페어링 패키지를 출시했다.',
    '노을 호텔은 새로운 제품을 출시했다.',
    '노을 호텔은 ‘푸른밥상 셰프컬렉션’을 출시했다.',
])
def test_launch_without_chef_is_not_collab(text):
    assert not any(g[0] == 'collab' for g in run(text))


def test_side_by_side_without_launch_remains_employment():
    assert ('employment_claim', '김하늘', '노을') in run('‘노을’ 김하늘 셰프는 재료 이야기를 전했다.')


@pytest.mark.parametrize('frame', ['팝업 레스토랑', '팝업', '레스토랑', '식당', '다이닝'])
@pytest.mark.parametrize('ending', ['을 연다.', '을 열고 손님을 맞는다.'])
def test_quoted_venue_object_opening(frame, ending):
    rs = records(f'외식연구회가 열두 번째 {frame} ‘달무리(Moonbow)’{ending}')
    assert {(r['label'], r['restaurant_text']) for r in rs} == {('opening', '달무리')}
    if '팝업' in frame:
        assert rs[0]['venue_format'] == 'popup'


def test_venue_object_reul_and_chef():
    assert run('김하늘 셰프가 레스토랑 ‘하루’를 연다.') == {('opening', '김하늘', '하루')}


def test_known_bare_venue_object_opening():
    assert run('외식연구회가 팝업 식당 노을을 열고 손님을 맞는다.') == {('opening', None, '노을')}


def test_opening_attaches_object_instead_of_organizer_venue():
    assert run('노을이 팝업 레스토랑 ‘한빛’을 연다.') == {('opening', None, '한빛')}


@pytest.mark.parametrize('event', ['갈라 디너', '만찬', '행사', '이벤트', '갈라'])
@pytest.mark.parametrize('verb', ['연다', '열고 손님을 맞는다'])
def test_framed_event_object_is_not_opening(event, verb):
    got = run(f'레스토랑 ‘노을’ 김하늘 셰프가 ‘{event}’를 {verb}.')
    assert not any(g[0] == 'opening' for g in got)


def test_event_yeonda_remains_collab():
    assert run('식당 ‘노을’ 김하늘 셰프가 갈라 디너를 연다.') == {('collab', '김하늘', '노을')}


def test_unregistered_bare_subject_venue_on_event_collab():
    assert run('식당 달무리가 초여름 갈라 디너를 연다.') == {('collab', None, '달무리')}


def test_yeonda_without_venue_frame_is_not_opening():
    assert not any(g[0] == 'opening' for g in run('외식연구회가 ‘달무리’를 연다.'))

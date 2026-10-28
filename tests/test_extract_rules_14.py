"""rules/1.4 generalisation: short-form coreference, coordinated restaurant lists, bracketed bilingual names, common-word venue names.
Sentences here are synthetic (NOT the held-out sets)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'daemon'))
from kg_collect.extract import extract_text  # noqa: E402
from kg_collect.gold import build_gold_db  # noqa: E402
from kg_collect.targets import Gazetteer  # noqa: E402

GOLD = {'targets': [{'kind': 'restaurant', 'name_ko': '한빛'}, {'kind': 'restaurant', 'name_ko': '파랑식당'}, {'kind': 'restaurant', 'name_ko': '노을'},
                    {'kind': 'restaurant', 'name_ko': '온기'}, {'kind': 'restaurant', 'name_ko': '마루', 'name_en': 'Maru'},
                    {'kind': 'chef', 'name_ko': '김하늘', 'restaurants': []}, {'kind': 'chef', 'name_ko': '박서준', 'restaurants': []}]}


def run(text):
    gaz = Gazetteer(build_gold_db(GOLD))
    return {(r['label'], r['chef_text'], (r['org_text'] or r['restaurant_text']) if r['label'] == 'career' else (r['restaurant_text'] or r['org_text'])) for r in extract_text(text, gaz=gaz, host='news.example')}


def test_short_form_coref_resolves_to_earlier_full_name():
    got = run('김하늘 셰프는 요리를 시작했다. 이후 김 셰프는 노을의 총괄 셰프로 합류했다.')
    assert ('employment_claim', '김하늘', '노을') in got


def test_short_form_coref_ambiguous_is_dropped():
    got = run('김하늘 셰프와 김민수 셰프가 만났다. 김 셰프는 노을의 총괄 셰프로 합류했다.')
    assert not any(g[1] == '김하늘' and g[2] == '노을' for g in got)


def test_given_name_short_form():
    got = run('박서준 셰프가 왔다. 서준 셰프는 한빛을 이끌고 있다.')
    assert ('employment_claim', '박서준', '한빛') in got


def test_coordinated_list_of_restaurants():
    got = run('박서준 셰프는 한빛, 파랑식당과 노을을 이끌고 있다.')
    assert {('employment_claim', '박서준', r) for r in ('한빛', '파랑식당', '노을')} <= got


def test_career_list():
    got = run('김하늘 셰프는 한빛과 파랑식당을 거쳐 독립했다.')
    assert {('career', '김하늘', '한빛'), ('career', '김하늘', '파랑식당')} <= got


def test_bracketed_bilingual_names():
    got = run('Maru(마루)의 정다은(Daeun Jung) 셰프가 새 코스를 공개했다.')
    assert ('employment_claim', '정다은', 'Maru') in got


def test_common_word_restaurant_alias_is_not_a_venue():
    got = run('김하늘 셰프는 재료 온기의 맛을 살린 요리를 선보이며 협업했다.')
    assert not any(g[2] == '온기' for g in got)
    got2 = run("김하늘 셰프는 '온기'에서 근무했다.")
    assert ('career', '김하늘', '온기') in got2

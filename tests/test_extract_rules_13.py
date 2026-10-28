"""rules/1.3 general rules, on invented sentences (not from any gold set)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'daemon'))
from kg_collect.gold import evaluate_gold  # noqa: E402

T = [{'kind': 'restaurant', 'name_ko': '가나식당', 'name_en': 'Gana'}, {'kind': 'restaurant', 'name_ko': '다라', 'name_en': 'Dara'},
     {'kind': 'chef', 'name_ko': '홍길동', 'name_en': 'Gildong Hong'}]


def _run(text, expect, tier='press'):
    r = evaluate_gold({'targets': T, 'sentences': [{'text': text, 'host': 'x.test', 'tier': tier, 'published_at': '2026-10-01', 'expect': expect}]})
    return r


def test_popup_opening_is_not_collab():
    r = _run("○○그룹, 다섯 번째 팝업 '가나식당' 오픈한다고 밝혔다.", [{'label': 'opening', 'restaurant': '가나식당', 'epistemic': 'claim'}])
    assert r['precision'] == 1.0 and r['recall'] == 1.0


def test_side_by_side_mention_is_weak_employment_claim():
    r = _run("홍길동 셰프 '가나식당' 미쉐린 2스타 유지", [{'label': 'employment_claim', 'chef': '홍길동', 'restaurant': '가나식당', 'epistemic': 'claim'}])
    assert r['recall'] == 1.0


def test_event_dinner_is_collab():
    r = _run("호텔, '가나식당' 홍길동 셰프 와인 디너 개최", [{'label': 'collab', 'chef': '홍길동', 'restaurant': '가나식당', 'epistemic': 'claim'}])
    assert r['recall'] == 1.0 and r['precision'] == 1.0


def test_english_former_and_opens():
    r = _run('Former Gana Star Gildong Hong Opens Dara in Seoul',
             [{'label': 'career', 'chef': 'Gildong Hong', 'org': 'Gana', 'epistemic': 'claim'}, {'label': 'opening', 'chef': 'Gildong Hong', 'restaurant': 'Dara', 'epistemic': 'claim'}])
    assert r['recall'] == 1.0 and r['precision'] == 1.0

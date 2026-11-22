import pytest

from daemon.kg_ai.deepseek import DeepSeekError, TaskReject, failure_code, parse_json_content, stored_error


def test_bare_array_is_wrapped_under_named_envelope_key():
    assert parse_json_content('[{"id": "a", "decision": "approve"}]', list_key='reviews') == {
        'reviews': [{'id': 'a', 'decision': 'approve'}]}
    assert parse_json_content('[]', list_key='findings') == {'findings': []}
    assert parse_json_content('```json\n[{"query": "x"}]\n```', list_key='queries') == {'queries': [{'query': 'x'}]}


def test_object_answer_is_unchanged_with_list_key():
    assert parse_json_content('{"reviews": []}', list_key='reviews') == {'reviews': []}


@pytest.mark.parametrize('content', ['null', '1', '"hello"', 'true'])
def test_non_array_non_object_still_rejected_with_list_key(content):
    with pytest.raises(DeepSeekError) as rejected:
        parse_json_content(content, list_key='reviews')
    assert failure_code(stored_error(rejected.value)) == 'invalid_task_json'


def test_bare_array_without_list_key_still_rejected():
    with pytest.raises(DeepSeekError):
        parse_json_content('[{"ok": true}]')

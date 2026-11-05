import json
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_daemon import db as kgdb                                  # noqa: E402
from kg_daemon import ontology as onto                            # noqa: E402
from kg_daemon.claims import add_evidence, create_claim           # noqa: E402
from kg_daemon.runner import STAGES, Daemon, StageResult         # noqa: E402
from kg_daemon.seed_migration import migrate_seed                 # noqa: E402
from kg_daemon.stages import HANDLERS                             # noqa: E402

FIX = os.path.join(ROOT, 'tests', 'fixtures')


@pytest.fixture()
def world(tmp_path):
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    migrate_seed(c, json.load(open(os.path.join(FIX, 'seed_minimal.json'), encoding='utf-8')), '2026-10-27T00:00:00Z')
    root = tmp_path / 'root'
    (root / 'crawler' / 'out').mkdir(parents=True)
    vault = tmp_path / 'vault.json'
    shutil.copy(os.path.join(FIX, 'vault_minimal.json'), vault)
    env = {'root': str(root), 'vault_path': str(vault), 'raw_dir': str(tmp_path / 'raw'), 'today': '2026-10-27'}
    return c, env, root


def daemon(c, env, handlers=None, **cfg):
    sleeps = []
    d = Daemon(c, handlers or HANDLERS, {'backoff_base_sec': 1.0, **cfg}, env=env, sleep=sleeps.append)
    d.sleeps = sleeps
    return d


def counts(c):
    return {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in
            ('document', 'candidate_fact', 'claim', 'evidence', 'employment', 'restaurant_status_event', 'review_queue', 'conflict', 'service_output')}


def branch(c, name):
    return c.execute('SELECT b.branch_id FROM restaurant r JOIN branch b USING(restaurant_id) WHERE r.name=?', (name,)).fetchone()[0]


def chef(c, name):
    return c.execute('SELECT chef_id FROM chef WHERE display_name=?', (name,)).fetchone()[0]


def test_full_pipeline_runs_all_nine_stages(world):
    c, env, _ = world
    rep = daemon(c, env).run()
    assert rep['status'] == 'succeeded' and list(rep['stages']) == list(STAGES) and set(rep['stages'].values()) == {'done'}


def test_pipeline_applies_closure_with_two_independent_sources_as_verified(world):
    c, env, _ = world
    daemon(c, env).run()
    r2 = branch(c, '가상식당2')
    assert onto.branch_state_asof(c, r2, '2026-10-27')['state'] == 'closed'
    ev = c.execute("SELECT * FROM restaurant_status_event WHERE branch_id=? AND kind='close'", (r2,)).fetchall()
    assert len(ev) == 1                                           # same fact from two sources = one event, two evidence rows
    st = c.execute('SELECT status FROM claim WHERE claim_id=?', (ev[0]['claim_id'],)).fetchone()[0]
    assert st in ('source_claim', 'verified')
    assert c.execute('SELECT COUNT(*) FROM claim_evidence WHERE claim_id=?', (ev[0]['claim_id'],)).fetchone()[0] == 2


def _corroborate_move(env, repost=False):
    """A second trusted host reports the move. repost=True: it copies the first article word for word (syndication), which is NOT independent."""
    v = json.load(open(env['vault_path'], encoding='utf-8'))
    r = json.loads(json.dumps(next(x for x in v['records'] if x['record_id'] == 't:3')))
    r.update(record_id='t:3b', url='https://other-press.example.org/m', source_host='other-press.example.org')
    if not repost:
        r.setdefault('evidence_locator', {})['quote'] = '다른 매체가 직접 취재해 다른 문장으로 쓴 이직 기사 (independent wording)'
    v['records'].append(r)
    json.dump(v, open(env['vault_path'], 'w', encoding='utf-8'), ensure_ascii=False)


def test_single_source_move_is_held_not_applied(world):
    c, env, _ = world
    cc = chef(c, '테스트셰프C')
    before = [dict(r) for r in c.execute('SELECT employment_id, state, valid_to FROM employment WHERE chef_id=?', (cc,))]
    daemon(c, env).run()
    after = [dict(r) for r in c.execute('SELECT employment_id, state, valid_to FROM employment WHERE chef_id=?', (cc,))]
    assert after == before                                   # nothing closed on one press article
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='move_needs_verification'").fetchone()[0] == 1


def test_pipeline_chef_move_closes_old_and_adds_new_without_deleting(world):
    c, env, _ = world
    _corroborate_move(env)                                   # 2 independent trusted hosts -> verified move
    cc = chef(c, '테스트셰프C')
    before = c.execute('SELECT COUNT(*) FROM employment WHERE chef_id=?', (cc,)).fetchone()[0]
    daemon(c, env).run()
    rows = c.execute('SELECT * FROM employment WHERE chef_id=?', (cc,)).fetchall()
    assert len(rows) == before + 1
    assert [e['branch_id'] for e in onto.current_employment(c, cc, '2026-10-27')] == [branch(c, '가상식당1')]
    assert any(r['state'] == 'closed' and r['valid_to'] == '2026-10-10' for r in rows)
    assert onto.current_employment(c, cc, '2026-09-01')[0]['branch_id'] == branch(c, '가상식당2')    # history preserved


def test_held_move_applies_once_corroborated_on_a_later_run(world):
    c, env, _ = world
    cc = chef(c, '테스트셰프C')
    daemon(c, env).run()
    assert [e['branch_id'] for e in onto.current_employment(c, cc, '2026-10-27')] != [branch(c, '가상식당1')]
    _corroborate_move(env)
    daemon(c, env).run(resume=False)
    assert [e['branch_id'] for e in onto.current_employment(c, cc, '2026-10-27')] == [branch(c, '가상식당1')]


def test_pipeline_never_guesses_identity(world):
    c, env, _ = world
    daemon(c, env).run()
    reasons = {r['reason'] for r in c.execute("SELECT reason FROM review_queue WHERE state='open'")}
    assert {'homonym', 'unknown_chef'} <= reasons
    assert c.execute("SELECT COUNT(*) FROM candidate_fact WHERE resolution='needs_review'").fetchone()[0] >= 2
    assert c.execute("SELECT COUNT(*) FROM employment WHERE seed_ref LIKE 'cand:%'").fetchone()[0] == 0      # none of them was materialised


def test_service_outputs_written_and_upserted(world):
    c, env, _ = world
    _corroborate_move(env)
    daemon(c, env).run()
    n = counts(c)['service_output']
    assert n > 0
    out = c.execute("SELECT payload_json FROM service_output WHERE output_key='current_employment' AND subject_id=?", (chef(c, '테스트셰프C'),)).fetchone()[0]
    assert json.loads(out)['current']


def test_rerun_is_idempotent(world):
    c, env, _ = world
    daemon(c, env).run()
    first = counts(c)
    daemon(c, env).run()
    second = counts(c)
    assert second == first


def test_lock_is_single_flight_and_expired_lock_is_taken_over(world):
    c, env, _ = world
    a, b = daemon(c, env), daemon(c, env)
    assert a.acquire_lock() is True
    assert b.run() == {'status': 'locked', 'run_id': None}
    c.execute("UPDATE run_lock SET expires_at='2000-01-01T00:00:00Z'")
    assert b.run()['status'] == 'succeeded'
    assert c.execute('SELECT COUNT(*) FROM daemon_run').fetchone()[0] == 1


def flaky(stage, fail_times, calls):
    def h(ctx):
        calls[stage] = calls.get(stage, 0) + 1
        if calls[stage] <= fail_times:
            raise RuntimeError('transient')
        return HANDLERS[stage](ctx)
    return h


def test_transient_failure_retries_with_capped_backoff_then_succeeds(world):
    c, env, _ = world
    calls = {}
    d = daemon(c, env, {**HANDLERS, 'extraction': flaky('extraction', 2, calls)}, backoff_max_sec=1.5)
    assert d.run()['status'] == 'succeeded'
    assert calls['extraction'] == 3 and d.sleeps == [1.0, 1.5]       # 1, min(2, cap 1.5)


def test_permanent_failure_is_bounded_goes_to_failure_queue_and_resumes_from_failed_stage(world):
    c, env, _ = world
    calls = {}
    d = daemon(c, env, {**HANDLERS, 'entity_resolution': flaky('entity_resolution', 99, calls)}, max_attempts=3)
    rep = d.run()
    assert rep['status'] == 'failed' and rep['failed_stage'] == 'entity_resolution' and calls['entity_resolution'] == 3
    assert rep['stages']['extraction'] == 'done' and rep['stages']['entity_resolution'] == 'failed' and rep['stages']['quality_check'] == 'pending'
    fq = c.execute("SELECT * FROM failure_queue WHERE state='open'").fetchall()
    assert len(fq) == 1 and fq[0]['stage'] == 'entity_resolution' and fq[0]['attempts'] == 3
    # fix + rerun: earlier stages are NOT re-run
    ran = []
    wrapped = {s: (lambda s: (lambda ctx: (ran.append(s), HANDLERS[s](ctx))[1]))(s) for s in STAGES}
    rep2 = daemon(c, env, wrapped).run()
    assert rep2['status'] == 'succeeded' and rep2['resumed_from'] == rep['run_id']
    assert ran == list(STAGES[STAGES.index('entity_resolution'):])


def test_non_retryable_invariant_violation_fails_immediately(world):
    c, env, _ = world
    cl = create_claim(c, 'employment', 'source_claim', 'test')                      # source_claim with no evidence = invariant violation
    rep = daemon(c, env).run()
    assert rep['status'] == 'failed' and rep['failed_stage'] == 'quality_check'
    assert c.execute("SELECT attempts FROM failure_queue").fetchone()[0] == 1       # no retries for deterministic failures
    assert 'claim_without_evidence' in c.execute("SELECT error FROM failure_queue").fetchone()[0] and cl


def paid_fetcher(n):
    def f(sid, s, ctx):
        if sid != 'official_sites':
            return
        for i in range(n):
            ctx.cost.charge('http_request', 1.0)
            yield {'url': f'https://official.example.com/p{i}', 'doc_key': '', 'published_at': '2026-10-20', 'collected_at': '2026-10-26',
                   'body': {'target_id': 'r1', 'kind': 'notice', 'title': f'공지 {i}', 'extracted_facts': {}, 'evidence_locator': {}}}
    return f


def test_cost_cap_pauses_resumably_and_a_raised_cap_completes(world):
    c, env, _ = world
    env['fetchers'] = {'http_fetch': paid_fetcher(5)}
    rep = daemon(c, env, cost_cap_units=3).run()
    assert rep['status'] == 'paused_cost_cap' and rep['failed_stage'] == 'incremental_fetch' and rep['cost_used'] == 3
    assert c.execute("SELECT COUNT(*) FROM failure_queue").fetchone()[0] == 0           # a pause is not a failure
    assert c.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0] == 3
    rep2 = daemon(c, env, cost_cap_units=50).run()
    assert rep2['status'] == 'succeeded' and rep2['resumed_from'] == rep['run_id']
    assert c.execute("SELECT COUNT(*) FROM document WHERE url LIKE 'https://official.example.com/p%'").fetchone()[0] == 5   # no dupes, none lost


def test_instagram_paused_by_flag_is_skipped_and_never_worked_around(world):
    c, env, root = world
    (root / 'crawler' / 'out' / 'ig_blocked.flag').write_text(json.dumps({'reason': 'LoginException: checkpoint', 'at': '2026-10-24T07:12:24+09:00'}))
    (root / 'crawler' / 'out' / 'ig-20261026.json').write_text(json.dumps([{'id': 'r1', 'kind': 'closure', 'url': 'https://www.instagram.com/p/FAKE', 'date': '2026-10-26', 'title': '곧 영업 종료'}]))
    rep = daemon(c, env).run()
    assert rep['status'] == 'succeeded'
    out = json.loads(c.execute("SELECT output_json FROM stage_checkpoint WHERE stage='source_discovery'").fetchone()[0])
    assert 'instagram_public' in out['paused'] and 'instagram_public' not in out['active']
    assert c.execute("SELECT COUNT(*) FROM document WHERE source_id='instagram_public'").fetchone()[0] == 0
    assert (root / 'crawler' / 'out' / 'ig_blocked.flag').exists()                   # the daemon never clears the flag


def test_instagram_candidates_stay_unverified_and_do_not_change_public_state(world):
    c, env, root = world
    (root / 'crawler' / 'out' / 'ig-20261026.json').write_text(json.dumps([{'id': 'r1', 'kind': 'closure', 'url': 'https://www.instagram.com/p/FAKE', 'date': '2026-10-26', 'title': '곧 영업 종료'}]))
    daemon(c, env).run()
    cand = c.execute("SELECT * FROM candidate_fact WHERE source_id='instagram_public'").fetchone()
    assert cand['verification'] == 'unverified' and cand['asserted_by'] == 'source'
    r1 = branch(c, '가상식당1')
    assert onto.branch_state_asof(c, r1, '2026-10-27')['state'] != 'closed'
    ev = c.execute("SELECT e.effective_date, cl.status FROM restaurant_status_event e JOIN claim cl USING(claim_id) WHERE e.branch_id=? AND e.kind='close'", (r1,)).fetchall()
    assert [(e['effective_date'], e['status']) for e in ev] == [(None, 'unverified')]      # recorded as a lead; undated, so it cannot move any as-of state


def test_all_sources_paused_pauses_the_run_instead_of_failing(world):
    c, env, _ = world
    import copy
    from kg_daemon.registry import load_registry
    reg = load_registry()
    for s in reg['sources'].values():
        s['collect_permission'] = 'manual_only' if s['collect_permission'] != 'prohibited' else 'prohibited'
        s['adapter'] = 'manual_findings' if s['collect_permission'] == 'manual_only' else 'none'
    env['registry'] = copy.deepcopy(reg)
    rep = daemon(c, env).run()
    assert rep['status'] == 'paused_source' and rep['failed_stage'] == 'source_discovery'


def test_ai_inference_candidates_are_never_materialised(world):
    c, env, _ = world
    env['ai_extractor'] = lambda doc, body: [('closure', {'target_id': 'r1', 'effective_date': '2026-10-01'}, 'AI가 추정한 폐업')] if doc['source_id'] == 'press_articles' and body.get('target_id') == 'r2' else []
    daemon(c, env).run()
    ai = c.execute("SELECT * FROM candidate_fact WHERE asserted_by='ai_inference'").fetchall()
    assert ai and all(a['verification'] == 'ai_inference' and a['applied_claim_id'] is None for a in ai)
    assert onto.branch_state_asof(c, branch(c, '가상식당1'), '2026-10-27')['state'] != 'closed'
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference'").fetchone()[0] >= 1
    # An injected fixture hook makes no paid call; only the shared HTTP client charges measured usage.
    assert c.execute("SELECT COUNT(*) FROM cost_ledger WHERE kind='llm_tokens_k'").fetchone()[0] == 0


def test_modified_source_after_apply_queues_review_and_keeps_old_claim(world):
    c, env, _ = world
    daemon(c, env).run()
    claims_before = c.execute('SELECT COUNT(*) FROM claim').fetchone()[0]
    v = json.load(open(env['vault_path'], encoding='utf-8'))
    v['records'][0]['extracted_facts']['closure'] = '2026-09-15'                    # the article was edited: different date
    v['records'][0]['evidence_locator']['quote'] = '9월 15일자로 영업을 종료한다'
    json.dump(v, open(env['vault_path'], 'w', encoding='utf-8'), ensure_ascii=False)
    daemon(c, env).run()
    r2 = branch(c, '가상식당2')
    assert c.execute("SELECT COUNT(*) FROM restaurant_status_event WHERE branch_id=? AND kind='close'", (r2,)).fetchone()[0] == 2      # both kept
    assert c.execute("SELECT COUNT(*) FROM conflict WHERE state='open'").fetchone()[0] >= 1                                           # disagreement preserved
    assert c.execute('SELECT COUNT(*) FROM claim').fetchone()[0] > claims_before


def test_missing_network_adapters_are_reported_as_stubs_not_silently_skipped(world):
    c, env, _ = world
    daemon(c, env).run()
    out = json.loads(c.execute("SELECT output_json FROM stage_checkpoint WHERE stage='incremental_fetch'").fetchone()[0])
    assert 'official_sites' in out['stubbed_adapters']


def test_handler_set_must_cover_every_stage(world):
    c, env, _ = world
    with pytest.raises(ValueError):
        Daemon(c, {k: v for k, v in HANDLERS.items() if k != 'extraction'})


def test_stageresult_roundtrip_is_json(world):
    assert StageResult({'a': 1}).output == {'a': 1}


def test_rename_is_a_dated_status_event_and_never_changes_the_display_name(world):
    c, env, _ = world
    v = json.load(open(env['vault_path'], encoding='utf-8'))
    v['records'].append({'record_id': 't:9', 'url': 'https://press.example.com/n9', 'title': '개명', 'source_host': 'press.example.com', 'source_tier': 'press',
                         'published_at': '2026-10-06', 'collected_at': '2026-10-26', 'target_id': 'r8', 'kind': 'rename',
                         'extracted_facts': {'new_name': '새이름식당'}, 'evidence_locator': {'quote': '새이름식당으로 이름을 바꿨다'}})
    json.dump(v, open(env['vault_path'], 'w', encoding='utf-8'), ensure_ascii=False)
    daemon(c, env).run()
    b = branch(c, '가상식당8')
    ev = c.execute("SELECT detail FROM restaurant_status_event WHERE branch_id=? AND kind='rename'", (b,)).fetchall()
    assert len(ev) == 1 and '새이름식당' in ev[0]['detail']
    assert c.execute("SELECT COUNT(*) FROM restaurant WHERE name='가상식당8'").fetchone()[0] == 1


def _out(c, key, subject):
    r = c.execute('SELECT payload_json FROM service_output WHERE output_key=? AND subject_id=? ORDER BY as_of DESC LIMIT 1', (key, subject)).fetchone()
    return json.loads(r[0]) if r else None


def test_verified_move_propagates_to_detail_career_map_and_recommendations(world):
    c, env, _ = world
    _corroborate_move(env)
    daemon(c, env).run()
    cc, new_b, old_b = chef(c, '테스트셰프C'), branch(c, '가상식당1'), branch(c, '가상식당2')
    cm = _out(c, 'career_map', cc)
    assert [e['branch_id'] for e in cm['current']] == [new_b]
    assert old_b in [e['branch_id'] for e in cm['past']] and old_b not in [e['branch_id'] for e in cm['current']]
    assert cc in [x['chef_id'] for x in _out(c, 'restaurant_detail', new_b)['current_chefs']]
    assert cc not in [x['chef_id'] for x in _out(c, 'restaurant_detail', old_b)['current_chefs']]
    assert cc in [x['chef_id'] for x in _out(c, 'restaurant_detail', old_b)['former_chefs']]
    assert cc in _out(c, 'recommendation_eligibility', new_b)['chef_ids']


def test_collector_bridge_rules():
    from kg_daemon import collect_bridge as cb
    base = {'extraction_id': 'e1', 'span_start': 0, 'span_end': 10, 'extractor': 'rule:x', 'extractor_version': 'rules/1.3', 'role_text': None,
            'tenure_from': None, 'tenure_to': None, 'org_text': None, 'published_at': '2026-10-01', 'date_text': None}
    ch, rs = {'name_ko': '가셰프', 'seed_id': None}, {'name_ko': '가식당', 'seed_id': 'r1'}
    mk = lambda **k: {'collect': {**base, **k}, 'tier': k.pop('tier', 'press') if 'tier' in k else 'press', 'chef': ch, 'restaurant': rs, 'org': None}  # noqa: E731
    # co-working / guest event -> event, never mentorship
    out = cb.candidates(mk(label='collab', epistemic='claim', ambiguity=None, quote='가셰프와 함께 일했다'), '2026-10-28')
    assert [o[0] for o in out] == ['collab']
    # employment claim without a move verb is NOT a move; old article cannot open a current tenure
    out = cb.candidates(mk(label='employment_claim', epistemic='claim', ambiguity=None, quote='가식당의 가셰프', published_at='2019-01-01'), '2026-10-28')
    assert out[0][0] == 'employment' and out[0][1]['current'] is False
    out = cb.candidates(mk(label='employment_claim', epistemic='claim', ambiguity=None, quote='가셰프가 가식당으로 옮겼다'), '2026-10-28')
    assert out[0][0] == 'chef_move'
    # search snippets / hearsay stay leads; negated/planned statements are not candidates
    out = cb.candidates(mk(label='opening', epistemic='unverified', ambiguity=None, quote='가식당 오픈'), '2026-10-28')
    assert out[0][1]['lead_only'] is True
    assert cb.candidates(mk(label='closure', epistemic='claim', ambiguity='negated_statement', quote='폐업 아니다'), '2026-10-28')[0][0] == 'ambiguous'
    assert cb.candidates(mk(label='closure', epistemic='claim', ambiguity='temporary_closure', quote='휴업'), '2026-10-28')[0][0] == 'pause'


def test_reposted_article_is_one_independent_source():
    from kg_daemon.claims import independent_sources
    q = '김하늘 셰프가 노을의 총괄 셰프로 합류했다고 밝혔다.'
    assert independent_sources([('https://a.example/1', q), ('https://b.example/9', q)]) == 1          # same wording on two hosts = a repost
    assert independent_sources([('https://a.example/1', q), ('https://b.example/9', '노을은 새 총괄 셰프로 김하늘을 영입했다.')]) == 2
    assert independent_sources([('https://a.example/1', q), ('https://a.example/2', '다른 문장으로 쓴 같은 매체 기사입니다.')]) == 1


def test_collect_plan_with_zero_budget_does_not_divide_by_zero():
    import sqlite3
    from kg_collect import scheduler
    from kg_daemon.db import migrate
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    migrate(c)
    pl = scheduler.plan(c, 0)
    assert all(v == 0 for v in pl['budgets'].values())


def test_verbatim_repost_does_not_verify_a_move(world):
    c, env, _ = world
    _corroborate_move(env, repost=True)
    cc = chef(c, '테스트셰프C')
    daemon(c, env).run()
    assert [e['branch_id'] for e in onto.current_employment(c, cc, '2026-10-27')] != [branch(c, '가상식당1')]   # still held: one source, reposted


def test_effective_date_accepts_collector_iso_date_text():
    from kg_daemon.collect_bridge import effective_date
    assert effective_date({'label': 'opening', 'date_text': '2024-11-21', 'published_at': '2024-10-30'}) == '2024-11-21'
    assert effective_date({'label': 'employment_claim', 'date_text': '2025-03', 'published_at': '2025-03-03'}) == '2025-03'
    assert effective_date({'label': 'collab', 'date_text': None, 'published_at': '2025-03-03'}) is None


class _Crash(BaseException):
    """Simulates the process dying mid-stage (not an Exception, so the runner cannot record it)."""


def test_resumed_crashed_run_is_closed_as_abandoned_not_left_running(world):
    c, env, _ = world

    def crash(ctx):
        raise _Crash()
    with pytest.raises(_Crash):
        daemon(c, env, {**HANDLERS, 'entity_resolution': crash}).run()
    dead = c.execute("SELECT * FROM daemon_run").fetchone()
    assert dead['status'] == 'running' and dead['finished_at'] is None
    rep = daemon(c, env).run()
    assert rep['status'] == 'succeeded' and rep['resumed_from'] == dead['run_id']
    old = c.execute('SELECT * FROM daemon_run WHERE run_id=?', (dead['run_id'],)).fetchone()
    new = c.execute('SELECT * FROM daemon_run WHERE run_id=?', (rep['run_id'],)).fetchone()
    assert old['status'] == 'abandoned' and old['finished_at'] == new['started_at']
    assert c.execute("SELECT COUNT(*) FROM daemon_run WHERE status='running'").fetchone()[0] == 0


def test_close_superseded_runs_repairs_legacy_rows_and_keeps_live_or_final_ones(world):
    c, env, _ = world
    ins = 'INSERT INTO daemon_run(run_id,daemon,status,trigger,cost_cap_units,resumed_from,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?)'
    c.execute(ins, ('run_stale', 'kg-daemon', 'running', 'manual', 10, None, '2026-10-28T01:25:13Z', None))
    c.execute(ins, ('run_next', 'kg-daemon', 'succeeded', 'manual', 10, 'run_stale', '2026-10-28T01:55:26Z', '2026-10-28T01:58:04Z'))
    c.execute(ins, ('run_failed', 'kg-daemon', 'failed', 'manual', 10, None, '2026-10-27T01:00:00Z', '2026-10-27T01:05:00Z'))
    c.execute(ins, ('run_after_fail', 'kg-daemon', 'succeeded', 'manual', 10, 'run_failed', '2026-10-27T02:00:00Z', '2026-10-27T02:05:00Z'))
    c.execute(ins, ('run_other', 'kg-collect', 'running', 'manual', 10, None, '2026-10-28T01:00:00Z', None))
    c.execute(ins, ('run_other_next', 'kg-collect', 'running', 'manual', 10, 'run_other', '2026-10-28T02:00:00Z', None))
    d = daemon(c, env)
    assert d.close_superseded_runs() == 1 and d.close_superseded_runs() == 0          # idempotent, only this daemon's rows
    row = lambda r: tuple(c.execute('SELECT status, finished_at FROM daemon_run WHERE run_id=?', (r,)).fetchone())  # noqa: E731
    assert row('run_stale') == ('abandoned', '2026-10-28T01:55:26Z')
    assert row('run_failed') == ('failed', '2026-10-27T01:05:00Z')                      # final statuses are history, untouched
    assert row('run_other') == ('running', None)                                        # another daemon's chain is its own business

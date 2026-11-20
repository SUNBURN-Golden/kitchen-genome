import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_daemon import db as kgdb                                                           # noqa: E402
from kg_daemon.registry import RegistryError, load_registry, pause_state, sync_registry, validate_registry  # noqa: E402


@pytest.fixture()
def reg():
    return load_registry()


def test_registry_loads_and_every_source_states_its_limits(reg):
    assert len(reg['sources']) >= 10
    for sid, s in reg['sources'].items():
        assert s['unsupported_scope'], sid


def test_instagram_is_registered_as_required_candidate_source_with_no_evasion(reg):
    ig = reg['sources']['instagram_kitchen_genome']
    assert ig['role'] == 'required_candidate_discovery'
    assert ig['trust_ceiling'] == 'unverified'
    assert ig['block_evasion'] == 'forbidden' and reg['defaults']['block_evasion'] == 'forbidden'
    assert {'checkpoint', 'http_429'} <= set(ig['pause_on'])
    assert any('NOT implemented' in x for x in ig['unsupported_scope'])


def test_prohibited_sources_have_no_adapter_and_no_fields(reg):
    for sid, s in reg['sources'].items():
        if s['collect_permission'] == 'prohibited':
            assert s['adapter'] == 'none' and not s.get('collectible_fields'), sid


@pytest.mark.parametrize('mutate,needle', [
    (lambda r: r['sources']['instagram_kitchen_genome'].update(block_evasion='allowed'), 'block_evasion'),
    (lambda r: r['sources']['instagram_kitchen_genome'].update(trust_ceiling='verified'), 'trust_ceiling'),
    (lambda r: r['sources']['instagram_kitchen_genome'].update(pause_on=['http_429']), 'checkpoint'),
    (lambda r: r['sources']['instagram_kitchen_genome'].update(pause_flag=None), 'pause_flag'),
    (lambda r: r['sources'].pop('instagram_public'), 'instagram_public must be registered'),
    (lambda r: r['sources']['instagram_kitchen_genome']['unsupported_scope'].append('rotate accounts when blocked'), 'bypass'),
    (lambda r: r['sources']['community_threads'].update(adapter='http_fetch'), 'automated adapter'),
    (lambda r: r['sources']['seed_legacy'].update(unsupported_scope=[]), 'unsupported_scope'),
])
def test_registry_validation_rejects_unsafe_changes(reg, mutate, needle):
    bad = copy.deepcopy(reg)
    mutate(bad)
    with pytest.raises(RegistryError, match=needle):
        validate_registry(bad)


def test_pause_flag_pauses_and_clearing_it_resumes(reg, tmp_path):
    ig = reg['sources']['instagram_kitchen_genome']
    assert pause_state('instagram_kitchen_genome', ig, str(tmp_path)) is None
    flag = tmp_path / ig['pause_flag']
    flag.parent.mkdir(parents=True)
    flag.write_text(json.dumps({'reason': 'LoginException: checkpoint', 'at': '2026-10-24T07:12:24+09:00'}))
    st = pause_state('instagram_kitchen_genome', ig, str(tmp_path))
    assert st['reason'].endswith('checkpoint') and st['since'].startswith('2026-10-24')
    flag.unlink()
    assert pause_state('instagram_kitchen_genome', ig, str(tmp_path)) is None


def test_legacy_block_flag_pauses_kitchen_genome_without_clearing_it(reg, tmp_path):
    kg = reg['sources']['instagram_kitchen_genome']
    assert kg['access_mode'] == 'saved_session_only' and kg['account'] == 'kitchen.genome'
    assert kg['pause_flag'] == 'crawler/out/ig_blocked.flag'
    assert pause_state('instagram_kitchen_genome', kg, str(tmp_path)) is None
    flag = tmp_path / 'crawler' / 'out' / 'ig_blocked.flag'
    flag.parent.mkdir(parents=True)
    flag.write_text('legacy-paused')
    paused = pause_state('instagram_kitchen_genome', kg, str(tmp_path))
    assert paused['flag'].endswith('ig_blocked.flag') and paused['reason'] == 'flag present'
    assert flag.read_text() == 'legacy-paused'


def test_sync_registry_enables_only_permitted_sources(reg):
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    sync_registry(c, reg)
    enabled = {r['source_id'] for r in c.execute('SELECT source_id FROM source WHERE enabled=1')}
    assert 'instagram_public' not in enabled and {'michelin_manual', 'instagram_kitchen_genome'} <= enabled
    assert all(reg['sources'][s]['collect_permission'] in ('allowed', 'allowed_with_limits') for s in enabled)
    n = c.execute('SELECT COUNT(*) FROM source').fetchone()[0]
    sync_registry(c, reg)
    assert c.execute('SELECT COUNT(*) FROM source').fetchone()[0] == n          # idempotent



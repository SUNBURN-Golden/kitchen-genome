"""Load + validate sources/registry.yaml and sync it into the service DB (source, source_policy)."""
import json
import os

import yaml

from .db import ROOT, utcnow

REGISTRY_PATH = os.path.join(ROOT, 'sources', 'registry.yaml')
IG_FLAG = os.path.join(ROOT, 'crawler', 'out', 'ig_blocked.flag')

PERMISSIONS = ('allowed', 'allowed_with_limits', 'manual_only', 'prohibited')
TIERS = ('official', 'press', 'specialist_blog', 'event_notice', 'social_lead', 'search_lead', 'human')
KINDS = ('official_site', 'press', 'rss', 'sitemap', 'social', 'guide', 'community', 'manual_clip', 'outreach', 'seed')
REQUIRED = ('name', 'kind', 'tier', 'role', 'collect_permission', 'collectible_fields', 'refresh_cadence', 'retention_days', 'unsupported_scope', 'adapter')
BYPASS_WORDS = ('rotate', 'switch account', 'switching', 'proxy', 'impersonat', 'captcha')


class RegistryError(ValueError):
    pass


def load_registry(path: str = REGISTRY_PATH) -> dict:
    reg = yaml.safe_load(open(path, encoding='utf-8'))
    validate_registry(reg)
    return reg


def validate_registry(reg: dict) -> None:
    errs = []
    if not reg.get('version'):
        errs.append('version missing')
    srcs = reg.get('sources') or {}
    for sid, s in srcs.items():
        for k in REQUIRED:
            if k not in s:
                errs.append(f'{sid}: missing {k}')
        if s.get('collect_permission') not in PERMISSIONS:
            errs.append(f'{sid}: bad collect_permission')
        if s.get('tier') not in TIERS:
            errs.append(f'{sid}: bad tier')
        if s.get('kind') not in KINDS:
            errs.append(f'{sid}: bad kind')
        if not isinstance(s.get('unsupported_scope'), list) or not s.get('unsupported_scope'):
            errs.append(f'{sid}: unsupported_scope must be a non-empty list (limits are stated, not hidden)')
        if s.get('collect_permission') in ('prohibited', 'manual_only') and s.get('adapter') not in ('none', 'manual_findings', 'crawler_inbox'):
            errs.append(f'{sid}: {s.get("collect_permission")} source must not have an automated adapter')
        if s.get('collect_permission') == 'prohibited' and s.get('collectible_fields'):
            errs.append(f'{sid}: prohibited source lists collectible fields')
    ig = srcs.get('instagram_public')
    if not ig:
        errs.append('instagram_public must be registered (required candidate-discovery source)')
    else:
        if ig.get('role') != 'required_candidate_discovery':
            errs.append('instagram_public.role must be required_candidate_discovery')
        if ig.get('block_evasion') != 'forbidden':
            errs.append('instagram_public.block_evasion must be forbidden')
        if ig.get('trust_ceiling') != 'unverified':
            errs.append('instagram_public.trust_ceiling must be unverified')
        for sig in ('checkpoint', 'http_429'):
            if sig not in (ig.get('pause_on') or []):
                errs.append(f'instagram_public.pause_on must include {sig}')
        if not ig.get('pause_flag'):
            errs.append('instagram_public.pause_flag missing')
        # unsupported_scope may NAME bypass techniques only to state they are not implemented.
        for item in ig.get('unsupported_scope', []):
            if any(w in item.lower() for w in BYPASS_WORDS) and 'not implemented' not in item.lower():
                errs.append(f'instagram_public: unsupported_scope mentions bypass without stating it is not implemented: {item}')
    if errs:
        raise RegistryError('; '.join(errs))


def pause_state(source_id: str, source: dict, root: str = ROOT) -> dict | None:
    """Return {'reason':..., 'since':...} if the source is paused by a human-clearable flag, else None."""
    flag = source.get('pause_flag')
    if not flag:
        return None
    p = flag if os.path.isabs(flag) else os.path.join(root, flag)
    if not os.path.exists(p):
        return None
    try:
        data = json.load(open(p, encoding='utf-8'))
    except Exception:
        data = {}
    return {'reason': data.get('reason', 'flag present'), 'since': data.get('at'), 'flag': flag}


def sync_registry(conn, reg: dict, now: str | None = None) -> int:
    """Upsert source + source_policy rows. Enabled iff permission is allowed/allowed_with_limits."""
    now = now or utcnow()
    n = 0
    for sid, s in reg['sources'].items():
        enabled = 1 if s['collect_permission'] in ('allowed', 'allowed_with_limits') else 0
        host = (s.get('hosts') or [None])[0]
        conn.execute("""INSERT INTO source(source_id,name,kind,host,tier,enabled,created_at) VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(source_id) DO UPDATE SET name=excluded.name, kind=excluded.kind, host=excluded.host, tier=excluded.tier, enabled=excluded.enabled""",
                     (sid, s['name'], s['kind'], host, s['tier'], enabled, now))
        conn.execute("""INSERT INTO source_policy(source_id,collect_permission,collectible_fields,unsupported_scope,refresh_cadence,retention_days,quote_max_chars,fulltext_allowed,pause_on,registry_version,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(source_id) DO UPDATE SET collect_permission=excluded.collect_permission, collectible_fields=excluded.collectible_fields,
                          unsupported_scope=excluded.unsupported_scope, refresh_cadence=excluded.refresh_cadence, retention_days=excluded.retention_days,
                          quote_max_chars=excluded.quote_max_chars, fulltext_allowed=excluded.fulltext_allowed, pause_on=excluded.pause_on,
                          registry_version=excluded.registry_version, updated_at=excluded.updated_at""",
                     (sid, s['collect_permission'], json.dumps(s['collectible_fields'], ensure_ascii=False),
                      json.dumps(s['unsupported_scope'], ensure_ascii=False), s['refresh_cadence'], s['retention_days'],
                      s.get('quote_max_chars', reg.get('defaults', {}).get('quote_max_chars', 200)), 1 if s.get('fulltext_allowed') else 0,
                      json.dumps(s.get('pause_on', [])), reg['version'], now))
        n += 1
    conn.commit()
    return n


TIER_TO_SOURCE = {'official': 'official_sites', 'press': 'press_articles', 'specialist_blog': 'specialist_blogs',
                  'event_notice': 'press_articles', 'search_lead': 'search_leads'}

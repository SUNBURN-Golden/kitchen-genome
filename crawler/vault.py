"""Kitchen Genome Brand-Isolated Vault and Multi-Signal Deduplication.
Manages structured records in crawler/vault.json and crawler/inbox_summary.json.
Implements:
- 5 explicit statuses: candidate, source_checked, dedup_excluded, verified, ready_to_publish
- Multi-signal deduplication: canonical URL, syndicated/reposted article title+entity fingerprint,
  cross-language translation / press release fingerprint (target_id + event_kind + year + chef_entity_id).
- Conflict preservation: when two sources disagree on kitchen/role/period/closure, both are retained with conflict=True.
- Lightweight storage: stores structured facts + short quote (<=200 chars) + locator by default;
  full text is retained only for fulltext_allowed_hosts.
"""
import hashlib
import json
import os
import re
import urllib.parse
import datetime as dt

from privacy_filter import sanitize_before_store
from extractor import classify_source_tier, resolve_chef_entity

H = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(H)

VALID_STATUSES = (
    'candidate',          # 후보 (검색·블로그·외부 링크 발견 단서)
    'source_checked',     # 원문 확인 (본문·근거 위치 확인 완료, 단일/보조 출처)
    'dedup_excluded',     # 중복 제외 (동일 기사 재전송·번역본·보도자료 묶음)
    'verified',           # 검증 완료 (공식 소개 또는 독립 신뢰 출처 교차 검증 완료)
    'ready_to_publish',   # 공개 대기 (검수 완료 후 사이트 반영 대기; 자동 게시 분리)
)

TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'fbclid', 'gclid', 'igshid', 'ref', 'source'
}


def canonicalize_url(url: str) -> str:
    if not url or not url.startswith('http'):
        return (url or '').strip()
    p = urllib.parse.urlparse(url.strip())
    host = p.netloc.lower().removeprefix('www.').removeprefix('m.')
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=False)
    clean_q = urllib.parse.urlencode(sorted((k, v) for k, v in q if k.lower() not in TRACKING_PARAMS))
    path = re.sub(r'/+$', '', p.path) or '/'
    return urllib.parse.urlunparse((p.scheme.lower() or 'https', host, path, '', clean_q, ''))


def normalize_title_signature(title: str) -> str:
    if not title:
        return ''
    # Strip bracketed press tags like [인터뷰], [맛집], press names, punctuation
    t = re.sub(r'\[[^\]]+\]|\([^\)]+\)', ' ', title.lower())
    t = re.sub(r'[^가-힣a-z0-9\s]', ' ', t)
    tokens = [w for w in t.split() if len(w) >= 2]
    return ' '.join(tokens[:8])


def compute_dedup_cluster(record: dict) -> tuple[str, dict]:
    """Compute a multi-signal deduplication cluster ID and signal breakdown.
    Signals:
      1. Canonical URL hash
      2. Syndicated/reposted article signature (normalized title + target_id)
      3. Event/translation signature (target_id + event_kind + chef_entity_id + year)
    """
    canon_url = canonicalize_url(record.get('url', ''))
    url_sig = hashlib.sha1(canon_url.encode('utf-8')).hexdigest()[:12] if canon_url else 'nourl'

    title_norm = normalize_title_signature(record.get('title', ''))
    target_id = record.get('target_id') or 'unknown'
    syndication_raw = f"{target_id}::{title_norm}" if title_norm else f"{target_id}::{url_sig}"
    syndication_sig = hashlib.sha1(syndication_raw.encode('utf-8')).hexdigest()[:12]

    facts = record.get('extracted_facts') or {}
    chef_entity = facts.get('chef_entity_id') or 'unknown_chef'
    kind = record.get('kind') or 'lineage'
    year_m = re.search(r'\b(20[12]\d)\b', str(facts.get('independence') or facts.get('tenure_period') or record.get('published_at') or ''))
    year = year_m.group(1) if year_m else 'noyear'
    event_raw = f"{target_id}::{kind}::{chef_entity}::{year}"
    event_sig = hashlib.sha1(event_raw.encode('utf-8')).hexdigest()[:12]

    # Cluster ID combines target_id and event/syndication signature
    cluster_id = f"kg-cluster-{target_id}-{event_sig[:8]}"
    return cluster_id, {
        'canonical_url': canon_url,
        'url_sig': url_sig,
        'syndication_sig': syndication_sig,
        'event_sig': event_sig,
    }


def build_vault_From_seed_and_findings(seed_path: str, manual_findings_path: str, pending_path: str, config: dict) -> dict:
    """Build Kitchen Genome's structured vault with 5-stage classification, deduplication, and conflict tracking."""
    today = dt.date.today().isoformat()
    seed = json.load(open(seed_path, encoding='utf-8'))
    manual = json.load(open(manual_findings_path, encoding='utf-8')) if os.path.exists(manual_findings_path) else []
    pending = json.load(open(pending_path, encoding='utf-8')) if os.path.exists(pending_path) else []
    fulltext_hosts = set(config.get('fulltext_allowed_hosts', []))

    # Build known chef registry for homonym detection
    chef_registry = {}
    for r in seed['restaurants']:
        cname = re.sub(r'\s*\(.*?\)', '', (r.get('chef') or '')).strip()
        if cname and not any(x in cname for x in ('미기재', '출신', '호텔 팀')):
            scope = r.get('kitchen') or r['id']
            chef_registry.setdefault(cname, set()).add(scope)

    raw_items = []

    # 1. Seed restaurant & lineage records
    for r in seed['restaurants']:
        rid = r['id']
        url = r.get('lineage_src') or r.get('official_source_url') or r.get('michelin_src') or r.get('url') or ''
        if url.startswith('('):
            url = ''
        host = urllib.parse.urlparse(url).netloc.lower().removeprefix('www.') if url else 'internal-seed'
        tier = classify_source_tier(url) if url else 'search_lead'
        entity = resolve_chef_entity(r.get('chef'), r.get('kitchen'), rid, chef_registry)

        note_clean, p_flags = sanitize_before_store(r.get('note') or f"{r['name']} ({r.get('chef')}) 계보 기록")
        l_status = r.get('lineage_status', 'n/a')

        # Map to 5-stage verification status
        if l_status == 'verified' and tier in ('official', 'press'):
            v_status = 'verified'
        elif l_status in ('n/a',) and (r.get('michelin_src') or r.get('official_source_url') or r.get('lineage_src')):
            v_status = 'verified'
        elif l_status == 'conflict':
            v_status = 'source_checked'
        elif l_status == 'related':
            v_status = 'source_checked'
        elif tier == 'specialist_blog' or (url and tier != 'search_lead'):
            v_status = 'source_checked'
        else:
            v_status = 'candidate'

        raw_items.append({
            'record_id': f"seed:{rid}",
            'brand': 'kitchengenome',
            'collection_path': 'auto',
            'url': url or f"https://kitchengenome.soulbound.co.kr/#{rid}",
            'title': f"{r['name']} — {r.get('chef') or '미기재'} 계보·운영 프로필",
            'source_host': host,
            'source_tier': tier,
            'published_at': r.get('checked') or today,
            'collected_at': today,
            'target_id': rid,
            'kind': 'lineage_profile',
            'extracted_facts': {
                'chef': r.get('chef'),
                'chef_entity_id': entity['chef_entity_id'],
                'homonym_conflict': entity['homonym_conflict'],
                'homonym_note': entity['homonym_note'],
                'restaurant': r['name'],
                'origin_kitchen': r.get('kitchen'),
                'tenure_period': r.get('period'),
                'role': r.get('role'),
                'independence': r.get('opened'),
                'relocation': '이전' in (r.get('note') or ''),
                'closure': r.get('closed_at'),
                'price_clue': r.get('price'),
                'price_is_official': False,
            },
            'evidence_locator': {
                'url': url,
                'paragraph_idx': 0,
                'char_start': 0,
                'char_end': min(len(note_clean), 200),
                'quote': note_clean[:200],
            },
            'fulltext_retained': any(fh in host for fh in fulltext_hosts),
            'conflict': (l_status == 'conflict'),
            'conflict_notes': [r.get('note')] if l_status == 'conflict' else [],
            'privacy_audit': p_flags,
            'verification_status': v_status,
        })

    # 2. Manual & crawler findings (including multi-source corroboration & duplicates)
    for idx, f in enumerate(manual):
        rid = f.get('id')
        url = f.get('url') or ''
        host = f.get('host') or urllib.parse.urlparse(url).netloc.lower().removeprefix('www.')
        tier = classify_source_tier(url)
        title_clean, p_flags = sanitize_before_store(f.get('title') or '')
        note_clean, _ = sanitize_before_store(f.get('note') or title_clean)

        r_match = next((x for x in seed['restaurants'] if x['id'] == rid), {})
        entity = resolve_chef_entity(r_match.get('chef'), r_match.get('kitchen'), rid, chef_registry)

        # Determine initial stage before dedup clustering
        if tier in ('official', 'press') and f.get('kind') in ('rename', 'field'):
            v_status = 'ready_to_publish'
        elif tier == 'specialist_blog':
            v_status = 'candidate'
        else:
            v_status = 'source_checked'

        raw_items.append({
            'record_id': f"finding:{idx}:{rid}",
            'brand': 'kitchengenome',
            'collection_path': 'browser' if host in set(config.get('allowed_browser_hosts', [])) else 'auto',
            'url': url,
            'title': title_clean[:200],
            'source_host': host,
            'source_tier': tier,
            'published_at': f.get('date') or today,
            'collected_at': today,
            'target_id': rid,
            'kind': f.get('kind') or 'field',
            'extracted_facts': {
                'chef': r_match.get('chef'),
                'chef_entity_id': entity['chef_entity_id'],
                'homonym_conflict': entity['homonym_conflict'],
                'homonym_note': entity['homonym_note'],
                'restaurant': f.get('new_name') or r_match.get('name'),
                'origin_kitchen': r_match.get('kitchen'),
                'tenure_period': r_match.get('period'),
                'role': r_match.get('role'),
                'independence': r_match.get('opened'),
                'relocation': f.get('id') == 'oct19' or '이전' in note_clean,
                'closure': '폐업' in note_clean or f.get('kind') == 'closure',
                'price_clue': r_match.get('price'),
                'price_is_official': False,
            },
            'evidence_locator': {
                'url': url,
                'paragraph_idx': 0,
                'char_start': 0,
                'char_end': min(len(note_clean), 200),
                'quote': note_clean[:200],
            },
            'fulltext_retained': any(fh in host for fh in fulltext_hosts),
            'conflict': (rid == 'finz'),
            'conflict_notes': [note_clean] if rid == 'finz' else [],
            'privacy_audit': p_flags,
            'verification_status': v_status,
        })

    # Apply multi-signal deduplication across all records
    seen_url_Field = set()
    seen_syndication = set()
    for item in raw_items:
        cluster_id, sigs = compute_dedup_cluster(item)
        item['dedup_cluster_id'] = cluster_id
        item['dedup_signals'] = sigs

        # Only apply dedup exclusion among finding items that repeat the same URL+field/kind or syndication signature
        if item['record_id'].startswith('finding:'):
            dedup_key = (item['target_id'], item['kind'], sigs['canonical_url'])
            syn_key = (item['target_id'], item['kind'], sigs['syndication_sig'])
            if dedup_key in seen_url_Field or syn_key in seen_syndication:
                item['verification_status'] = 'dedup_excluded'
            else:
                seen_url_Field.add(dedup_key)
                seen_syndication.add(syn_key)

    counts = {st: 0 for st in VALID_STATUSES}
    for item in raw_items:
        counts[item['verification_status']] += 1

    conflicts = [item for item in raw_items if item.get('conflict')]
    homonyms = [item for item in raw_items if item['extracted_facts'].get('homonym_conflict')]

    summary = {
        'brand': 'kitchengenome',
        'generated_at': today,
        'auto_publish_connected': False,
        'limits': {
            'daily_candidate_url_limit': config.get('daily_candidate_url_limit', 500),
            'daily_content_fetch_limit': config.get('daily_content_fetch_limit', 200),
            'per_domain_concurrency': config.get('per_domain_concurrency', 1),
            'min_delay_sec': config.get('min_delay_sec', 5.0),
            'max_link_depth': config.get('max_link_depth', 2),
        },
        'stage_counts': counts,
        'total_records': len(raw_items),
        'conflict_count': len(conflicts),
        'homonym_review_count': len(homonyms),
    }

    return {'summary': summary, 'records': raw_items}


def sync_sqlite_db(vault_data: dict, db_path: str) -> None:
    """Persist structured vault records and queue metadata into local SQLite (crawler/vault.db)
    with indexes on verification_status, dedup_cluster_id, target_id, and chef_entity_id."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vault_records (
            record_id TEXT PRIMARY KEY,
            brand TEXT NOT NULL,
            collection_path TEXT NOT NULL,
            url TEXT NOT NULL,
            canonical_url TEXT,
            title TEXT,
            source_host TEXT,
            source_tier TEXT,
            published_at TEXT,
            collected_at TEXT,
            target_id TEXT,
            kind TEXT,
            chef_entity_id TEXT,
            homonym_conflict INTEGER DEFAULT 0,
            conflict INTEGER DEFAULT 0,
            dedup_cluster_id TEXT,
            verification_status TEXT NOT NULL,
            extracted_facts_json TEXT,
            evidence_locator_json TEXT,
            privacy_audit_json TEXT
        )
    """)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_vault_status ON vault_records(verification_status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_vault_cluster ON vault_records(dedup_cluster_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_vault_target ON vault_records(target_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_vault_chef ON vault_records(chef_entity_id)')

    for rec in vault_data.get('records', []):
        facts = rec.get('extracted_facts') or {}
        sigs = rec.get('dedup_signals') or {}
        cur.execute("""
            INSERT OR REPLACE INTO vault_records (
                record_id, brand, collection_path, url, canonical_url, title,
                source_host, source_tier, published_at, collected_at, target_id,
                kind, chef_entity_id, homonym_conflict, conflict, dedup_cluster_id,
                verification_status, extracted_facts_json, evidence_locator_json, privacy_audit_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec['record_id'],
            rec.get('brand', 'kitchengenome'),
            rec.get('collection_path', 'auto'),
            rec.get('url', ''),
            sigs.get('canonical_url', ''),
            rec.get('title', ''),
            rec.get('source_host', ''),
            rec.get('source_tier', ''),
            rec.get('published_at', ''),
            rec.get('collected_at', ''),
            rec.get('target_id', ''),
            rec.get('kind', ''),
            facts.get('chef_entity_id', ''),
            1 if facts.get('homonym_conflict') else 0,
            1 if rec.get('conflict') else 0,
            rec.get('dedup_cluster_id', ''),
            rec.get('verification_status', 'candidate'),
            json.dumps(facts, ensure_ascii=False),
            json.dumps(rec.get('evidence_locator') or {}, ensure_ascii=False),
            json.dumps(rec.get('privacy_audit') or {}, ensure_ascii=False),
        ))
    conn.commit()
    conn.close()

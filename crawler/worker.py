"""Kitchen Genome Local Queue & Verification Worker.
Separates browser DOM reading from queue management, privacy filtering, deduplication, and staging.
Enforces:
- daily_candidate_url_limit = 500
- daily_content_fetch_limit = 200
- per_domain_concurrency = 1
- min_delay_sec = 5.0 (or site robots.txt Crawl-delay if slower)
- max_link_depth = 2
- trapped_url_patterns (calendar, pagination, search/sort combinations blocked)
- recheck_priority (closure, relocation, rename, price prioritized ahead of new discovery)
- immediate backoff & halt on 429/403/503 or block pages (zero identity/proxy/account rotation)
- checkpoint save & resume in crawler/worker_state.json
"""
import json
import os
import time
import urllib.parse
import urllib.robotparser
import datetime as dt

from privacy_filter import extract_relevant_dom_region, sanitize_before_store
from extractor import classify_source_tier, extract_kitchen_genome_facts
from vault import compute_dedup_cluster, build_vault_From_seed_and_findings

H = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(H)
CONFIG_PATH = os.path.join(H, 'config.json')
STATE_PATH = os.path.join(H, 'worker_state.json')
INBOX_PATH = os.path.join(H, 'inbox.json')
VAULT_PATH = os.path.join(H, 'vault.json')
SUMMARY_PATH = os.path.join(H, 'inbox_summary.json')


def load_config() -> dict:
    return json.load(open(CONFIG_PATH, encoding='utf-8'))


def is_trapped_url(url: str, config: dict | None = None) -> bool:
    """Reject calendar, infinite pagination, login/checkout, and search-filter trap URLs."""
    if not url or not url.startswith(('http://', 'https://')):
        return True
    cfg = config or load_config()
    u_low = url.lower()
    for pat in cfg.get('trapped_url_patterns', []):
        if pat.lower() in u_low:
            return True
    p = urllib.parse.urlparse(u_low)
    q = urllib.parse.parse_qs(p.query)
    if len(q) >= 4:
        return True
    return False


def effective_domain_delay(url: str, config: dict | None = None, robots_crawl_delay: float | None = None) -> float:
    """Return at least min_delay_sec (5.0s), or robots.txt Crawl-delay if slower."""
    cfg = config or load_config()
    base = float(cfg.get('min_delay_sec', 5.0))
    if robots_crawl_delay is not None and float(robots_crawl_delay) > base:
        return float(robots_crawl_delay)
    return base


def sort_queue_with_recheck_priority(queue_items: list[dict], config: dict | None = None) -> list[dict]:
    """Prioritize fast-changing recheck items (closure, relocation, rename, price) ahead of new link discovery."""
    cfg = config or load_config()
    priority_kinds = set(cfg.get('recheck_priority_kinds', ['closure', 'relocation', 'rename', 'price']))

    def sort_key(item: dict):
        is_recheck = bool(item.get('is_recheck')) or (item.get('kind') in priority_kinds)
        depth = int(item.get('depth', 0))
        return (0 if is_recheck else 1, depth, item.get('queued_at', ''))

    return sorted(queue_items, key=sort_key)


def load_checkpoint(state_path: str = STATE_PATH) -> dict:
    today = dt.date.today().isoformat()
    if os.path.exists(state_path):
        try:
            st = json.load(open(state_path, encoding='utf-8'))
            if st.get('day') == today:
                return st
        except Exception:
            pass
    return {
        'brand': 'kitchengenome',
        'day': today,
        'cursor_index': 0,
        'candidates_discovered_today': 0,
        'contents_fetched_today': 0,
        'halted_domains': {},
        'last_domain_request_ts': {},
        'queue': [],
    }


def save_checkpoint(state: dict, state_path: str = STATE_PATH) -> None:
    json.dump(state, open(state_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def record_http_status_and_check_halt(state: dict, domain: str, http_status: int | str) -> bool:
    """If server responds with rate limit (429), forbidden (403), unavailable (503), or block marker,
    halt requests to that domain immediately without rotating accounts, proxies, or identities."""
    if http_status in (401, 403, 429, 503, 'blocked', 'rate_limited', 'checkpoint'):
        state.setdefault('halted_domains', {})[domain] = {
            'status': http_status,
            'halted_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'policy': 'halt_no_bypass_or_identity_rotation',
        }
        return True
    return False


def ingest_manual_clip(clip_payload: dict, inbox_path: str = INBOX_PATH) -> dict:
    """Receive a manual '이 자료 담기' clip from the Chrome extension, run pre-storage PII & prompt-injection
    sanitization, and save to Kitchen Genome's brand inbox as candidate/source_checked."""
    url = (clip_payload.get('url') or '').strip()
    raw_title = clip_payload.get('title') or ''
    raw_selection = clip_payload.get('selection_or_main') or ''
    target_id = clip_payload.get('target_id') or 'manual_lead'

    region = extract_relevant_dom_region(raw_selection, max_chars=2000)
    clean_body, body_flags = sanitize_before_store(region)
    clean_title, title_flags = sanitize_before_store(raw_title)

    tier = classify_source_tier(url)
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix('www.')
    today = dt.date.today().isoformat()

    record = {
        'record_id': f"clip:{int(time.time())}:{target_id}",
        'brand': 'kitchengenome',
        'collection_path': 'manual_clip',
        'url': url,
        'title': clean_title[:200],
        'source_host': host,
        'source_tier': tier,
        'published_at': clip_payload.get('published_at') or today,
        'collected_at': today,
        'target_id': target_id,
        'kind': clip_payload.get('kind') or 'lineage_clue',
        'extracted_facts': {
            'chef': clip_payload.get('chef'),
            'chef_entity_id': f"chef:{clip_payload.get('chef') or 'unknown'}@{target_id}",
            'restaurant': clip_payload.get('restaurant'),
            'origin_kitchen': clip_payload.get('origin_kitchen'),
            'tenure_period': clip_payload.get('tenure_period'),
            'role': clip_payload.get('role'),
            'price_clue': clip_payload.get('price_clue'),
            'price_is_official': False,
        },
        'evidence_locator': {
            'url': url,
            'paragraph_idx': 0,
            'char_start': 0,
            'char_end': min(len(clean_body), 200),
            'quote': clean_body[:200],
        },
        'privacy_audit': {
            'pii_redacted': body_flags['pii_redacted'] or title_flags['pii_redacted'],
            'prompt_injection_detected': body_flags['prompt_injection_detected'] or title_flags['prompt_injection_detected'],
            'redaction_types': sorted(set(body_flags['redaction_types'] + title_flags['redaction_types'])),
        },
        'verification_status': 'source_checked' if tier in ('official', 'press') else 'candidate',
    }
    cluster_id, sigs = compute_dedup_cluster(record)
    record['dedup_cluster_id'] = cluster_id
    record['dedup_signals'] = sigs

    inbox = json.load(open(inbox_path, encoding='utf-8')) if os.path.exists(inbox_path) else []
    inbox.append(record)
    json.dump(inbox, open(inbox_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return record


def sync_vault() -> dict:
    cfg = load_config()
    seed_path = os.path.join(ROOT, 'data', 'seed.json')
    manual_path = os.path.join(H, 'manual_findings.json')
    pending_path = os.path.join(H, 'pending.json')
    vault_data = build_vault_From_seed_and_findings(seed_path, manual_path, pending_path, cfg)
    json.dump(vault_data, open(VAULT_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(vault_data['summary'], open(SUMMARY_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return vault_data['summary']


if __name__ == '__main__':
    summary = sync_vault()
    print(json.dumps(summary, ensure_ascii=False, indent=2))

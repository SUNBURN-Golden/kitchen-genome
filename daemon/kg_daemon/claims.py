"""Claim / evidence / conflict primitives. Status vocabulary and promotion rules live here."""
import json
import urllib.parse

from .db import new_id, stable_id, utcnow

STATUSES = ('verified', 'source_claim', 'ai_inference', 'unverified')
# Ordering for "is this admissible for output at min_status". ai_inference is lowest on purpose.
RANK = {'verified': 3, 'source_claim': 2, 'unverified': 1, 'ai_inference': 0}
# Tiers that may count toward 'verified'. social_lead / search_lead never do.
TRUSTED_TIERS = ('official', 'press', 'human')


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url or '').netloc.lower().removeprefix('www.').removeprefix('m.')


def create_claim(conn, assertion_type: str, status: str, created_by: str, note: str | None = None,
                 claim_id: str | None = None, now: str | None = None) -> str:
    if status not in STATUSES:
        raise ValueError(f'bad claim status {status!r}')
    now = now or utcnow()
    cid = claim_id or new_id('clm')
    conn.execute('INSERT OR IGNORE INTO claim(claim_id,assertion_type,status,created_by,note,created_at,status_changed_at) VALUES (?,?,?,?,?,?,?)',
                 (cid, assertion_type, status, created_by, note, now, now))
    return cid


def add_evidence(conn, *, source_id: str, source_url: str, collected_at: str, locator: dict | None = None,
                 quote: str | None = None, effective_from: str | None = None, published_at: str | None = None,
                 document_id: str | None = None, evidence_id: str | None = None) -> str:
    if quote is not None and len(quote) > 200:
        raise ValueError('evidence quote must be <= 200 chars (short quotes only)')
    loc = json.dumps(locator or {}, ensure_ascii=False, sort_keys=True)
    eid = evidence_id or stable_id('evd', source_id, source_url, loc, collected_at)
    conn.execute('INSERT OR IGNORE INTO evidence(evidence_id,document_id,source_id,source_url,locator_json,quote,collected_at,effective_from,published_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                 (eid, document_id, source_id, source_url, loc, quote, collected_at, effective_from, published_at, utcnow()))
    return eid


def link_evidence(conn, claim_id: str, evidence_id: str, stance: str = 'supports') -> None:
    conn.execute('INSERT OR IGNORE INTO claim_evidence(claim_id,evidence_id,stance) VALUES (?,?,?)', (claim_id, evidence_id, stance))


def supporting_hosts(conn, claim_id: str) -> set[str]:
    """Distinct hosts of supporting evidence from trusted tiers."""
    rows = conn.execute("""SELECT e.source_url FROM claim_evidence ce JOIN evidence e ON e.evidence_id=ce.evidence_id
                           JOIN source s ON s.source_id=e.source_id
                           WHERE ce.claim_id=? AND ce.stance='supports' AND s.tier IN ('official','press','human')""", (claim_id,)).fetchall()
    return {host_of(r['source_url']) for r in rows if r['source_url']}


def can_verify(conn, claim_id: str) -> bool:
    """verified needs >=2 independent trusted hosts, or 1 official/human source."""
    hosts = supporting_hosts(conn, claim_id)
    if len(hosts) >= 2:
        return True
    row = conn.execute("""SELECT 1 FROM claim_evidence ce JOIN evidence e ON e.evidence_id=ce.evidence_id
                          JOIN source s ON s.source_id=e.source_id
                          WHERE ce.claim_id=? AND ce.stance='supports' AND s.tier IN ('official','human')""", (claim_id,)).fetchone()
    return row is not None


def set_claim_status(conn, claim_id: str, new_status: str, changed_by: str, reason: str | None = None, now: str | None = None) -> None:
    if new_status not in STATUSES:
        raise ValueError(f'bad claim status {new_status!r}')
    row = conn.execute('SELECT status FROM claim WHERE claim_id=?', (claim_id,)).fetchone()
    if row is None:
        raise KeyError(claim_id)
    old = row['status']
    if old == new_status:
        return
    if new_status == 'verified':
        if old == 'ai_inference' and not changed_by.startswith('human:'):
            raise ValueError('ai_inference can only become verified by a human reviewer')
        if not can_verify(conn, claim_id):
            raise ValueError('verified requires >=2 independent trusted hosts or one official/human source')
    now = now or utcnow()
    conn.execute('UPDATE claim SET status=?, status_changed_at=? WHERE claim_id=?', (new_status, now, claim_id))
    conn.execute('UPDATE claim_status_history SET changed_by=?, reason=? WHERE history_id=(SELECT MAX(history_id) FROM claim_status_history WHERE claim_id=?)',
                 (changed_by, reason, claim_id))


def open_conflict(conn, subject_type: str, subject_id: str, field: str, claim_a: str, claim_b: str, now: str | None = None) -> str:
    """Record a source disagreement. Neither claim is overwritten or deleted."""
    a, b = sorted((claim_a, claim_b))
    cid = stable_id('cfl', subject_type, subject_id, field, a, b)
    conn.execute('INSERT OR IGNORE INTO conflict(conflict_id,subject_type,subject_id,field,claim_a,claim_b,opened_at) VALUES (?,?,?,?,?,?,?)',
                 (cid, subject_type, subject_id, field, a, b, now or utcnow()))
    return cid


def queue_review(conn, reason: str, subject_type: str, subject_id: str | None, payload: dict | None = None,
                 dedupe_key: str | None = None, now: str | None = None) -> str:
    rid = stable_id('rev', dedupe_key) if dedupe_key else new_id('rev')
    conn.execute('INSERT OR IGNORE INTO review_queue(review_id,reason,subject_type,subject_id,payload_json,dedupe_key,created_at) VALUES (?,?,?,?,?,?,?)',
                 (rid, reason, subject_type, subject_id, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), dedupe_key, now or utcnow()))
    return rid


def quote_fingerprint(quote: str | None) -> str:
    """Wording fingerprint: letters/digits only, lower-cased. Syndicated or reposted articles repeat the same sentence verbatim."""
    import re
    return re.sub(r'[^0-9a-z가-힣]', '', (quote or '').lower())[:160]


def independent_sources(evidence: list[tuple[str, str | None]]) -> int:
    """[(url, quote)] -> number of INDEPENDENT sources. Hosts are merged when they carry the same wording (a repost / wire copy of one article
    counts once); different hosts with different wording count separately. Union-find over hosts linked by a shared fingerprint."""
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by_fp: dict = {}
    for url, quote in evidence:
        h = host_of(url)
        find(h)
        fp = quote_fingerprint(quote)
        if len(fp) >= 12:
            if fp in by_fp:
                parent[find(h)] = find(by_fp[fp])
            else:
                by_fp[fp] = h
    return len({find(h) for h in parent})

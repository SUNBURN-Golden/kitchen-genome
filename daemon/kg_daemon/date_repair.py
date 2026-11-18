"""Repair legacy timeline dates before readers use them; preserve every change."""
from .collect_bridge import normalized_effective_date
from .db import utcnow
from .ontology import parse_iso_date


def repair_timeline_dates(conn):
    """Idempotent repair. Ambiguous/unparseable dates become unknown, never guessed."""
    now = utcnow()
    repaired = 0

    def record(table, row_id, field, old, new):
        nonlocal repaired
        conn.execute("""INSERT INTO correction_history
            (table_name,row_id,field,old_value,new_value,reason,corrected_by,corrected_at)
            VALUES (?,?,?,?,?,'malformed_date','daemon:service_output_recompute',?)""",
                     (table, row_id, field, old, new, now))
        repaired += 1

    for row in conn.execute('SELECT * FROM restaurant_status_event WHERE effective_date IS NOT NULL').fetchall():
        old = row['effective_date']
        parsed = parse_iso_date(old)
        if parsed and parsed[0] == old:
            continue
        if not parsed:
            publications = conn.execute("""SELECT e.published_at FROM claim_evidence ce
                JOIN evidence e USING(evidence_id) WHERE ce.claim_id=? AND ce.stance='supports'""",
                                        (row['claim_id'],)).fetchall()
            candidates = {normalized_effective_date({'date_text': old, 'published_at': e['published_at']})
                          for e in publications}
            # Explicit years can be recovered even without linked evidence.
            if not publications:
                candidates = {normalized_effective_date({'date_text': old})}
            candidates.discard(None)
            parsed = candidates.pop() if len(candidates) == 1 else None
        value, precision = parsed if parsed else (None, None)
        conn.execute('UPDATE restaurant_status_event SET effective_date=?, effective_precision=? WHERE event_id=?',
                     (value, precision, row['event_id']))
        record('restaurant_status_event', row['event_id'], 'effective_date', old, value)

    for row in conn.execute('SELECT * FROM employment WHERE valid_from IS NOT NULL OR valid_to IS NOT NULL').fetchall():
        # Employment dates have separate start/end claims. Clear malformed
        # wording conservatively instead of inferring a tenure from publication.
        for field in ('valid_from', 'valid_to'):
            old = row[field]
            if old is None:
                continue
            parsed = parse_iso_date(old)
            if parsed and parsed[0] == old:
                continue
            value, precision = parsed if parsed else (None, None)
            end = ", valid_to_kind='unknown'" if field == 'valid_to' and value is None else ''
            conn.execute(f'UPDATE employment SET {field}=?, {field}_precision=?{end} WHERE employment_id=?',
                         (value, precision, row['employment_id']))
            record('employment', row['employment_id'], field, old, value)
    return repaired

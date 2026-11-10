"""Offline, deterministic weekly draft writer. No provider, subprocess, or publishing calls."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
HEADER = ('DRAFT — NOT APPROVED FOR PUBLICATION. This draft MUST pass Gemini Argon review\n'
          'via `marketing/argon_review.sh` before any human/VIRAL publishes it. Generation\n'
          'does not run Argon or publish to X, Instagram, email, or any network service.\n')
MARKER = '<!-- kitchen-genome generated chef-move digest v1 -->'
NAME = re.compile(r'chef-moves-(\d{4}-\d{2}-\d{2})\.md')


def week_start(value):
    date = dt.date.fromisoformat(value)
    if date.weekday() != 0:
        raise ValueError('week must be a Monday')
    return date


def approval_date(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(KST).date()


def published(seed):
    rows = list(seed.get('ai_reviewed_unlinked', []))
    for holder in seed.get('restaurants', []) + seed.get('kitchens', []):
        rows.extend(holder.get('ai_reviewed', []))
    return {(r.get('extraction_id'), r.get('review_id')): r for r in rows
            if r.get('provenance') == 'ai_reviewed' and r.get('badge') == 'AI 검토'}


def atomic_write(path, content):
    if path.is_symlink():
        raise ValueError('symlink output refused')
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return False
    fd, name = tempfile.mkstemp(prefix='.digest-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def build(repo, *, week=None, now=None):
    repo = Path(repo).absolute()
    drafts = repo / 'marketing' / 'drafts'
    if any(p.is_symlink() for p in (repo / 'marketing', drafts)):
        raise ValueError('symlink draft directory refused')
    drafts.mkdir(parents=True, exist_ok=True)
    now = now or dt.datetime.now(KST)
    today = now.astimezone(KST).date()
    completed = today - dt.timedelta(days=today.weekday()+7)
    requested = week_start(week) if week else completed
    seed_path, log_path = repo/'data/seed.json', repo/'data/ai_review_log.jsonl'
    seed = json.loads(seed_path.read_text(encoding='utf-8'))
    live = published(seed)
    events = [json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines() if line.strip()] if log_path.exists() else []
    approvals = {}
    for e in events:
        key = (e.get('extraction_id'),e.get('review_id'))
        if e.get('action') == 'rollback':
            approvals.pop(key, None)
        elif e.get('action') == 'approve' and e.get('published'):
            approvals[key] = e
    by_week = {}
    for key, e in approvals.items():
        r = live.get(key)
        if not r or r.get('label') not in ('career','employment_claim') or not r.get('chef') or not r.get('src') or not r.get('quote'):
            continue
        if e.get('quote') != r['quote'] or e.get('src') != r['src']:
            continue
        date = approval_date(e['at'])
        monday = date-dt.timedelta(days=date.weekday())
        by_week.setdefault(monday,[]).append(r)
    weeks = {requested}
    if not week:
        weeks.update(w for w in by_week if w <= completed)
    # Always remove stale rolled-back copy from older generated files too.
    for path in drafts.iterdir():
        match = NAME.fullmatch(path.name)
        if match:
            if path.is_symlink():
                raise ValueError('symlink output refused')
            weeks.add(week_start(match[1]))
    changed = []
    for monday in sorted(weeks):
        path = drafts / f'chef-moves-{monday.isoformat()}.md'
        if path.exists() and MARKER not in path.read_text(encoding='utf-8'):
            raise ValueError('refusing to overwrite a human draft')
        rows = sorted(by_week.get(monday,[]), key=lambda r:(r['reviewed_at'],r['review_id'],r['extraction_id']))
        unique, seen = [], set()
        for r in rows:
            claim = (r['label'],r['chef'],r['quote'],r['src'])
            if claim not in seen:
                unique.append(r)
                seen.add(claim)
        if not unique and not path.exists():
            continue
        text = MARKER+'\n\n'+HEADER+f'\n# Findings approved {monday} through {monday+dt.timedelta(days=6)} (KST)\n\n'
        text += 'Approval week is shown; the employment or move may have occurred earlier.\n'
        for r in unique:
            # JSON quoting keeps embedded Markdown/source text visibly verbatim as data.
            text += '\n- Chef: '+json.dumps(r['chef'],ensure_ascii=False)+'\n'
            text += '  Approved quote: '+json.dumps(r['quote'],ensure_ascii=False)+'\n'
            text += '  Source: '+r['src']+'\n'
            text += '  Review: '+r['review_id']+'; extraction: '+r['extraction_id']+'\n'
        if not unique:
            text += '\nWITHDRAWN — no currently published approvals remain for this week.\n'
        if atomic_write(path,text):
            changed.append(path.name)
            # Separate human/Argon revisions are preserved and explicitly invalidated.
            if any(p.name != path.name for p in drafts.glob(path.stem+'*')):
                atomic_write(drafts/(path.stem+'.review-required.txt'), 'Generated input changed. Human/Argon revisions MUST receive a fresh Argon review before publication.\n')
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument('--week', help='Monday YYYY-MM-DD in KST; default: last completed week')
    args = ap.parse_args(argv)
    try:
        changed = build(args.repo,week=args.week)
    except (ValueError,OSError,KeyError):
        ap.exit(2,'DIGEST=failed reason=invalid_input\n')
    print('DIGEST=draft_only changed='+str(len(changed))+' argon=required')


if __name__ == '__main__':
    main()

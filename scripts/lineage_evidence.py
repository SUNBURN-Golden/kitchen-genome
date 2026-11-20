"""Render public lineage evidence in the static restaurant pages."""
from html import escape
import re
from urllib.parse import urlsplit


def evidence_html(row, heading, labels, source_label):
    items = []
    for ref in row.get('lineage_evidence', []):
        url, quote = ref.get('src'), ref.get('quote')
        if not url or not quote or re.search(r'[\s\x00-\x1f\x7f\\#]', url):
            continue
        try:
            parts = urlsplit(url)
            if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None:
                continue
        except ValueError:
            continue
        label = labels.get(ref.get('kind'), source_label)
        items.append(f'<li>{escape(label)} · “{escape(quote)}” '
                     f'(<a href="{escape(url)}" rel="nofollow noopener" target="_blank">{escape(source_label)}</a>)</li>')
    return f'<h2>{escape(heading)}</h2><ul>{"".join(items)}</ul>' if items else ''

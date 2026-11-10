#!/usr/bin/env bash
# Usage: argon_review.sh <file> [model]   -> prints Gemini Argon's revised copy to stdout
set -euo pipefail
digest=0
if [ "${1:-}" = --digest ]; then digest=1; shift; fi
f="${1:?usage: argon_review.sh <file>}"; model="${2:-gemini-4-argon-medium}"
prompt="You are reviewing public social copy for Kitchen Genome (Seoul chef-lineage tracker) before it is posted on X.
Revise for natural native English and X engagement.
HARD RULES: Do NOT add any new facts, names, numbers, awards, or claims. Keep every existing fact, Korean name in parentheses, and URL exactly as-is (do not alter URLs). Each post must be <=280 characters (URLs count as 23). Bio <=160 characters. Max 2 hashtags per post. Do not use tools or edit files.
Output ONLY the revised markdown in this structure: '## Profile' (Display name, Bio, Website field), then '## Posts' with '### 1.' .. '### 5.' each containing only the post text. Then a final line 'NOTES:' with a one-line list of changes.

--- COPY ---
$(cat "$f")"
if [ "$digest" = 1 ]; then
  prompt="Review this Kitchen Genome weekly draft for accuracy, clarity and safe public wording. Do not use tools or edit files. Preserve the DRAFT header, every approved verbatim quote, Korean name, URL and review ID exactly. Add no facts or claims. Retain the digest format and approval-week distinction; flag unsupported copy. Output revised Markdown followed by NOTES. A human must approve any publication.

--- COPY ---
$(cat "$f")"
fi
exec agy -p "$prompt" --model "$model" --output-format json --print-timeout 300s --disable-slash-commands

# Offline weekly drafts

Run `python3 scripts/build_weekly_digest.py` to generate the most recent completed
KST week and recover missed weeks, or supply `--week YYYY-MM-DD` (a Monday).
Only still-published, approved career/employment quotes are included. Regeneration
withdraws rolled-back entries, including older weeks. This command has no network
or publication step.

Every draft MUST pass Gemini Argon review through `marketing/argon_review.sh`
before any human/VIRAL publishes it. The script uses a network model and may cost
money: do not invoke it under the current no-spend order. It is never invoked by
the daily pipeline. Generation is not approval. Keep human/Argon revisions in
separate files; changed generated input requires a new Argon review.

For a later explicitly authorized digest review, use
`marketing/argon_review.sh --digest marketing/drafts/chef-moves-YYYY-MM-DD.md`.
The default two-argument profile/posts interface is retained for existing copy.
